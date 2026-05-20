"""
ZendureProxy – AppDaemon entry point.

Responsibilities:
  * Load configuration and initialise shared state.
  * Start the aiohttp HTTP server.
  * Register AppDaemon HTTP API endpoints for Home Assistant automations.
  * Accept incoming HA requests, enqueue them, return futures to callers.
  * Run the background queue-processor loop that drives GET/POST execution.
  * Wire together all the other modules; contain no business logic itself.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Optional

import aiohttp
import aiohttp.web
import appdaemon.plugins.hass.hassapi as hass

from zendure_proxy_config import Config, load_config
from zendure_proxy_device_client import DeviceClient
from zendure_proxy_get_handler import execute_get
from zendure_proxy_logging import ProxyFileLogger, render_log_dashboard
from zendure_proxy_post_handler import execute_post
from zendure_proxy_power import PROXY_VERSION, now
from zendure_proxy_queue import RequestQueue
from zendure_proxy_state import DeviceState, ProxyState


class ZendureProxy(hass.Hass):
    """AppDaemon app: async HTTP proxy between Home Assistant and Zendure devices."""

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        self._cfg: Config = load_config(self.args)
        self._file_logger: Optional[ProxyFileLogger] = self._create_file_logger()

        self._clients: list[DeviceClient] = [
            DeviceClient(ip, self._log) for ip in self._cfg.device_ips
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

        self._report_endpoint_handle = await self.register_endpoint(
            self._api_report, "zendure_proxy_report"
        )
        self._write_endpoint_handle = await self.register_endpoint(
            self._api_write, "zendure_proxy_write"
        )
        if self._cfg.log_dashboard_enabled:
            self._logs_route_handle = await self.register_route(
                self._logs_dashboard, self._cfg.log_dashboard_route
            )
        await self._start_server()

        if not self._cfg.device_ips:
            self._log("WARNING: no Zendure IPs configured - proxy will not function",
                      level="WARNING")
        else:
            self._log(
                f"Zendure proxy v{PROXY_VERSION} started | "
                f"port={self._cfg.server_port} | "
                f"api_endpoints=['zendure_proxy_report', 'zendure_proxy_write'] | "
                f"log_dashboard=/app/{self._cfg.log_dashboard_route} | "
                f"devices={self._cfg.device_ips}"
            )

    async def terminate(self) -> None:
        if getattr(self, "_logs_route_handle", None):
            await self.deregister_route(self._logs_route_handle)
        if getattr(self, "_report_endpoint_handle", None):
            await self.deregister_endpoint(self._report_endpoint_handle)
        if getattr(self, "_write_endpoint_handle", None):
            await self.deregister_endpoint(self._write_endpoint_handle)
        if hasattr(self, "_runner"):
            await self._runner.cleanup()
        if hasattr(self, "_processor_task"):
            self._processor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._processor_task
        for client in self._clients:
            await client.close()
        self._log("Zendure proxy stopped")
        if self._file_logger is not None:
            self._file_logger.close()

    # -- Logging ---------------------------------------------------------------

    def _create_file_logger(self) -> Optional[ProxyFileLogger]:
        if not self._cfg.log_file_enabled:
            return None

        log_path = self._cfg.log_file_path or str(
            Path(__file__).resolve().parents[2] / "logs" / "zendure_proxy.log"
        )
        try:
            return ProxyFileLogger(
                log_path,
                self._cfg.log_file_max_bytes,
                self._cfg.log_file_backup_count,
            )
        except Exception as exc:
            self.log(f"Could not start Zendure proxy file logger: {exc}", level="WARNING")
            return None

    def _log(self, message: str, level: str = "INFO", **kwargs) -> None:
        self.log(message, level=level, **kwargs)
        if self._file_logger is not None:
            self._file_logger.log(message, level)

    async def _logs_dashboard(
        self, request: aiohttp.web.Request, _kwargs
    ) -> aiohttp.web.Response:
        if self._file_logger is None:
            return aiohttp.web.Response(
                text="Zendure proxy file logging is disabled.",
                content_type="text/plain",
            )

        if request.query.get("download") == "1":
            return aiohttp.web.Response(
                text=self._file_logger.read_all(),
                content_type="text/plain",
                headers={
                    "Content-Disposition": 'attachment; filename="zendure_proxy.log"'
                },
            )

        html = render_log_dashboard(
            "Zendure proxy log",
            self._file_logger.read_tail(self._cfg.log_dashboard_lines),
            "?download=1",
        )
        return aiohttp.web.Response(text=html, content_type="text/html")

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
        data, status = await self._execute_report_request()
        return aiohttp.web.json_response(data, status=status)

    async def _handle_post(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        try:
            payload = await request.json()
        except Exception:
            return aiohttp.web.Response(status=400, text="Invalid JSON")

        data, status = await self._execute_write_request(payload)
        return aiohttp.web.json_response(data, status=status)

    # ── AppDaemon API endpoints ────────────────────────────────────────────────

    async def _api_report(self, _args, _kwargs) -> tuple[dict, int]:
        return await self._execute_report_request()

    async def _api_write(self, json_obj, _kwargs) -> tuple[dict, int]:
        if not isinstance(json_obj, dict):
            return {"error": "Invalid JSON"}, 400

        return await self._execute_write_request(json_obj)

    # ── Request execution shared by aiohttp and AppDaemon API ──────────────────

    async def _execute_report_request(self) -> tuple[dict, int]:
        self._state.counter_get_received += 1
        self._state.latest_get_ts = now()

        if not self._cfg.device_ips:
            return {"error": "No devices configured"}, 503

        if not all(d.sn for d in self._state.devices):
            if self._state.last_get_response:
                return self._state.last_get_response, 200
            return {"error": "Initializing - try again shortly"}, 503

        fut = await self._queue.enqueue_get()
        try:
            data = await asyncio.wait_for(fut, timeout=30.0)
            self._state.counter_get_replies += 1
            return data, 200
        except asyncio.TimeoutError:
            if self._state.last_get_response:
                return self._state.last_get_response, 200
            return {"error": "Upstream timeout"}, 504
        except Exception as exc:
            self._log(f"GET handler error: {exc}", level="ERROR")
            if self._state.last_get_response:
                return self._state.last_get_response, 200
            return {"error": str(exc)}, 502

    async def _execute_write_request(self, payload: dict) -> tuple[dict, int]:
        self._state.counter_post_received += 1

        if not self._cfg.device_ips:
            return {"ack": "pong"}, 200

        fut = await self._queue.enqueue_post(payload)
        try:
            data = await asyncio.wait_for(fut, timeout=30.0)
            self._state.counter_post_replies += 1
            return data, 200
        except asyncio.TimeoutError:
            return {"ack": "pong"}, 200
        except Exception as exc:
            self._log(f"POST handler error: {exc}", level="ERROR")
            return {"ack": "pong"}, 200

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
                skipped_post_count = sum(len(skipped) for _, _, skipped in posts)
                if len(gets) > 1:
                    self._log(
                        "Queue cleanup: coalesced "
                        f"{len(gets)} queued GET requests into 1 upstream GET",
                        level="WARNING",
                    )
                if skipped_post_count:
                    self._log(
                        "Queue cleanup: deduplicated "
                        f"{skipped_post_count} queued POST requests",
                        level="WARNING",
                    )

                get_response: Optional[dict] = None

                if gets:
                    try:
                        get_response = await execute_get(
                            self._clients, self._state, self._cfg, self._log
                        )
                        for fut in gets:
                            if not fut.done():
                                fut.set_result(get_response)
                    except Exception as exc:
                        self._log(f"GET execution failed: {exc}", level="ERROR")
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
                            self._cfg, self._log,
                        )
                        if not latest_fut.done():
                            latest_fut.set_result(resp)
                    except Exception as exc:
                        self._log(f"POST execution failed: {exc}", level="ERROR")
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
                            self._clients, self._state, self._cfg, self._log,
                            is_repeat=True,
                        )
                    except Exception as exc:
                        self._log(f"Manual-repeat failed: {exc}", level="WARNING")

            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log(f"Queue processor error: {exc}", level="ERROR")
                await asyncio.sleep(1)

    # ── Startup helper ─────────────────────────────────────────────────────────

    async def _init_serial_numbers(self) -> None:
        """Fetch and store each device's serial number at startup."""
        for i, client in enumerate(self._clients):
            try:
                data = await client.get()
                if data and "sn" in data:
                    self._state.devices[i].sn = data["sn"]
                    self._log(f"Device {i+1} SN: {data['sn']}")
                else:
                    self._log(
                        f"Device {i+1}: could not fetch serial number",
                        level="WARNING",
                    )
            except Exception as exc:
                self._log(f"Device {i+1} SN init error: {exc}", level="WARNING")
