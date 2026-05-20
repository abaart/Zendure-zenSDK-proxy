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
import inspect
import json
import math
from pathlib import Path
import time
from typing import Any, Optional

import aiohttp
import aiohttp.web
import appdaemon.plugins.hass.hassapi as hass

from zendure_proxy_config import Config, load_config
from zendure_proxy_device_client import DeviceClient
from zendure_proxy_get_handler import execute_get
from zendure_proxy_ha_sensors import build_proxy_ha_sensors
from zendure_proxy_logging import ProxyFileLogger, render_log_dashboard
from zendure_proxy_metrics import MetricsRegistry, render_metrics_dashboard
from zendure_proxy_mqtt_discovery import mqtt_sensor_config, mqtt_sensor_topics
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
        self._mqtt_api = self._get_mqtt_api()
        self._mqtt_sensor_error_logged = False
        self._metrics = MetricsRegistry(len(self._cfg.device_ips))
        self._proxy_ha_sensor_owned_entities: set[str] = set()
        await self._restore_metrics_counters_from_ha()

        self._clients: list[DeviceClient] = [
            DeviceClient(ip, self._proxy_log, self._metrics, idx)
            for idx, ip in enumerate(self._cfg.device_ips)
        ]
        self._state = ProxyState(
            device_count=len(self._cfg.device_ips),
            devices=[DeviceState(ip=ip) for ip in self._cfg.device_ips],
            equal_mode=self._cfg.equal_mode,
            always_dual_mode=self._cfg.always_dual_mode,
            dualmode_damper_enabled=self._cfg.damper_enable,
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
        if self._cfg.metrics_enabled and self._cfg.metrics_dashboard_enabled:
            self._metrics_route_handle = await self.register_route(
                self._metrics_dashboard, self._cfg.metrics_dashboard_route
            )
        if self._cfg.metrics_enabled and self._cfg.metrics_ha_sensors_enabled:
            self._metrics_sensor_timer = await self.run_every(
                self._publish_metrics_sensors,
                "now",
                self._cfg.metrics_ha_sensors_interval,
            )
        await self._start_server()

        if not self._cfg.device_ips:
            self._proxy_log(
                "WARNING: no Zendure IPs configured - proxy will not function",
                level="WARNING",
            )
        else:
            self._proxy_log(
                f"Zendure proxy v{PROXY_VERSION} started | "
                f"port={self._cfg.server_port} | "
                f"api_endpoints=['zendure_proxy_report', 'zendure_proxy_write'] | "
                f"log_dashboard=/app/{self._cfg.log_dashboard_route} | "
                f"metrics_dashboard=/app/{self._cfg.metrics_dashboard_route} | "
                f"devices={self._cfg.device_ips}"
            )

    async def terminate(self) -> None:
        if getattr(self, "_metrics_sensor_timer", None):
            await self.cancel_timer(self._metrics_sensor_timer, silent=True)
        if getattr(self, "_metrics_route_handle", None):
            await self.deregister_route(self._metrics_route_handle)
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
        self._proxy_log("Zendure proxy stopped")
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

    def _proxy_log(self, message: str, level: str = "INFO", **kwargs) -> None:
        self.log(message, level=level, **kwargs)
        if self._file_logger is not None:
            self._file_logger.log(message, level)

    def _get_mqtt_api(self):
        if not self._cfg.proxy_ha_sensors_mqtt_discovery_enabled:
            return None
        try:
            return self.get_plugin_api("MQTT")
        except Exception as exc:
            self.log(
                "Could not start MQTT discovery for Zendure proxy sensors; "
                f"falling back to AppDaemon set_state sensors: {exc}",
                level="WARNING",
            )
            return None

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

    async def _metrics_dashboard(
        self, _request: aiohttp.web.Request, _kwargs
    ) -> aiohttp.web.Response:
        html = render_metrics_dashboard(
            "Zendure proxy metrics",
            self._metrics.snapshot(),
            self._cfg.metrics_dashboard_refresh,
        )
        return aiohttp.web.Response(text=html, content_type="text/html")

    async def _publish_metrics_sensors(self, _kwargs=None) -> None:
        if not self._cfg.metrics_enabled or not self._cfg.metrics_ha_sensors_enabled:
            return

        for entity_id, (state, attributes) in self._metrics.flat_ha_sensors().items():
            sensor_attributes = {
                "friendly_name": entity_id.replace("sensor.zendure_proxy_", "Zendure proxy ")
                .replace("_", " ")
                .title(),
                **attributes,
            }
            await self._resolve_appdaemon_result(
                self.set_state(
                    entity_id,
                    state=self._ha_sensor_state(state),
                    attributes=sensor_attributes,
                    replace=True,
                    check_existence=False,
                )
            )

    async def _publish_proxy_ha_sensors(self, response: dict) -> None:
        if not self._cfg.proxy_ha_sensors_enabled:
            return

        try:
            battery_order = await self._resolve_appdaemon_result(
                self.get_state("input_text.zendure_2400_ac_batterij_volgorde")
            )
        except Exception:
            battery_order = None

        updated_at = str(int(time.time()))
        for entity_id, (state, attributes) in build_proxy_ha_sensors(
            response, battery_order
        ).items():
            existing_state = await self._get_entity_state(entity_id)
            owned = self._entity_is_proxy_managed(entity_id, existing_state)
            if (
                self._cfg.proxy_ha_sensors_skip_existing
                and entity_id not in self._proxy_ha_sensor_owned_entities
                and existing_state is not None
                and not owned
            ):
                continue
            if self._mqtt_api is not None:
                try:
                    await self._publish_proxy_mqtt_sensor(
                        entity_id, state, attributes, response, updated_at
                    )
                    self._proxy_ha_sensor_owned_entities.add(entity_id)
                    continue
                except Exception as exc:
                    if not self._mqtt_sensor_error_logged:
                        self._proxy_log(
                            "MQTT discovery publish failed for Zendure proxy sensors; "
                            f"using AppDaemon set_state fallback: {exc}",
                            level="WARNING",
                        )
                        self._mqtt_sensor_error_logged = True
            await self._resolve_appdaemon_result(
                self.set_state(
                    entity_id,
                    state=self._ha_sensor_state(state),
                    attributes={
                        **attributes,
                        "proxy_updated_at": updated_at,
                        "zendure_proxy_managed": True,
                    },
                    replace=True,
                    check_existence=False,
                )
            )
            self._proxy_ha_sensor_owned_entities.add(entity_id)

    async def _publish_proxy_mqtt_sensor(
        self,
        entity_id: str,
        state,
        attributes: dict[str, Any],
        response: dict,
        updated_at: str,
    ) -> None:
        if self._mqtt_api is None:
            return

        sensor_attributes = {
            **attributes,
            "proxy_updated_at": updated_at,
            "proxy_version": response.get("proxyVersion", PROXY_VERSION),
            "zendure_proxy_managed": True,
        }
        discovery_topic, state_topic, attrs_topic = mqtt_sensor_topics(
            entity_id,
            self._cfg.proxy_ha_sensors_mqtt_discovery_prefix,
            self._cfg.proxy_ha_sensors_mqtt_state_prefix,
        )
        config = mqtt_sensor_config(
            entity_id,
            sensor_attributes,
            self._cfg.proxy_ha_sensors_mqtt_discovery_prefix,
            self._cfg.proxy_ha_sensors_mqtt_state_prefix,
        )
        retain = self._cfg.proxy_ha_sensors_mqtt_retain
        await self._resolve_appdaemon_result(
            self._mqtt_api.mqtt_publish(
                discovery_topic,
                json.dumps(config, ensure_ascii=False),
                retain=retain,
            )
        )
        await self._resolve_appdaemon_result(
            self._mqtt_api.mqtt_publish(
                state_topic,
                self._ha_sensor_state(state),
                retain=retain,
            )
        )
        await self._resolve_appdaemon_result(
            self._mqtt_api.mqtt_publish(
                attrs_topic,
                json.dumps(sensor_attributes, ensure_ascii=False),
                retain=retain,
            )
        )

    async def _get_entity_state(self, entity_id: str):
        try:
            state = await self._resolve_appdaemon_result(
                self.get_state(entity_id, attribute="all")
            )
        except Exception:
            return None
        if state is None:
            return None
        return state if isinstance(state, dict) else {"state": state, "attributes": {}}

    def _entity_is_proxy_managed(self, entity_id: str, state: dict | None) -> bool:
        if entity_id in self._proxy_ha_sensor_owned_entities:
            return True
        if state is None:
            return False
        attributes = state.get("attributes", {})
        return attributes.get("zendure_proxy_managed") is True

    @staticmethod
    def _ha_sensor_state(value) -> str:
        if value is None:
            return "unknown"
        if isinstance(value, float) and not math.isfinite(value):
            return "unknown"
        return str(value)

    @staticmethod
    async def _resolve_appdaemon_result(value):
        if inspect.isawaitable(value):
            return await value
        return value

    async def _restore_metrics_counters_from_ha(self) -> None:
        if not self._cfg.metrics_enabled or not self._cfg.metrics_ha_sensors_enabled:
            return

        states = {}
        for entity_id in self._metrics.counter_sensor_entity_ids():
            try:
                states[entity_id] = await self._resolve_appdaemon_result(
                    self.get_state(entity_id)
                )
            except Exception as exc:
                self._proxy_log(
                    f"Could not restore metric counter {entity_id}: {exc}",
                    level="WARNING",
                )
        restored = self._metrics.restore_counters_from_sensors(states)
        if restored:
            self._proxy_log(f"Restored {restored} metric counters from Home Assistant")

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
        self._remember_local_proxy_url(request)
        data, status = await self._execute_report_request()
        return aiohttp.web.json_response(data, status=status)

    async def _handle_post(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        self._remember_local_proxy_url(request)
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

    def _remember_local_proxy_url(self, request: aiohttp.web.Request) -> None:
        host = request.headers.get("Host") if hasattr(request, "headers") else None
        path = getattr(request, "path", "")
        if not host or not path:
            return
        local_proxy_url = f"{host}{path}"
        for client in self._clients:
            setter = getattr(client, "set_local_proxy_url", None)
            if setter is not None:
                setter(local_proxy_url)

    # ── Request execution shared by aiohttp and AppDaemon API ──────────────────

    async def _execute_report_request(self) -> tuple[dict, int]:
        start = time.monotonic()
        self._metrics.start_incoming("GET")
        status = 500
        timeout = False
        self._state.counter_get_received += 1
        self._state.latest_get_ts = now()

        try:
            if not self._cfg.device_ips:
                status = 503
                return {"error": "No devices configured"}, status

            if not all(d.sn for d in self._state.devices):
                await self._ensure_serial_numbers()
            if not all(d.sn for d in self._state.devices):
                if self._state.last_get_response:
                    status = 200
                    await self._publish_proxy_ha_sensors(self._state.last_get_response)
                    return self._state.last_get_response, status
                status = 503
                return {"error": "Initializing - try again shortly"}, status

            fut = await self._queue.enqueue_get()
            await self._record_incoming_depths()
            try:
                data = await asyncio.wait_for(fut, timeout=30.0)
                self._state.counter_get_replies += 1
                status = 200
                await self._publish_proxy_ha_sensors(data)
                return data, status
            except asyncio.TimeoutError:
                timeout = True
                if self._state.last_get_response:
                    status = 200
                    await self._publish_proxy_ha_sensors(self._state.last_get_response)
                    return self._state.last_get_response, status
                status = 504
                return {"error": "Upstream timeout"}, status
            except Exception as exc:
                self._proxy_log(f"GET handler error: {exc}", level="ERROR")
                if self._state.last_get_response:
                    status = 200
                    await self._publish_proxy_ha_sensors(self._state.last_get_response)
                    return self._state.last_get_response, status
                status = 502
                return {"error": str(exc)}, status
        finally:
            self._metrics.finish_incoming(
                "GET", (time.monotonic() - start) * 1000.0, status, timeout
            )

    async def _execute_write_request(self, payload: dict) -> tuple[dict, int]:
        start = time.monotonic()
        self._metrics.start_incoming("POST")
        status = 500
        timeout = False
        self._state.counter_post_received += 1

        try:
            if not self._cfg.device_ips:
                status = 200
                return {"ack": "pong"}, status

            fut = await self._queue.enqueue_post(payload)
            await self._record_incoming_depths()
            try:
                data = await asyncio.wait_for(fut, timeout=30.0)
                self._state.counter_post_replies += 1
                status = 200
                return data, status
            except asyncio.TimeoutError:
                timeout = True
                status = 200
                return {"ack": "pong"}, status
            except Exception as exc:
                self._proxy_log(f"POST handler error: {exc}", level="ERROR")
                status = 200
                return {"ack": "pong"}, status
        finally:
            self._metrics.finish_incoming(
                "POST", (time.monotonic() - start) * 1000.0, status, timeout
            )

    async def _record_incoming_depths(self) -> None:
        get_depth, post_depth = await self._queue.depths()
        self._metrics.set_incoming_queue_depth(get_depth, post_depth)

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
                await self._record_incoming_depths()
                skipped_post_count = sum(len(skipped) for _, _, skipped in posts)
                deduplicated_groups = sum(1 for _, _, skipped in posts if skipped)
                self._metrics.record_queue_batch(
                    get_count=len(gets),
                    post_group_count=len(posts),
                    coalesced_gets=max(0, len(gets) - 1),
                    deduplicated_posts=skipped_post_count,
                    deduplicated_groups=deduplicated_groups,
                )
                if len(gets) > 1:
                    self._proxy_log(
                        "Queue cleanup: coalesced "
                        f"{len(gets)} queued GET requests into 1 upstream GET",
                        level="WARNING",
                    )
                if skipped_post_count:
                    self._proxy_log(
                        "Queue cleanup: deduplicated "
                        f"{skipped_post_count} queued POST requests",
                        level="WARNING",
                    )

                get_response: Optional[dict] = None

                if gets:
                    try:
                        get_response = await execute_get(
                            self._clients, self._state, self._cfg, self._proxy_log
                        )
                        for fut in gets:
                            if not fut.done():
                                fut.set_result(get_response)
                    except Exception as exc:
                        self._proxy_log(f"GET execution failed: {exc}", level="ERROR")
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
                            self._cfg, self._proxy_log,
                        )
                        if not latest_fut.done():
                            latest_fut.set_result(resp)
                    except Exception as exc:
                        self._proxy_log(f"POST execution failed: {exc}", level="ERROR")
                        if not latest_fut.done():
                            latest_fut.set_result({"ack": "pong"})

                # Manual-mode repeat: re-apply the last power command after each
                # GET cycle so SoC balancing continues without new HA POSTs.
                if (
                    gets
                    and get_response is not None
                    and not posts
                    and should_repeat_last_power(self._state, self._cfg, current_ts=now())
                ):
                    try:
                        await execute_post(
                            self._state.last_post_payload,
                            self._clients, self._state, self._cfg, self._proxy_log,
                            is_repeat=True,
                        )
                    except Exception as exc:
                        self._proxy_log(f"Manual-repeat failed: {exc}", level="WARNING")

            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._proxy_log(f"Queue processor error: {exc}", level="ERROR")
                await asyncio.sleep(1)

    # ── Startup helper ─────────────────────────────────────────────────────────

    async def _init_serial_numbers(self) -> None:
        """Fetch and store each device's serial number at startup."""
        await self._ensure_serial_numbers()

    async def _ensure_serial_numbers(self) -> bool:
        """Retry missing serial numbers and return True when all serials exist."""
        for i, client in enumerate(self._clients):
            if i < len(self._state.devices) and self._state.devices[i].sn:
                continue
            try:
                data = await client.get()
                if data and "sn" in data:
                    self._state.devices[i].sn = data["sn"]
                    self._proxy_log(f"Device {i+1} SN: {data['sn']}")
                else:
                    self._proxy_log(
                        f"Device {i+1}: could not fetch serial number",
                        level="WARNING",
                    )
            except Exception as exc:
                self._proxy_log(
                    f"Device {i+1} SN init error: {exc}", level="WARNING"
                )
        return all(d.sn for d in self._state.devices)


def should_repeat_last_power(
    state: ProxyState,
    cfg: Config,
    *,
    current_ts: float | None = None,
) -> bool:
    """Return True when Node-RED manual power-repeat conditions are satisfied."""
    ts = now() if current_ts is None else current_ts
    if not cfg.manual_mode_repeat:
        return False
    if state.device_count < 2:
        return False
    if state.last_post_payload is None:
        return False
    payload_power_cmd = _repeat_payload_power_cmd(
        state.last_post_payload.get("properties") or {}, state.ac_mode
    )
    if payload_power_cmd == 0:
        return False
    if state.latest_power_message_ts <= 0 or ts - state.latest_power_message_ts < 30:
        return False
    if state.latest_get_ts <= 0 or ts - state.latest_get_ts > 10:
        return False
    if state.latest_power_cmd == 0:
        return False
    if state.latest_power_cmd > 0 and all(d.soc_limit == 1 for d in state.devices):
        return False
    if state.latest_power_cmd < 0 and all(d.soc_limit == 2 for d in state.devices):
        return False
    return True


def _repeat_payload_power_cmd(props: dict, current_ac_mode: int) -> int:
    input_limit = _int(props.get("inputLimit", 0))
    output_limit = _int(props.get("outputLimit", 0))
    if "acMode" in props:
        ac_mode = _int(props["acMode"])
    elif input_limit > 0 and output_limit <= 0:
        ac_mode = 1
    elif output_limit > 0 and input_limit <= 0:
        ac_mode = 2
    else:
        ac_mode = current_ac_mode

    if ac_mode == 1:
        return max(0, input_limit)
    if ac_mode == 2:
        return -max(0, output_limit)
    return 0


def _int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default
