"""
ZendureProxy – AppDaemon entry point.

Responsibilities:
  * Load configuration and initialise shared state.
  * Start the aiohttp HTTP server.
  * Accept incoming HA requests, enqueue them, return futures to callers.
  * Run the background queue-processor loop that drives GET/POST execution.
  * Wire together all the other modules; contain no business logic itself.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import aiohttp
import aiohttp.web
import appdaemon.plugins.hass.hassapi as hass

from zendure_proxy_config import Config, load_config
from zendure_proxy_device_client import DeviceClient
from zendure_proxy_get_handler import execute_get
from zendure_proxy_post_handler import execute_post
from zendure_proxy_power import PROXY_VERSION, now
from zendure_proxy_queue import RequestQueue
from zendure_proxy_state import DeviceState, ProxyState


class ZendureProxy(hass.Hass):
    """AppDaemon app: async HTTP proxy between Home Assistant and Zendure devices."""

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        self._cfg: Config = load_config(self.args)

        self._clients: list[DeviceClient] = [
            DeviceClient(ip, self.log) for ip in self._cfg.device_ips
        ]
        self._state = ProxyState(
            device_count=len(self._cfg.device_ips),
            devices=[DeviceState(ip=ip) for ip in self._cfg.device_ips],
            equal_mode=self._cfg.equal_mode,
            always_dual_mode=self._cfg.always_dual_mode,
        )
        self._queue = RequestQueue()

        self._processor_task = asyncio.ensure_future(self._processor())
        asyncio.ensure_future(self._init_serial_numbers())

        await self._start_server()

        if not self._cfg.device_ips:
            self.log("WARNING: no Zendure IPs configured – proxy will not function",
                     level="WARNING")
        else:
            self.log(
                f"Zendure proxy v{PROXY_VERSION} started | "
                f"port={self._cfg.server_port} | "
                f"devices={self._cfg.device_ips}"
            )

    async def terminate(self) -> None:
        if hasattr(self, "_runner"):
            await self._runner.cleanup()
        if hasattr(self, "_processor_task"):
            self._processor_task.cancel()
        for client in self._clients:
            await client.close()
        self.log("Zendure proxy stopped")

    # ── HTTP server ────────────────────────────────────────────────────────────

    async def _start_server(self) -> None:
        app = aiohttp.web.Application()
        for prefix in ("", "/endpoint"):
            app.router.add_get(f"{prefix}/properties/report", self._handle_get)
            app.router.add_post(f"{prefix}/properties/write", self._handle_post)
        self._runner = aiohttp.web.AppRunner(app)
        await self._runner.setup()
        site = aiohttp.web.TCPSite(
            self._runner, self._cfg.server_host, self._cfg.server_port
        )
        await site.start()

    # ── HTTP handlers ──────────────────────────────────────────────────────────

    async def _handle_get(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        self._state.counter_get_received += 1
        self._state.latest_get_ts = now()

        if not self._cfg.device_ips:
            return aiohttp.web.Response(status=503, text="No devices configured")

        if not all(d.sn for d in self._state.devices):
            if self._state.last_get_response:
                return aiohttp.web.json_response(self._state.last_get_response)
            return aiohttp.web.Response(status=503, text="Initializing – try again shortly")

        fut = await self._queue.enqueue_get()
        try:
            data = await asyncio.wait_for(fut, timeout=30.0)
            self._state.counter_get_replies += 1
            return aiohttp.web.json_response(data)
        except asyncio.TimeoutError:
            if self._state.last_get_response:
                return aiohttp.web.json_response(self._state.last_get_response)
            return aiohttp.web.Response(status=504, text="Upstream timeout")
        except Exception as exc:
            self.log(f"GET handler error: {exc}", level="ERROR")
            if self._state.last_get_response:
                return aiohttp.web.json_response(self._state.last_get_response)
            return aiohttp.web.Response(status=502, text=str(exc))

    async def _handle_post(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        self._state.counter_post_received += 1

        if not self._cfg.device_ips:
            return aiohttp.web.json_response({"ack": "pong"})

        try:
            payload = await request.json()
        except Exception:
            return aiohttp.web.Response(status=400, text="Invalid JSON")

        fut = await self._queue.enqueue_post(payload)
        try:
            data = await asyncio.wait_for(fut, timeout=30.0)
            self._state.counter_post_replies += 1
            return aiohttp.web.json_response(data)
        except asyncio.TimeoutError:
            return aiohttp.web.json_response({"ack": "pong"})
        except Exception as exc:
            self.log(f"POST handler error: {exc}", level="ERROR")
            return aiohttp.web.json_response({"ack": "pong"})

    # ── Queue processor ────────────────────────────────────────────────────────

    async def _processor(self) -> None:
        """
        Main worker loop.

        Each iteration:
          1. Wait for at least one queued request (via RequestQueue.drain).
          2. Execute one real GET → resolve all coalesced GET futures.
          3. Execute deduplicated POSTs → resolve each future; skipped ones
             get an immediate {"ack":"pong"}.
          4. Optionally re-run the last power POST (manual-mode repeat) so
             that SoC balancing stays active without new HA commands.
        """
        while True:
            try:
                gets, posts = await self._queue.drain()

                get_response: Optional[dict] = None

                if gets:
                    try:
                        get_response = await execute_get(
                            self._clients, self._state, self._cfg, self.log
                        )
                        for fut in gets:
                            if not fut.done():
                                fut.set_result(get_response)
                    except Exception as exc:
                        self.log(f"GET execution failed: {exc}", level="ERROR")
                        cached = self._state.last_get_response
                        for fut in gets:
                            if not fut.done():
                                if cached:
                                    fut.set_result(cached)
                                else:
                                    fut.set_exception(exc)

                for latest_payload, latest_fut, skipped_futs in posts:
                    for fut in skipped_futs:
                        if not fut.done():
                            fut.set_result({"ack": "pong"})
                    try:
                        resp = await execute_post(
                            latest_payload, self._clients, self._state,
                            self._cfg, self.log,
                        )
                        if not latest_fut.done():
                            latest_fut.set_result(resp)
                    except Exception as exc:
                        self.log(f"POST execution failed: {exc}", level="ERROR")
                        if not latest_fut.done():
                            latest_fut.set_result({"ack": "pong"})

                # Manual-mode repeat: re-apply the last power command after each
                # GET cycle so SoC balancing continues without new HA POSTs.
                if (
                    gets
                    and get_response is not None
                    and not posts
                    and self._cfg.manual_mode_repeat
                    and self._state.last_post_payload is not None
                    and self._state.latest_power_message_ts > 0
                ):
                    try:
                        await execute_post(
                            self._state.last_post_payload,
                            self._clients, self._state, self._cfg, self.log,
                            is_repeat=True,
                        )
                    except Exception as exc:
                        self.log(f"Manual-repeat failed: {exc}", level="WARNING")

            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.log(f"Queue processor error: {exc}", level="ERROR")
                await asyncio.sleep(1)

    # ── Startup helper ─────────────────────────────────────────────────────────

    async def _init_serial_numbers(self) -> None:
        """Fetch and store each device's serial number at startup."""
        for i, client in enumerate(self._clients):
            try:
                data = await client.get()
                if data and "sn" in data:
                    self._state.devices[i].sn = data["sn"]
                    self.log(f"Device {i+1} SN: {data['sn']}")
                else:
                    self.log(
                        f"Device {i+1}: could not fetch serial number",
                        level="WARNING",
                    )
            except Exception as exc:
                self.log(f"Device {i+1} SN init error: {exc}", level="WARNING")
