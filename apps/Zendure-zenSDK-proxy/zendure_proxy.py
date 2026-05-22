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
import html
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
from zendure_proxy_anti_pingpong import (
    activation_mode,
    reserve_discharge_capacity_watts,
    smart_evaluate_window,
    smart_sample_grid_power,
)
from zendure_proxy_health import (
    cache_is_usable,
    eligible_device_indices,
    response_with_proxy_health,
)
from zendure_proxy_logging import ProxyFileLogger, render_log_dashboard
from zendure_proxy_metrics import MetricsRegistry, render_metrics_dashboard
from zendure_proxy_mqtt_discovery import mqtt_sensor_config, mqtt_sensor_topics
from zendure_proxy_post_handler import execute_post
from zendure_proxy_power import PROXY_VERSION, now
from zendure_proxy_queue import RequestQueue
from zendure_proxy_state import DeviceState, ProxyState
from zendure_proxy_standby import manage_standby


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
        self._proxy_ha_health_states: dict[int, str] | None = None
        self._proxy_ha_sensor_refresh_in_progress = False
        await self._restore_metrics_counters_from_ha()

        self._clients: list[DeviceClient] = [
            DeviceClient(
                ip,
                self._proxy_log,
                self._metrics,
                idx,
                request_timeout=self._cfg.zendure_request_timeout,
                separate_get_post_connections=(
                    self._cfg.separate_get_post_connections
                ),
                idle_connection_close_seconds=(
                    self._cfg.idle_connection_close_seconds
                ),
            )
            for idx, ip in enumerate(self._cfg.device_ips)
        ]
        self._state = ProxyState(
            device_count=len(self._cfg.device_ips),
            devices=[
                DeviceState(
                    ip=ip,
                    configured_charge_max_watts=(
                        self._cfg.device_power_limits[idx].charge_max_watts
                        if idx < len(self._cfg.device_power_limits)
                        else None
                    ),
                    configured_discharge_max_watts=(
                        self._cfg.device_power_limits[idx].discharge_max_watts
                        if idx < len(self._cfg.device_power_limits)
                        else None
                    ),
                )
                for idx, ip in enumerate(self._cfg.device_ips)
            ],
            equal_mode=self._cfg.equal_mode,
            always_dual_mode=self._cfg.always_dual_mode,
            dualmode_damper_enabled=self._cfg.damper_enable,
            startup_ts=now(),
        )
        self._queue = RequestQueue()

        self._processor_task = asyncio.ensure_future(self._processor())
        if not self._cfg.proxy_ha_sensors_enabled:
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
        if self._cfg.diagnostics_dashboard_enabled:
            self._diagnostics_route_handle = await self.register_route(
                self._diagnostics_dashboard, self._cfg.diagnostics_dashboard_route
            )
        if self._cfg.metrics_enabled and self._cfg.metrics_ha_sensors_enabled:
            self._metrics_sensor_timer = await self.run_every(
                self._publish_metrics_sensors,
                "now",
                self._cfg.metrics_ha_sensors_interval,
            )
        if self._cfg.proxy_ha_sensors_enabled and self._cfg.device_ips:
            self._proxy_ha_sensor_refresh_timer = await self.run_every(
                self._refresh_proxy_ha_sensors,
                "now",
                300,
            )
        if len(self._cfg.device_ips) > 1:
            self._standby_check_timer = await self.run_every(
                self._standby_check,
                "now",
                10,
            )
        if (
            self._cfg.anti_pingpong_enable
            and activation_mode(self._cfg) == "smart"
        ):
            await self._resolve_anti_pingpong_grid_power_entity()
            self._anti_pingpong_sample_timer = await self.run_every(
                self._anti_pingpong_sample_grid_power,
                "now",
                self._cfg.anti_pingpong_smart_sample_interval_seconds,
            )
            self._anti_pingpong_eval_timer = await self.run_every(
                self._anti_pingpong_evaluate_smart,
                "now",
                self._cfg.anti_pingpong_smart_evaluate_interval_seconds,
            )
        await self._start_server()

        if not self._cfg.device_ips:
            self._proxy_log(
                "WARNING: no Zendure IPs configured - proxy will not function",
                level="WARNING",
            )
        else:
            for warning in self._cfg.config_warnings:
                self._proxy_log(f"Configuration warning: {warning}", level="WARNING")
            self._proxy_log(
                f"Zendure proxy v{PROXY_VERSION} started | "
                f"port={self._cfg.server_port} | "
                f"api_endpoints=['zendure_proxy_report', 'zendure_proxy_write'] | "
                f"log_dashboard=/app/{self._cfg.log_dashboard_route} | "
                f"metrics_dashboard=/app/{self._cfg.metrics_dashboard_route} | "
                f"devices={self._cfg.device_ips}"
            )

    async def terminate(self) -> None:
        if getattr(self, "_anti_pingpong_eval_timer", None):
            await self.cancel_timer(self._anti_pingpong_eval_timer, silent=True)
        if getattr(self, "_anti_pingpong_sample_timer", None):
            await self.cancel_timer(self._anti_pingpong_sample_timer, silent=True)
        if getattr(self, "_standby_check_timer", None):
            await self.cancel_timer(self._standby_check_timer, silent=True)
        if getattr(self, "_metrics_sensor_timer", None):
            await self.cancel_timer(self._metrics_sensor_timer, silent=True)
        if getattr(self, "_proxy_ha_sensor_refresh_timer", None):
            await self.cancel_timer(
                self._proxy_ha_sensor_refresh_timer,
                silent=True,
            )
        if getattr(self, "_diagnostics_route_handle", None):
            await self.deregister_route(self._diagnostics_route_handle)
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
        for device in getattr(self, "_state", ProxyState()).devices:
            if device.standby_task and not device.standby_task.done():
                device.standby_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await device.standby_task
                device.standby_task = None
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

    def _debug_capture_payload(
        self,
        message_type: str,
        direction: str,
        payload: dict,
    ) -> None:
        if not self._cfg.debug_payload_capture_enabled:
            return
        captured = {
            "debug_message_type": message_type,
            "debug_direction": direction,
            "debug_timestamp": int(time.time()),
            "debug_payload": payload,
        }
        line = json.dumps(captured, ensure_ascii=False, sort_keys=True)
        if self._file_logger is not None:
            self._file_logger.log(line, "DEBUG")
        else:
            self.log(line, level="DEBUG")

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

    async def _resolve_anti_pingpong_grid_power_entity(self) -> str:
        configured = self._cfg.anti_pingpong_grid_power_entity
        if configured:
            self._state.anti_pingpong_grid_power_entity_resolved = configured
            self._state.anti_pingpong_grid_power_entity_source = "config"
            return configured

        if not self._cfg.anti_pingpong_grid_power_autodiscover:
            return ""

        try:
            raw_entity = await self._resolve_appdaemon_result(
                self.get_state("input_text.afwijkende_p1_sensor")
            )
        except Exception:
            raw_entity = None
        candidate = str(raw_entity or "").strip()
        if self._valid_ha_entity_id(candidate) and await self._ha_entity_exists(candidate):
            self._state.anti_pingpong_grid_power_entity_resolved = candidate
            self._state.anti_pingpong_grid_power_entity_source = (
                "input_text.afwijkende_p1_sensor"
            )
            return candidate

        homewizard = "sensor.homewizard_p1_vermogen"
        if await self._ha_entity_exists(homewizard):
            self._state.anti_pingpong_grid_power_entity_resolved = homewizard
            self._state.anti_pingpong_grid_power_entity_source = homewizard
            return homewizard

        if not getattr(self, "_anti_pingpong_grid_warning_logged", False):
            self._proxy_log(
                "Reserve mode smart calculation could not find a P1 power entity; "
                "set anti_pingpong_grid_power_entity or configure "
                "input_text.afwijkende_p1_sensor / sensor.homewizard_p1_vermogen",
                level="WARNING",
            )
            self._anti_pingpong_grid_warning_logged = True
        return ""

    async def _ha_entity_exists(self, entity_id: str) -> bool:
        if not self._valid_ha_entity_id(entity_id):
            return False
        try:
            value = await self._resolve_appdaemon_result(self.get_state(entity_id))
        except Exception:
            return False
        return value is not None and value not in ("unknown", "unavailable", "")

    @staticmethod
    def _valid_ha_entity_id(value: str) -> bool:
        if value in ("", "unknown", "unavailable", "none"):
            return False
        return "." in value and " " not in value

    async def _anti_pingpong_sample_grid_power(self, _kwargs=None) -> None:
        if not self._cfg.anti_pingpong_enable or activation_mode(self._cfg) != "smart":
            return
        entity_id = self._state.anti_pingpong_grid_power_entity_resolved
        if not entity_id:
            entity_id = await self._resolve_anti_pingpong_grid_power_entity()
        if not entity_id:
            return
        try:
            raw_value = await self._resolve_appdaemon_result(self.get_state(entity_id))
            grid_power = float(raw_value)
        except Exception as exc:
            self._proxy_log(
                f"Reserve mode smart sample failed for {entity_id}: {exc}",
                level="WARNING",
            )
            return
        if not self._cfg.anti_pingpong_grid_power_import_positive:
            grid_power *= -1
        smart_sample_grid_power(self._state, self._cfg, grid_power, now())

    async def _anti_pingpong_evaluate_smart(self, _kwargs=None) -> None:
        if not self._cfg.anti_pingpong_enable or activation_mode(self._cfg) != "smart":
            return
        eligible = eligible_device_indices(self._state, self._cfg)
        reserve_capacity = reserve_discharge_capacity_watts(
            self._state, self._cfg, eligible
        )
        smart_evaluate_window(self._state, self._cfg, reserve_capacity, now())

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

    async def _diagnostics_dashboard(
        self, request: aiohttp.web.Request, _kwargs
    ) -> aiohttp.web.Response:
        if request.query.get("reset") == "1":
            self._reset_node_red_counters()
        self._log_diagnostics_warnings()
        page = self._render_diagnostics_dashboard()
        return aiohttp.web.Response(text=page, content_type="text/html")

    def _render_diagnostics_dashboard(self) -> str:
        snapshot = self._diagnostics_snapshot()
        warnings = self._diagnostics_warnings()
        warning_rows = "".join(
            f"<tr><td>{html.escape(text)}</td></tr>" for text in warnings
        ) or "<tr><td>No active warnings</td></tr>"
        counter_rows = "".join(
            f"<tr><td>{html.escape(name)}</td><td>{value}</td></tr>"
            for name, value in snapshot["counters"].items()
        )
        device_rows = "".join(
            "<tr>"
            f"<td>{device['idx']}</td>"
            f"<td>{html.escape(device['sn'])}</td>"
            f"<td>{html.escape(device['ip'])}</td>"
            f"<td>{device['electric_level']}</td>"
            f"<td>{device['soc_limit']}</td>"
            f"<td>{device['latest_power_cmd']}</td>"
            f"<td>{device['charge_max_limit']}</td>"
            f"<td>{device['inverse_max_power']}</td>"
            f"<td>{device['effective_charge_max_watts']}</td>"
            f"<td>{device['effective_discharge_max_watts']}</td>"
            f"<td>{device['missing_replies']}</td>"
            "</tr>"
            for device in snapshot["devices"]
        )
        reset_href = f"/app/{html.escape(self._cfg.diagnostics_dashboard_route)}?reset=1"
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Zendure proxy diagnostics</title>
  <style>
    body {{ margin: 0; font-family: system-ui, sans-serif; background: #111827; color: #e5e7eb; }}
    header {{ padding: 16px 20px; border-bottom: 1px solid #374151; background: #0f172a; }}
    h1 {{ margin: 0; font-size: 18px; }}
    main {{ padding: 16px 20px; display: grid; gap: 16px; }}
    section {{ border: 1px solid #374151; border-radius: 8px; overflow: hidden; background: #020617; }}
    h2 {{ margin: 0; padding: 12px 14px; font-size: 15px; background: #1f2937; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ padding: 9px 12px; border-top: 1px solid #1f2937; text-align: right; }}
    th:first-child, td:first-child {{ text-align: left; }}
    th {{ color: #93c5fd; font-weight: 650; }}
    a {{ color: #bfdbfe; }}
  </style>
</head>
<body>
  <header><h1>Zendure proxy diagnostics</h1></header>
  <main>
    <section>
      <h2>Proxy Info</h2>
      <table>
        <tr><th>Field</th><th>Value</th></tr>
        <tr><td>Version</td><td>{html.escape(PROXY_VERSION)}</td></tr>
        <tr><td>Device count</td><td>{snapshot['proxy']['device_count']}</td></tr>
        <tr><td>Active indices</td><td>{html.escape(str(snapshot['proxy']['devices_active_idx']))}</td></tr>
        <tr><td>Active count</td><td>{snapshot['proxy']['device_active_count']}</td></tr>
        <tr><td>latestPowerCmd</td><td>{snapshot['proxy']['latest_power_cmd']}</td></tr>
      </table>
    </section>
    <section>
      <h2>Node-RED Counters</h2>
      <table><tr><th>Counter</th><th>Value</th></tr>{counter_rows}</table>
      <p style="padding:0 12px 12px;margin:0;"><a href="{reset_href}">Reset counters to zero</a></p>
    </section>
    <section>
      <h2>Active Warnings</h2>
      <table><tr><th>Warning</th></tr>{warning_rows}</table>
    </section>
    <section>
      <h2>Devices</h2>
      <table>
        <tr><th>Device</th><th>SN</th><th>IP</th><th>SoC</th><th>socLimit</th><th>latestPowerCmd</th><th>chargeMaxLimit</th><th>inverseMaxPower</th><th>effectiveChargeMax</th><th>effectiveInverseMaxPower</th><th>Missing GET</th></tr>
        {device_rows}
      </table>
    </section>
  </main>
</body>
</html>"""

    def _diagnostics_snapshot(self) -> dict[str, Any]:
        state = self._state
        return {
            "proxy": {
                "device_count": state.device_count,
                "devices_active_idx": state.devices_active_idx,
                "device_active_count": state.device_active_count,
                "latest_power_cmd": state.latest_power_cmd,
            },
            "counters": {
                "GET received": state.counter_get_received,
                "GET replies sent": state.counter_get_replies,
                "GET timeout": state.counter_get_timeouts,
                "Config drop": state.counter_config_drop,
                "Serial missing drop": state.counter_serial_missing_drop,
                "POST received": state.counter_post_received,
            },
            "devices": [
                {
                    "idx": idx + 1,
                    "sn": device.sn,
                    "ip": device.ip,
                    "electric_level": device.electric_level,
                    "soc_limit": device.soc_limit,
                    "latest_power_cmd": device.latest_power_cmd,
                    "charge_max_limit": device.charge_max_limit,
                    "inverse_max_power": device.inverse_max_power,
                    "effective_charge_max_watts": device.effective_charge_max_watts,
                    "effective_discharge_max_watts": (
                        device.effective_discharge_max_watts
                    ),
                    "configured_charge_max_watts": (
                        device.configured_charge_max_watts
                    ),
                    "configured_discharge_max_watts": (
                        device.configured_discharge_max_watts
                    ),
                    "missing_replies": (
                        state.counter_missing[idx]
                        if idx < len(state.counter_missing)
                        else 0
                    ),
                }
                for idx, device in enumerate(state.devices)
            ],
        }

    def _diagnostics_warnings(self) -> list[str]:
        state = self._state
        warnings: list[str] = []
        devices = state.devices
        for attr, label in (
            ("charge_max_limit", "chargeMaxLimit"),
            ("inverse_max_power", "inverseMaxPower"),
        ):
            values = [getattr(device, attr) for device in devices]
            if values and len(set(values)) > 1:
                warnings.append(f"{label} differs between devices: {values}")

        min_soc_values = []
        soc_set_values = []
        for device in devices:
            response = device.last_response or {}
            props = response.get("properties", {})
            if "minSoc" in props:
                min_soc_values.append(props["minSoc"])
            if "socSet" in props:
                soc_set_values.append(props["socSet"])
        if min_soc_values and len(set(min_soc_values)) > 1:
            warnings.append(f"minSoc differs between devices: {min_soc_values}")
        if soc_set_values and len(set(soc_set_values)) > 1:
            warnings.append(f"socSet differs between devices: {soc_set_values}")

        if state.latest_get_ts <= 0 or now() - state.latest_get_ts > 10:
            warnings.append("No recent GET within 10 seconds")
        return warnings

    def _log_diagnostics_warnings(self) -> None:
        logged = getattr(self, "_diagnostics_logged_warnings", set())
        for warning in self._diagnostics_warnings():
            if warning in logged:
                continue
            self._proxy_log(f"Diagnostics warning: {warning}", level="WARNING")
            logged.add(warning)
        self._diagnostics_logged_warnings = logged

    def _reset_node_red_counters(self) -> None:
        self._state.counter_get_received = 0
        self._state.counter_get_replies = 0
        self._state.counter_get_timeouts = 0
        self._state.counter_config_drop = 0
        self._state.counter_serial_missing_drop = 0
        self._state.counter_post_received = 0
        self._state.counter_post_replies = 0
        self._state.counter_missing = [0] * self._state.device_count

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
            try:
                await self._resolve_appdaemon_result(
                    self.set_state(
                        entity_id,
                        state=self._ha_sensor_state(state),
                        attributes=sensor_attributes,
                        replace=True,
                        check_existence=False,
                    )
                )
            except Exception as exc:
                self._proxy_log(
                    "Metrics sensor publish failed: "
                    f"entity_id={entity_id} error={exc}",
                    level="WARNING",
                )

    async def _publish_report_sensors(
        self,
        response: dict,
        *,
        force_health_sensor_refresh: bool = False,
    ) -> None:
        await self._publish_health_transition_sensors(
            response,
            force_all=force_health_sensor_refresh,
        )
        await self._publish_proxy_ha_sensors(response)

    async def _refresh_proxy_ha_sensors(self, _kwargs=None) -> None:
        if not self._cfg.proxy_ha_sensors_enabled or not self._cfg.device_ips:
            return
        if self._proxy_ha_sensor_refresh_in_progress:
            return

        self._proxy_ha_sensor_refresh_in_progress = True
        self._state.get_refresh_in_progress = True
        self._state.last_upstream_get_ts = now()
        try:
            response = await execute_get(
                self._clients,
                self._state,
                self._cfg,
                self._proxy_log,
                metrics=self._metrics,
            )
            self._mark_passive_zero_timestamps()
            await self._standby_check()
            await self._publish_report_sensors(
                response,
                force_health_sensor_refresh=True,
            )
        except Exception as exc:
            self._proxy_log(
                f"Proxy sensor refresh failed: error={exc}",
                level="WARNING",
            )
        finally:
            self._state.get_refresh_in_progress = False
            self._proxy_ha_sensor_refresh_in_progress = False

    async def _publish_proxy_ha_sensors(
        self,
        response: dict,
        *,
        entity_ids: set[str] | None = None,
        force_existing_entities: bool = False,
    ) -> None:
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
            if entity_ids is not None and entity_id not in entity_ids:
                continue
            try:
                existing_state = await self._get_entity_state(entity_id)
                owned = self._entity_is_proxy_managed(entity_id, existing_state)
                if (
                    self._cfg.proxy_ha_sensors_skip_existing
                    and not force_existing_entities
                    and entity_id not in self._proxy_ha_sensor_owned_entities
                    and existing_state is not None
                    and not owned
                ):
                    continue
                transient_existing_update = (
                    force_existing_entities
                    and existing_state is not None
                    and not owned
                )
                if self._mqtt_api is not None and not transient_existing_update:
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
                            **(
                                {}
                                if transient_existing_update
                                else {"zendure_proxy_managed": True}
                            ),
                        },
                        replace=not transient_existing_update,
                        check_existence=False,
                    )
                )
                if not transient_existing_update:
                    self._proxy_ha_sensor_owned_entities.add(entity_id)
            except Exception as exc:
                self._proxy_log(
                    "Proxy sensor publish failed: "
                    f"entity_id={entity_id} error={exc}",
                    level="WARNING",
                )

    async def _publish_health_transition_sensors(
        self,
        response: dict,
        *,
        force_all: bool = False,
    ) -> None:
        current_states = _health_transition_states(response)
        previous_states = getattr(self, "_proxy_ha_health_states", None)
        self._proxy_ha_health_states = current_states

        if previous_states is None:
            changed_slots = frozenset(current_states)
        else:
            changed_slots = _changed_health_slots(previous_states, current_states)
        refresh_slots = frozenset(current_states) if force_all else changed_slots
        if not refresh_slots:
            return

        if changed_slots:
            self._log_health_transitions(
                response,
                changed_slots,
                current_states,
                previous_states,
            )

        entity_ids = _health_transition_entity_ids(refresh_slots)
        await self._publish_proxy_ha_sensors(
            response,
            entity_ids=entity_ids,
            force_existing_entities=True,
        )

    def _log_health_transitions(
        self,
        response: dict,
        changed_slots: frozenset[int],
        current_states: dict[int, str],
        previous_states: dict[int, str] | None,
    ) -> None:
        for slot in sorted(changed_slots):
            previous_state = (
                previous_states.get(slot, "Healthy")
                if previous_states is not None
                else "Healthy"
            )
            current_state = current_states.get(slot, "Healthy")
            if previous_state == current_state:
                continue

            item = _health_item_for_slot(response, slot)
            serial = item.get("serialNumber") or f"slot-{slot}"
            ip_address = item.get("ipAddress") or "unknown"
            last_error = item.get("lastGetError") or "unknown"
            age = item.get("lastSuccessfulGetAgeSeconds")
            if current_state == "Healthy":
                self._proxy_log(
                    "Zendure pool recovered: "
                    f"slot={slot} serial={serial} ip={ip_address} "
                    f"previous_health_state={previous_state}",
                    level="INFO",
                )
                continue

            level = "WARNING"
            label = "dead" if current_state == "Dead" else "degraded"
            self._proxy_log(
                f"Zendure pool {label}: "
                f"slot={slot} serial={serial} ip={ip_address} "
                f"previous_health_state={previous_state} "
                f"last_successful_get_age_seconds={age} "
                f"last_get_error={last_error}",
                level=level,
            )

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
        request_ts = now()

        try:
            if not self._cfg.device_ips:
                status = 503
                self._state.counter_config_drop += 1
                return {"error": "No devices configured"}, status

            self._state.last_ha_get_ts = request_ts
            if (
                self._state.last_upstream_get_ts > 0
                and request_ts - self._state.last_upstream_get_ts
                <= self._cfg.get_rate_limit_window
                and cache_is_usable(self._state, self._cfg, current_ts=request_ts)
            ):
                self._metrics.record_incoming_get_rate_limited_cache()
                status = 200
                data = response_with_proxy_health(
                    self._state.last_get_response or {},
                    self._state,
                    self._cfg,
                    served_from_cache=True,
                    reason="rate_limited",
                    refresh_in_progress=self._state.get_refresh_in_progress,
                    current_ts=request_ts,
                )
                await self._publish_report_sensors(data)
                self._debug_capture_payload("GET", "To Home Assistant", data)
                return data, status

            fut = await self._queue.enqueue_get()
            await self._record_incoming_depths()
            try:
                data = await asyncio.wait_for(
                    fut, timeout=self._cfg.ha_get_response_timeout
                )
                self._state.counter_get_replies += 1
                status = 200
                self._mark_passive_zero_timestamps()
                await self._standby_check()
                await self._publish_report_sensors(data)
                self._debug_capture_payload("GET", "To Home Assistant", data)
                return data, status
            except asyncio.TimeoutError:
                timeout = True
                self._state.counter_get_timeouts += 1
                if cache_is_usable(self._state, self._cfg, current_ts=now()):
                    status = 200
                    data = response_with_proxy_health(
                        self._state.last_get_response or {},
                        self._state,
                        self._cfg,
                        served_from_cache=True,
                        reason="ha_get_timeout",
                        refresh_in_progress=True,
                        current_ts=now(),
                    )
                    await self._publish_report_sensors(data)
                    self._debug_capture_payload("GET", "To Home Assistant", data)
                    return data, status
                status = 504
                return {"error": "Cached GET response expired"}, status
            except Exception as exc:
                self._proxy_log(f"GET handler error: {exc}", level="ERROR")
                if cache_is_usable(self._state, self._cfg, current_ts=now()):
                    status = 200
                    data = response_with_proxy_health(
                        self._state.last_get_response or {},
                        self._state,
                        self._cfg,
                        served_from_cache=True,
                        reason="upstream_partial",
                        refresh_in_progress=self._state.get_refresh_in_progress,
                        current_ts=now(),
                    )
                    await self._publish_report_sensors(data)
                    return data, status
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
        self._debug_capture_payload("POST", "From Home Assistant", payload)

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
                self._debug_capture_payload("POST", "To Home Assistant", data)
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

    def _mark_passive_zero_timestamps(self) -> None:
        stamp = now()
        active = set(self._state.devices_active_idx)
        for idx, device in enumerate(self._state.devices):
            if (
                idx not in active
                and device.latest_power_cmd == 0
                and device.latest_power_cmd_zero_ts <= 0
            ):
                device.latest_power_cmd_zero_ts = stamp

    async def _standby_check(self, _kwargs=None) -> None:
        if self._state.device_count < 2:
            return
        await manage_standby(
            self._state,
            self._clients,
            self._state.ac_mode,
            [0] * self._state.device_count,
            self._cfg,
            self._proxy_log,
        )

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
                    gets = [fut for fut in gets if not fut.done()]
                    try:
                        self._state.get_refresh_in_progress = True
                        self._state.last_upstream_get_ts = now()
                        get_response = await execute_get(
                            self._clients,
                            self._state,
                            self._cfg,
                            self._proxy_log,
                            metrics=self._metrics,
                        )
                        self._state.get_refresh_in_progress = False
                        self._mark_passive_zero_timestamps()
                        await self._standby_check()
                        await self._publish_report_sensors(get_response)
                        for fut in gets:
                            if not fut.done():
                                fut.set_result(get_response)
                        pending_gets = await self._queue.drain_gets_nowait()
                        for fut in pending_gets:
                            if not fut.done():
                                fut.set_result(get_response)
                    except Exception as exc:
                        self._state.get_refresh_in_progress = False
                        self._proxy_log(f"GET execution failed: {exc}", level="ERROR")
                        cached = self._state.last_get_response
                        for fut in gets:
                            if not fut.done():
                                if cached:
                                    fut.set_result(
                                        response_with_proxy_health(
                                            cached,
                                            self._state,
                                            self._cfg,
                                            served_from_cache=True,
                                            reason="upstream_partial",
                                            refresh_in_progress=False,
                                            current_ts=now(),
                                        )
                                    )
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


def _health_transition_states(response: dict) -> dict[int, str]:
    health = response.get("proxyHealth") or {}
    configured_count = _int(health.get("configuredCount", 3), 3)
    degraded_slots = _health_slots(response, "degradedDevices")
    unhealthy_slots = _health_slots(response, "unhealthyDevices")
    dead_slots = _health_slots(response, "deadDevices")
    states: dict[int, str] = {}
    for slot in range(1, max(0, configured_count) + 1):
        if slot in dead_slots:
            states[slot] = "Dead"
        elif slot in degraded_slots or slot in unhealthy_slots:
            states[slot] = "Degraded"
        else:
            states[slot] = "Healthy"
    return states


def _health_slots(response: dict, key: str) -> frozenset[int]:
    health = response.get("proxyHealth") or {}
    slots: set[int] = set()
    for item in health.get(key, []) or []:
        if not isinstance(item, dict):
            continue
        slot = _int(item.get("slot", 0))
        if 1 <= slot <= 10:
            slots.add(slot)
    return frozenset(slots)


def _changed_health_slots(
    previous_states: dict[int, str],
    current_states: dict[int, str],
) -> frozenset[int]:
    slots = set(previous_states) | set(current_states)
    return frozenset(
        slot
        for slot in slots
        if previous_states.get(slot, "Healthy") != current_states.get(slot, "Healthy")
    )


def _health_item_for_slot(response: dict, slot: int) -> dict:
    health = response.get("proxyHealth") or {}
    for key in (
        "deadDevices",
        "degradedDevices",
        "unhealthyDevices",
        "excludedDevices",
        "recoveringDevices",
    ):
        for item in health.get(key, []) or []:
            if not isinstance(item, dict):
                continue
            if _int(item.get("slot", 0)) == slot:
                return item

    props = response.get("properties") or {}
    return {
        "slot": slot,
        "serialNumber": props.get(f"sn_{slot}") or response.get(f"sn_{slot}", ""),
        "ipAddress": props.get(f"ipAddress_{slot}", "unknown"),
    }


def _health_transition_entity_ids(slots: frozenset[int]) -> set[str]:
    entity_ids = {
        "sensor.proxy_zendure_pool_healthy",
        "sensor.vermogensopdracht",
        "sensor.zendure_actief_device",
    }
    slot_suffixes = (
        "health",
        "laadpercentage",
        "vermogen_aansturing",
        "modus",
        "relais_stand",
        "kalibratie_bezig",
        "opslagmodus",
        "soc_limiet_status",
        "omvormer_temperatuur",
        "offgrid_modus",
        "serienummer",
        "ip_adres",
    )
    for slot in slots:
        entity_ids.add(f"sensor.vermogensopdracht_zendure_{slot}")
        for suffix in slot_suffixes:
            entity_ids.add(f"sensor.zendure_{slot}_{suffix}")
    return entity_ids


def should_repeat_last_power(
    state: ProxyState,
    cfg: Config,
    *,
    current_ts: float | None = None,
) -> bool:
    """Return True when Node-RED manual power-repeat conditions are satisfied."""
    ts = now() if current_ts is None else current_ts
    eligible = eligible_device_indices(state, cfg, current_ts=ts)
    if not cfg.manual_mode_repeat:
        return False
    if len(eligible) < 2:
        return False
    if state.last_post_payload is None:
        return False
    props = state.last_post_payload.get("properties") or {}
    if _repeat_payload_has_explicit_zero_power(props):
        return False
    payload_power_cmd = _repeat_payload_power_cmd(props, state.ac_mode)
    if payload_power_cmd == 0:
        return False
    if state.latest_power_message_ts <= 0 or ts - state.latest_power_message_ts < 30:
        return False
    if state.latest_power_repeat_ts > 0 and ts - state.latest_power_repeat_ts < 20:
        return False
    if state.latest_get_ts <= 0 or ts - state.latest_get_ts > 10:
        return False
    if state.latest_power_cmd == 0:
        return False
    if state.latest_power_cmd > 0 and all(state.devices[i].soc_limit == 1 for i in eligible):
        return False
    if state.latest_power_cmd < 0 and all(state.devices[i].soc_limit == 2 for i in eligible):
        return False
    return True


def _repeat_payload_has_explicit_zero_power(props: dict) -> bool:
    return any(
        key in props and _int(props.get(key)) == 0
        for key in ("inputLimit", "outputLimit")
    )


def _repeat_payload_power_cmd(props: dict, current_ac_mode: int) -> int:
    input_limit = _int(props.get("inputLimit", 0))
    output_limit = _int(props.get("outputLimit", 0))
    if "acMode" in props:
        ac_mode = _int(props["acMode"])
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
