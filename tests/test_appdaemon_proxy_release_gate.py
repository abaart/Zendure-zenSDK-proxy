from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "apps" / "Zendure-zenSDK-proxy"
sys.path.insert(0, str(APP_DIR))


def _install_fake_appdaemon() -> None:
    appdaemon = types.ModuleType("appdaemon")
    plugins = types.ModuleType("appdaemon.plugins")
    hass = types.ModuleType("appdaemon.plugins.hass")
    hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")

    class Hass:
        pass

    hassapi.Hass = Hass
    sys.modules.setdefault("appdaemon", appdaemon)
    sys.modules.setdefault("appdaemon.plugins", plugins)
    sys.modules.setdefault("appdaemon.plugins.hass", hass)
    sys.modules.setdefault("appdaemon.plugins.hass.hassapi", hassapi)


def _install_fake_aiohttp() -> None:
    aiohttp = types.ModuleType("aiohttp")
    web = types.ModuleType("aiohttp.web")

    class Request:
        pass

    class Response:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class Application:
        def __init__(self):
            self.router = types.SimpleNamespace(add_get=lambda *args, **kwargs: None,
                                                add_post=lambda *args, **kwargs: None)

    class AppRunner:
        def __init__(self, app):
            self.app = app

        async def setup(self):
            return None

        async def cleanup(self):
            return None

    class TCPSite:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        async def start(self):
            return None

    def json_response(data, status=200):
        return Response(data=data, status=status)

    web.Request = Request
    web.Response = Response
    web.Application = Application
    web.AppRunner = AppRunner
    web.TCPSite = TCPSite
    web.json_response = json_response
    aiohttp.web = web
    sys.modules.setdefault("aiohttp", aiohttp)
    sys.modules.setdefault("aiohttp.web", web)


_install_fake_appdaemon()
_install_fake_aiohttp()

from zendure_proxy import ZendureProxy  # noqa: E402
from zendure_proxy_config import Config  # noqa: E402
from zendure_proxy_get_handler import build_combined_response, execute_get  # noqa: E402
from zendure_proxy_ha_sensors import build_proxy_ha_sensors  # noqa: E402
from zendure_proxy_health import (  # noqa: E402
    degraded_power_by_index,
    eligible_device_indices,
    health_summary,
    record_get_results,
    response_with_proxy_health,
)
from zendure_proxy_metrics import MetricsRegistry, render_metrics_dashboard  # noqa: E402
from zendure_proxy_mqtt_discovery import mqtt_sensor_config  # noqa: E402
from zendure_proxy_post_handler import execute_post  # noqa: E402
from zendure_proxy_power import now  # noqa: E402
from zendure_proxy_state import DeviceState, ProxyState  # noqa: E402


class AppDaemonAsyncBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_appdaemon_result_accepts_task_and_direct_value(self) -> None:
        task = asyncio.create_task(asyncio.sleep(0, result="1;2;3"))

        self.assertEqual(
            await ZendureProxy._resolve_appdaemon_result(task),
            "1;2;3",
        )
        self.assertEqual(
            await ZendureProxy._resolve_appdaemon_result("direct-value"),
            "direct-value",
        )

    async def test_publish_proxy_ha_sensors_accepts_async_get_state(self) -> None:
        proxy = ZendureProxy.__new__(ZendureProxy)
        proxy._cfg = types.SimpleNamespace(
            proxy_ha_sensors_enabled=True,
            proxy_ha_sensors_skip_existing=False,
            proxy_ha_sensors_mqtt_discovery_enabled=False,
        )
        proxy._mqtt_api = None
        proxy._mqtt_sensor_error_logged = False
        proxy._proxy_ha_sensor_owned_entities = set()
        written_states: dict[str, tuple[str, dict]] = {}

        def get_state(entity_id: str, attribute=None):
            if entity_id == "input_text.zendure_2400_ac_batterij_volgorde":
                return asyncio.create_task(asyncio.sleep(0, result="1;2;3"))
            return asyncio.create_task(asyncio.sleep(0, result=None))

        def set_state(entity_id: str, state, attributes, replace, check_existence):
            written_states[entity_id] = (state, attributes)
            return asyncio.create_task(asyncio.sleep(0))

        proxy.get_state = get_state
        proxy.set_state = set_state
        proxy._proxy_log = lambda *args, **kwargs: None

        await proxy._publish_proxy_ha_sensors({"properties": {}, "packData": []})

        self.assertIn("sensor.zendure_proxy_versie", written_states)
        self.assertEqual(written_states["sensor.zendure_proxy_versie"][0], "unknown")
        self.assertTrue(
            written_states["sensor.zendure_proxy_versie"][1]["zendure_proxy_managed"]
        )

    async def test_publish_proxy_ha_sensors_continues_after_set_state_error(
        self,
    ) -> None:
        proxy = ZendureProxy.__new__(ZendureProxy)
        proxy._cfg = types.SimpleNamespace(
            proxy_ha_sensors_enabled=True,
            proxy_ha_sensors_skip_existing=False,
            proxy_ha_sensors_mqtt_discovery_enabled=False,
        )
        proxy._mqtt_api = None
        proxy._mqtt_sensor_error_logged = False
        proxy._proxy_ha_sensor_owned_entities = set()
        written_entities: set[str] = set()
        log_lines: list[tuple[str, str]] = []

        def get_state(entity_id: str, attribute=None):
            if entity_id == "input_text.zendure_2400_ac_batterij_volgorde":
                return None
            return None

        def set_state(entity_id: str, state, attributes, replace, check_existence):
            if entity_id == "sensor.zendure_1_health":
                raise RuntimeError("set_state failed")
            written_entities.add(entity_id)
            return None

        proxy.get_state = get_state
        proxy.set_state = set_state
        proxy._proxy_log = lambda message, level="INFO", **_kwargs: log_lines.append(
            (message, level)
        )

        await proxy._publish_proxy_ha_sensors({"properties": {}, "packData": []})

        self.assertNotIn("sensor.zendure_1_health", written_entities)
        self.assertIn("sensor.zendure_proxy_versie", written_entities)
        self.assertEqual(len(log_lines), 1)
        self.assertEqual(log_lines[0][1], "WARNING")
        self.assertIn(
            "Proxy sensor publish failed: entity_id=sensor.zendure_1_health",
            log_lines[0][0],
        )

    async def test_first_health_sync_updates_existing_healthy_sensors(self) -> None:
        proxy = ZendureProxy.__new__(ZendureProxy)
        proxy._cfg = types.SimpleNamespace(
            proxy_ha_sensors_enabled=True,
            proxy_ha_sensors_skip_existing=True,
            proxy_ha_sensors_mqtt_discovery_enabled=False,
        )
        proxy._mqtt_api = None
        proxy._mqtt_sensor_error_logged = False
        proxy._proxy_ha_sensor_owned_entities = set()
        written_states: dict[str, tuple[str, dict, bool]] = {}
        log_lines: list[tuple[str, str]] = []

        def get_state(entity_id: str, attribute=None):
            if entity_id == "input_text.zendure_2400_ac_batterij_volgorde":
                return None
            return {
                "state": "Degraded",
                "attributes": {"friendly_name": "Existing REST sensor"},
            }

        def set_state(entity_id: str, state, attributes, replace, check_existence):
            written_states[entity_id] = (state, attributes, replace)
            return None

        proxy.get_state = get_state
        proxy.set_state = set_state
        proxy._proxy_log = lambda message, level="INFO", **_kwargs: log_lines.append(
            (message, level)
        )
        response = {
            "proxyVersion": "test",
            "packData": [],
            "properties": {
                "sn_1": "SN1",
                "sn_2": "SN2",
                "ipAddress_1": "ip1",
                "ipAddress_2": "ip2",
                "electricLevel_1": 54,
                "electricLevel_2": 55,
            },
            "proxyHealth": {
                "configuredCount": 2,
                "healthyCount": 2,
                "unhealthyCount": 0,
                "excludedCount": 0,
                "recoveringCount": 0,
                "degradedCount": 0,
                "deadCount": 0,
                "unhealthyDevices": [],
                "excludedDevices": [],
                "recoveringDevices": [],
                "degradedDevices": [],
                "deadDevices": [],
            },
        }

        await proxy._publish_health_transition_sensors(response)

        self.assertEqual(
            written_states["sensor.proxy_zendure_pool_healthy"][0],
            "Healthy",
        )
        self.assertEqual(written_states["sensor.zendure_1_health"][0], "Healthy")
        self.assertEqual(written_states["sensor.zendure_2_health"][0], "Healthy")
        self.assertFalse(written_states["sensor.proxy_zendure_pool_healthy"][2])
        self.assertNotIn(
            "zendure_proxy_managed",
            written_states["sensor.proxy_zendure_pool_healthy"][1],
        )
        self.assertEqual(log_lines, [])

    async def test_health_transition_updates_existing_health_sensors_until_recovered(
        self,
    ) -> None:
        proxy = ZendureProxy.__new__(ZendureProxy)
        proxy._cfg = types.SimpleNamespace(
            proxy_ha_sensors_enabled=True,
            proxy_ha_sensors_skip_existing=True,
            proxy_ha_sensors_mqtt_discovery_enabled=False,
        )
        proxy._mqtt_api = None
        proxy._mqtt_sensor_error_logged = False
        proxy._proxy_ha_sensor_owned_entities = set()
        written_states: dict[str, tuple[str, dict]] = {}
        log_lines: list[tuple[str, str]] = []

        def get_state(entity_id: str, attribute=None):
            if entity_id == "input_text.zendure_2400_ac_batterij_volgorde":
                return None
            return {
                "state": "old",
                "attributes": {"friendly_name": "Existing REST sensor"},
            }

        def set_state(entity_id: str, state, attributes, replace, check_existence):
            written_states[entity_id] = (state, attributes)
            return None

        proxy.get_state = get_state
        proxy.set_state = set_state
        proxy._proxy_log = lambda message, level="INFO", **_kwargs: log_lines.append(
            (message, level)
        )
        health_item = {
            "slot": 2,
            "serialNumber": "SN2",
            "ipAddress": "ip2",
            "lastSuccessfulGetAgeSeconds": 301.0,
            "lastGetError": "GET returned no response",
            "recoverySecondsRemaining": 0.0,
        }
        response = {
            "proxyVersion": "test",
            "packData": [],
            "properties": {
                "sn_1": "SN1",
                "sn_2": "SN2",
                "ipAddress_1": "ip1",
                "ipAddress_2": "ip2",
                "electricLevel_2": "unavailable",
                "latestPowerCmd_2": "unavailable",
            },
            "proxyHealth": {
                "configuredCount": 2,
                "healthyCount": 1,
                "unhealthyCount": 1,
                "excludedCount": 1,
                "recoveringCount": 0,
                "degradedCount": 1,
                "deadCount": 0,
                "unhealthyDevices": [health_item],
                "excludedDevices": [health_item],
                "recoveringDevices": [],
                "degradedDevices": [health_item],
                "deadDevices": [],
            },
        }

        await proxy._publish_health_transition_sensors(response)

        self.assertEqual(
            written_states["sensor.proxy_zendure_pool_healthy"][0],
            "Degraded",
        )
        self.assertEqual(written_states["sensor.zendure_2_health"][0], "Degraded")
        self.assertEqual(
            written_states["sensor.zendure_2_laadpercentage"][0],
            "unavailable",
        )
        self.assertEqual(written_states["sensor.zendure_1_health"][0], "Healthy")
        self.assertNotIn(
            "zendure_proxy_managed",
            written_states["sensor.proxy_zendure_pool_healthy"][1],
        )
        self.assertEqual(proxy._proxy_ha_sensor_owned_entities, set())
        self.assertEqual(len(log_lines), 1)
        self.assertEqual(log_lines[0][1], "WARNING")
        self.assertIn("Zendure pool degraded: slot=2", log_lines[0][0])
        self.assertIn("serial=SN2", log_lines[0][0])
        self.assertIn("previous_health_state=Healthy", log_lines[0][0])
        self.assertIn("last_get_error=GET returned no response", log_lines[0][0])

        written_states.clear()
        await proxy._publish_health_transition_sensors(response)

        self.assertEqual(written_states, {})
        self.assertEqual(len(log_lines), 1)

        await proxy._publish_health_transition_sensors(response, force_all=True)

        self.assertEqual(
            written_states["sensor.proxy_zendure_pool_healthy"][0],
            "Degraded",
        )
        self.assertEqual(written_states["sensor.zendure_1_health"][0], "Healthy")
        self.assertEqual(written_states["sensor.zendure_2_health"][0], "Degraded")
        self.assertEqual(len(log_lines), 1)
        written_states.clear()

        dead_health_item = {
            **health_item,
            "lastSuccessfulGetAgeSeconds": 2000.0,
            "dead": True,
        }
        dead_response = {
            "proxyVersion": "test",
            "packData": [],
            "properties": {
                "sn_1": "SN1",
                "sn_2": "SN2",
                "ipAddress_1": "ip1",
                "ipAddress_2": "ip2",
                "electricLevel_2": "unavailable",
                "latestPowerCmd_2": "unavailable",
            },
            "proxyHealth": {
                "configuredCount": 2,
                "healthyCount": 1,
                "unhealthyCount": 1,
                "excludedCount": 1,
                "recoveringCount": 0,
                "degradedCount": 0,
                "deadCount": 1,
                "unhealthyDevices": [dead_health_item],
                "excludedDevices": [dead_health_item],
                "recoveringDevices": [],
                "degradedDevices": [],
                "deadDevices": [dead_health_item],
            },
        }

        await proxy._publish_health_transition_sensors(dead_response)

        self.assertEqual(
            written_states["sensor.proxy_zendure_pool_healthy"][0],
            "Degraded",
        )
        self.assertEqual(written_states["sensor.zendure_2_health"][0], "Dead")
        self.assertEqual(
            written_states["sensor.zendure_2_laadpercentage"][0],
            "unavailable",
        )
        self.assertEqual(len(log_lines), 2)
        self.assertEqual(log_lines[1][1], "WARNING")
        self.assertIn("Zendure pool dead: slot=2", log_lines[1][0])
        self.assertIn("previous_health_state=Degraded", log_lines[1][0])
        written_states.clear()

        healthy_response = {
            "proxyVersion": "test",
            "packData": [],
            "properties": {
                "sn_1": "SN1",
                "sn_2": "SN2",
                "ipAddress_1": "ip1",
                "ipAddress_2": "ip2",
                "electricLevel_2": 55,
                "latestPowerCmd_2": 0,
            },
            "proxyHealth": {
                "configuredCount": 2,
                "healthyCount": 2,
                "unhealthyCount": 0,
                "excludedCount": 0,
                "recoveringCount": 0,
                "degradedCount": 0,
                "deadCount": 0,
                "unhealthyDevices": [],
                "excludedDevices": [],
                "recoveringDevices": [],
                "degradedDevices": [],
                "deadDevices": [],
            },
        }

        await proxy._publish_health_transition_sensors(healthy_response)

        self.assertEqual(
            written_states["sensor.proxy_zendure_pool_healthy"][0],
            "Healthy",
        )
        self.assertEqual(written_states["sensor.zendure_2_health"][0], "Healthy")
        self.assertEqual(written_states["sensor.zendure_2_laadpercentage"][0], "55")
        self.assertEqual(len(log_lines), 3)
        self.assertEqual(log_lines[2][1], "INFO")
        self.assertIn("Zendure pool recovered: slot=2", log_lines[2][0])
        self.assertIn("previous_health_state=Dead", log_lines[2][0])

        written_states.clear()
        await proxy._publish_health_transition_sensors(dead_response)

        self.assertEqual(written_states["sensor.zendure_2_health"][0], "Dead")
        self.assertEqual(len(log_lines), 4)
        self.assertIn("Zendure pool dead: slot=2", log_lines[3][0])
        self.assertIn("previous_health_state=Healthy", log_lines[3][0])

    async def test_publish_proxy_mqtt_sensor_accepts_async_mqtt_publish(self) -> None:
        published: list[tuple[str, str, bool]] = []

        class FakeMqtt:
            def mqtt_publish(self, topic: str, payload: str, retain: bool):
                published.append((topic, payload, retain))
                return asyncio.create_task(asyncio.sleep(0))

        proxy = ZendureProxy.__new__(ZendureProxy)
        proxy._cfg = types.SimpleNamespace(
            proxy_ha_sensors_mqtt_discovery_prefix="homeassistant",
            proxy_ha_sensors_mqtt_state_prefix="zendure_proxy",
            proxy_ha_sensors_mqtt_retain=True,
        )
        proxy._mqtt_api = FakeMqtt()

        await proxy._publish_proxy_mqtt_sensor(
            "sensor.zendure_2_serienummer",
            "SN2",
            {"friendly_name": "Zendure 2 Serienummer"},
            {"proxyVersion": "test-version"},
            "123",
        )

        self.assertEqual(len(published), 3)
        self.assertEqual(
            published[0][0],
            "homeassistant/sensor/zendure_proxy/zendure_2_serienummer/config",
        )
        self.assertEqual(
            published[1][0],
            "zendure_proxy/sensor/zendure_2_serienummer/state",
        )
        self.assertEqual(
            published[2][0],
            "zendure_proxy/sensor/zendure_2_serienummer/attributes",
        )

    async def test_refresh_proxy_ha_sensors_fetches_devices_and_publishes_report(
        self,
    ) -> None:
        proxy = ZendureProxy.__new__(ZendureProxy)
        proxy._cfg = Config(device_ips=["ip1"])
        proxy._state = ProxyState(
            device_count=1,
            devices=[DeviceState(ip="ip1")],
            startup_ts=100.0,
        )
        proxy._clients = [_MutableGetClient(_device(1, "SN1"))]
        proxy._metrics = _FakeMetrics()
        proxy._proxy_ha_sensor_refresh_in_progress = False
        published_responses: list[tuple[dict, bool]] = []
        standby_checks = 0

        async def publish_report_sensors(
            response: dict,
            *,
            force_health_sensor_refresh: bool = False,
        ) -> None:
            published_responses.append((response, force_health_sensor_refresh))

        async def standby_check(_kwargs=None) -> None:
            nonlocal standby_checks
            standby_checks += 1

        proxy._publish_report_sensors = publish_report_sensors
        proxy._standby_check = standby_check
        proxy._mark_passive_zero_timestamps = lambda: None
        proxy._proxy_log = lambda *args, **kwargs: None

        await proxy._refresh_proxy_ha_sensors()

        self.assertEqual(len(published_responses), 1)
        self.assertEqual(published_responses[0][0]["proxyHealth"]["reason"], "fresh")
        self.assertTrue(published_responses[0][1])
        self.assertEqual(proxy._state.devices[0].sn, "SN1")
        self.assertGreater(proxy._state.last_upstream_get_ts, 0)
        self.assertFalse(proxy._state.get_refresh_in_progress)
        self.assertFalse(proxy._proxy_ha_sensor_refresh_in_progress)
        self.assertEqual(standby_checks, 1)

    async def test_refresh_proxy_ha_sensors_skips_overlap(self) -> None:
        proxy = ZendureProxy.__new__(ZendureProxy)
        proxy._cfg = Config(device_ips=["ip1"])
        proxy._proxy_ha_sensor_refresh_in_progress = True
        proxy._publish_report_sensors = lambda *_args, **_kwargs: self.fail(
            "_publish_report_sensors should not be called"
        )

        await proxy._refresh_proxy_ha_sensors()

    async def test_initialize_schedules_proxy_sensor_refresh_every_300_seconds(
        self,
    ) -> None:
        proxy = ZendureProxy.__new__(ZendureProxy)
        proxy.args = {
            "ip_zendure_1": "ip1",
            "proxy_ha_sensors_enabled": True,
        }
        proxy._create_file_logger = lambda: None
        proxy._get_mqtt_api = lambda: None
        proxy._restore_metrics_counters_from_ha = _noop_record_depths
        proxy._start_server = _noop_record_depths
        proxy.log = lambda *args, **kwargs: None
        run_every_calls: list[tuple[object, str, int]] = []

        async def register_endpoint(*args, **kwargs):
            return f"endpoint-{len(args)}"

        async def register_route(*args, **kwargs):
            return f"route-{len(args)}"

        async def run_every(callback, start, interval):
            run_every_calls.append((callback, start, interval))
            return f"timer-{interval}"

        proxy.register_endpoint = register_endpoint
        proxy.register_route = register_route
        proxy.run_every = run_every

        class FakeDeviceClient:
            def __init__(self, *args, **kwargs):
                return None

            async def close(self):
                return None

        with patch("zendure_proxy.DeviceClient", FakeDeviceClient):
            await proxy.initialize()

        proxy._processor_task.cancel()
        try:
            await proxy._processor_task
        except asyncio.CancelledError:
            pass

        self.assertIn(
            (proxy._refresh_proxy_ha_sensors, "now", 300),
            run_every_calls,
        )
        self.assertFalse(hasattr(proxy, "_proxy_ha_degraded_slots"))

    async def test_report_request_returns_cache_after_ha_get_timeout(self) -> None:
        proxy = ZendureProxy.__new__(ZendureProxy)
        proxy._cfg = Config(
            device_ips=["ip1"],
            ha_get_response_timeout=0.01,
            get_cache_max_age=999999999.0,
        )
        proxy._state = ProxyState(
            device_count=1,
            devices=[DeviceState(ip="ip1", sn="SN1")],
            startup_ts=100.0,
            latest_get_ts=100.0,
            last_get_response={
                "proxyVersion": "test",
                "packData": [],
                "properties": {"sn_1": "SN1", "ipAddress_1": "ip1"},
            },
        )
        proxy._queue = _NeverResolvingQueue()
        proxy._metrics = _FakeMetrics()
        proxy._publish_proxy_ha_sensors = _noop_publish
        proxy._record_incoming_depths = _noop_record_depths
        proxy._debug_capture_payload = lambda *args, **kwargs: None

        data, status = await proxy._execute_report_request()

        self.assertEqual(status, 200)
        self.assertEqual(data["proxyHealth"]["reason"], "ha_get_timeout")
        self.assertTrue(data["proxyHealth"]["servedFromCache"])

    async def test_report_rate_limit_uses_last_upstream_get_timestamp(self) -> None:
        current_ts = now()
        response = {
            "proxyHealth": {"servedFromCache": False, "reason": "fresh"},
            "packData": [],
            "properties": {"sn_1": "SN1", "ipAddress_1": "ip1"},
        }
        queue = _ImmediateGetQueue(response)
        proxy = ZendureProxy.__new__(ZendureProxy)
        proxy._cfg = Config(
            device_ips=["ip1"],
            ha_get_response_timeout=0.1,
            get_cache_max_age=300.0,
            get_rate_limit_window=1.0,
        )
        proxy._state = ProxyState(
            device_count=1,
            devices=[DeviceState(ip="ip1", sn="SN1")],
            startup_ts=current_ts - 10.0,
            last_ha_get_ts=current_ts,
            last_upstream_get_ts=current_ts - 2.0,
            latest_get_ts=current_ts,
            last_get_response={
                "proxyVersion": "test",
                "packData": [],
                "properties": {"sn_1": "SN1", "ipAddress_1": "ip1"},
            },
        )
        proxy._queue = queue
        proxy._metrics = _FakeMetrics()
        proxy._publish_proxy_ha_sensors = _noop_publish
        proxy._record_incoming_depths = _noop_record_depths
        proxy._debug_capture_payload = lambda *args, **kwargs: None
        proxy._standby_check = _noop_record_depths
        proxy._mark_passive_zero_timestamps = lambda: None

        data, status = await proxy._execute_report_request()

        self.assertEqual(status, 200)
        self.assertEqual(queue.enqueue_get_calls, 1)
        self.assertNotEqual(data["proxyHealth"]["reason"], "rate_limited")

    async def test_report_rate_limit_returns_cache_for_recent_upstream_get(self) -> None:
        current_ts = now()
        queue = _ImmediateGetQueue(
            {
                "proxyHealth": {"servedFromCache": False, "reason": "fresh"},
                "packData": [],
                "properties": {"sn_1": "SN1", "ipAddress_1": "ip1"},
            }
        )
        proxy = ZendureProxy.__new__(ZendureProxy)
        proxy._cfg = Config(
            device_ips=["ip1"],
            ha_get_response_timeout=0.1,
            get_cache_max_age=300.0,
            get_rate_limit_window=1.0,
        )
        proxy._state = ProxyState(
            device_count=1,
            devices=[DeviceState(ip="ip1", sn="SN1")],
            startup_ts=current_ts - 10.0,
            last_ha_get_ts=0.0,
            last_upstream_get_ts=current_ts,
            latest_get_ts=current_ts,
            last_get_response={
                "proxyVersion": "test",
                "packData": [],
                "properties": {"sn_1": "SN1", "ipAddress_1": "ip1"},
            },
        )
        proxy._queue = queue
        proxy._metrics = _FakeMetrics()
        published_reasons: list[str] = []

        async def publish_report_sensors(response: dict) -> None:
            published_reasons.append(response["proxyHealth"]["reason"])

        proxy._publish_report_sensors = publish_report_sensors
        proxy._record_incoming_depths = _noop_record_depths
        proxy._debug_capture_payload = lambda *args, **kwargs: None

        data, status = await proxy._execute_report_request()

        self.assertEqual(status, 200)
        self.assertEqual(queue.enqueue_get_calls, 0)
        self.assertEqual(data["proxyHealth"]["reason"], "rate_limited")
        self.assertTrue(data["proxyHealth"]["servedFromCache"])
        self.assertEqual(published_reasons, ["rate_limited"])

    async def test_rapid_report_requests_use_one_upstream_get_per_window(self) -> None:
        clock = _FakeClock(base=1000.0, step=0.1)
        proxy = ZendureProxy.__new__(ZendureProxy)
        proxy._cfg = Config(
            device_ips=["ip1"],
            ha_get_response_timeout=0.1,
            get_cache_max_age=300.0,
            get_rate_limit_window=1.0,
        )
        proxy._state = ProxyState(
            device_count=1,
            devices=[DeviceState(ip="ip1", sn="SN1")],
            startup_ts=clock.now() - 10.0,
        )
        queue = _ClockedGetQueue(proxy._state, proxy._cfg, clock)
        proxy._queue = queue
        proxy._metrics = _FakeMetrics()
        proxy._publish_proxy_ha_sensors = _noop_publish
        proxy._record_incoming_depths = _noop_record_depths
        proxy._debug_capture_payload = lambda *args, **kwargs: None
        proxy._standby_check = _noop_record_depths
        proxy._mark_passive_zero_timestamps = lambda: None

        responses = []
        with patch("zendure_proxy.now", clock.now):
            for tick in range(25):
                clock.tick = tick
                data, status = await proxy._execute_report_request()

                self.assertEqual(status, 200)
                responses.append(data)

        reasons = [data["proxyHealth"]["reason"] for data in responses]
        fresh_indices = [
            idx for idx, reason in enumerate(reasons) if reason == "fresh"
        ]

        self.assertEqual(queue.enqueue_get_calls, 3)
        self.assertEqual(fresh_indices, [0, 11, 22])
        self.assertEqual(reasons.count("rate_limited"), 22)
        self.assertEqual(responses[10]["properties"]["freshCounter"], 1)
        self.assertEqual(responses[11]["properties"]["freshCounter"], 2)
        self.assertEqual(responses[21]["properties"]["freshCounter"], 2)
        self.assertEqual(responses[22]["properties"]["freshCounter"], 3)

    async def test_processor_updates_cache_and_answers_pending_gets(self) -> None:
        proxy = ZendureProxy.__new__(ZendureProxy)
        proxy._cfg = Config(device_ips=["ip1"], get_cache_max_age=300.0)
        proxy._state = ProxyState(
            device_count=1,
            devices=[DeviceState(ip="ip1", sn="SN1")],
            startup_ts=100.0,
        )
        client = _DelayedGetClient(_device(1, "SN1"))
        proxy._clients = [client]
        proxy._queue = __import__("zendure_proxy_queue").RequestQueue()
        proxy._metrics = _FakeMetrics()
        proxy._proxy_log = lambda *args, **kwargs: None
        proxy._record_incoming_depths = _noop_record_depths
        proxy._publish_proxy_ha_sensors = _noop_publish
        proxy._standby_check = _noop_record_depths
        proxy._mark_passive_zero_timestamps = lambda: None

        processor = asyncio.create_task(proxy._processor())
        first = await proxy._queue.enqueue_get()
        await asyncio.sleep(0)
        second = await proxy._queue.enqueue_get()
        client.release()

        first_data = await asyncio.wait_for(first, timeout=1)
        second_data = await asyncio.wait_for(second, timeout=1)
        processor.cancel()
        await processor

        self.assertEqual(first_data["properties"]["sn_1"], "SN1")
        self.assertEqual(second_data["properties"]["sn_1"], "SN1")
        self.assertGreater(proxy._state.last_upstream_get_ts, 0)
        self.assertEqual(proxy._state.last_get_response["properties"]["sn_1"], "SN1")


class ProxySensorCompatibilityTests(unittest.TestCase):
    def test_get_cache_config_defaults_and_overrides_are_loaded(self) -> None:
        default_cfg = Config(device_ips=[])
        self.assertEqual(default_cfg.zendure_request_timeout, 60.0)
        self.assertTrue(default_cfg.separate_get_post_connections)
        self.assertEqual(default_cfg.idle_connection_close_seconds, 600.0)
        self.assertEqual(default_cfg.ha_get_response_timeout, 8.0)
        self.assertEqual(default_cfg.get_cache_max_age, 300.0)
        self.assertEqual(default_cfg.get_rate_limit_window, 1.0)
        self.assertEqual(default_cfg.get_recovery_window, 30.0)
        self.assertEqual(default_cfg.degraded_power_hold_seconds, 1800.0)
        self.assertEqual(default_cfg.anti_pingpong_reserve_power_watts, 40)
        self.assertEqual(default_cfg.relay_saver_min_power_watts, 40)

    def test_proxy_sensor_builder_ignores_non_string_battery_order(self) -> None:
        class TaskLike:
            pass

        sensors = build_proxy_ha_sensors(
            {"properties": {}, "packData": []},
            battery_order_raw=TaskLike(),
        )

        self.assertIn("sensor.zendure_proxy_versie", sensors)

    def test_proxy_sensor_builder_keeps_node_red_rest_compatibility(self) -> None:
        response = _combined_three_device_response()
        sensors = build_proxy_ha_sensors(response)

        self.assertEqual(response["properties"]["socLimit_2"], 2)
        self.assertEqual(response["properties"]["acMode_2"], 1)
        self.assertEqual(response["properties"]["inputLimit_2"], 100)
        self.assertEqual(response["properties"]["outputLimit_2"], 0)
        self.assertEqual(response["sn_2"], "SN2")
        self.assertEqual(response["properties"]["dualModeDamper"], 1)
        self.assertEqual(response["properties"]["activeDevice"], 7)
        self.assertEqual(
            sensors["sensor.zendure_2_soc_limiet_status"][0],
            "Ontlaadlimiet bereikt",
        )
        self.assertEqual(sensors["sensor.zendure_2_serienummer"][0], "SN2")
        self.assertEqual(sensors["sensor.zendure_2_modus"][0], "Opladen")
        self.assertEqual(sensors["sensor.zendure_2_relais_stand"][0], "Oplaadstand")
        self.assertEqual(sensors["sensor.dual_mode_demper_status"][0], "Aan")
        self.assertIn("sensor.anti_pingpong_status", sensors)
        self.assertEqual(sensors["sensor.proxy_zendure_pool_healthy"][0], "Healthy")

    def test_proxy_sensor_builder_publishes_per_device_modes(self) -> None:
        results = [
            _device(1, "SN1", ac_mode=1, input_limit=500, grid_input_power=500),
            _device(2, "SN2", ac_mode=None, input_limit=0, grid_input_power=0),
            _device(
                3,
                "SN3",
                ac_mode=2,
                input_limit=0,
                output_limit=700,
                grid_input_power=0,
                output_home_power=700,
            ),
        ]
        state = ProxyState(
            device_count=3,
            devices=[
                DeviceState(ip="ip1", sn="SN1"),
                DeviceState(ip="ip2", sn="SN2"),
                DeviceState(ip="ip3", sn="SN3"),
            ],
        )
        for idx, dev in enumerate(state.devices):
            props = results[idx]["properties"]
            dev.electric_level = props["electricLevel"]
            dev.smart_mode = props["smartMode"]
            dev.soc_limit = props["socLimit"]

        response = build_combined_response(
            results,
            state,
            Config(device_ips=["ip1", "ip2", "ip3"]),
        )
        sensors = build_proxy_ha_sensors(response)

        self.assertEqual(sensors["sensor.zendure_1_modus"][0], "Opladen")
        self.assertEqual(sensors["sensor.zendure_1_relais_stand"][0], "Oplaadstand")
        self.assertEqual(sensors["sensor.zendure_2_modus"][0], "Standby")
        self.assertEqual(sensors["sensor.zendure_2_relais_stand"][0], "Standby")
        self.assertEqual(sensors["sensor.zendure_3_modus"][0], "Ontladen")
        self.assertEqual(sensors["sensor.zendure_3_relais_stand"][0], "Ontlaadstand")

    def test_combined_response_recovers_power_command_after_restart(self) -> None:
        results = [
            _device(1, "SN1", input_limit=799, grid_input_power=799),
            _device(2, "SN2", input_limit=800, grid_input_power=800),
            _device(3, "SN3", input_limit=800, grid_input_power=800),
        ]
        state = ProxyState(
            device_count=3,
            devices=[
                DeviceState(ip="ip1", sn="SN1"),
                DeviceState(ip="ip2", sn="SN2"),
                DeviceState(ip="ip3", sn="SN3"),
            ],
        )
        for idx, dev in enumerate(state.devices):
            dev.electric_level = results[idx]["properties"]["electricLevel"]
            dev.smart_mode = results[idx]["properties"]["smartMode"]
            dev.soc_limit = results[idx]["properties"]["socLimit"]

        response = build_combined_response(
            results,
            state,
            Config(device_ips=["ip1", "ip2", "ip3"]),
        )
        sensors = build_proxy_ha_sensors(response)

        self.assertEqual(response["properties"]["latestPowerCmd"], 2399)
        self.assertEqual(response["properties"]["latestPowerCmd_1"], 799)
        self.assertEqual(response["properties"]["latestPowerCmd_2"], 800)
        self.assertEqual(response["properties"]["latestPowerCmd_3"], 800)
        self.assertEqual(response["properties"]["activeDevice"], 7)
        self.assertEqual(sensors["sensor.vermogensopdracht"][0], 2399)
        self.assertEqual(sensors["sensor.vermogensopdracht_zendure_2"][0], 800)
        self.assertEqual(sensors["sensor.zendure_actief_device"][0], "Alle")

    def test_post_uses_previous_charge_mode_for_input_limit_without_ac_mode(self) -> None:
        state = ProxyState(
            device_count=3,
            devices=[
                DeviceState(ip="ip1", sn="SN1", electric_level=81),
                DeviceState(ip="ip2", sn="SN2", electric_level=89),
                DeviceState(ip="ip3", sn="SN3", electric_level=81),
            ],
            max_power_in=800,
            ac_mode=1,
        )
        clients = [_FakeClient() for _idx in range(3)]

        asyncio.run(
            execute_post(
                {"properties": {"inputLimit": 2400}},
                clients,
                state,
                Config(device_ips=["ip1", "ip2", "ip3"]),
                lambda *args, **kwargs: None,
            )
        )

        self.assertEqual(state.ac_mode, 1)
        self.assertEqual(state.latest_power_cmd, 2400)
        self.assertEqual([dev.latest_power_cmd for dev in state.devices], [800, 799, 800])
        self.assertEqual(["acMode" in client.posts[0]["properties"] for client in clients], [False, False, False])
        self.assertEqual(
            [client.posts[0]["properties"]["inputLimit"] for client in clients],
            [800, 799, 800],
        )

    def test_zero_power_post_does_not_send_unknown_ac_mode_zero(self) -> None:
        state = ProxyState(
            device_count=2,
            devices=[
                DeviceState(ip="ip1", sn="SN1", electric_level=80),
                DeviceState(ip="ip2", sn="SN2", electric_level=80),
            ],
            max_power_in=800,
            max_power_out=800,
            ac_mode=0,
        )
        clients = [_FakeClient() for _idx in range(2)]

        asyncio.run(
            execute_post(
                {"properties": {"inputLimit": 0, "outputLimit": 0}},
                clients,
                state,
                Config(device_ips=["ip1", "ip2"]),
                lambda *args, **kwargs: None,
            )
        )

        self.assertEqual(
            [client.posts[0]["properties"] for client in clients],
            [
                {"inputLimit": 0, "outputLimit": 0},
                {"inputLimit": 0, "outputLimit": 0},
            ],
        )

    def test_mqtt_discovery_keeps_unique_id_and_default_entity_id(self) -> None:
        config = mqtt_sensor_config(
            "sensor.zendure_2_serienummer",
            {"friendly_name": "Zendure 2 Serienummer"},
            "homeassistant",
            "zendure_proxy",
        )

        self.assertEqual(config["unique_id"], "zendure_proxy_zendure_2_serienummer")
        self.assertEqual(
            config["default_entity_id"],
            "sensor.zendure_2_serienummer",
        )

    def test_metrics_restore_keeps_incremental_counters_after_restart(self) -> None:
        metrics = MetricsRegistry(1)

        restored = metrics.restore_counters_from_sensors(
            {
                "sensor.zendure_proxy_incoming_get_total": "5",
                "sensor.zendure_proxy_incoming_get_rate_limited_cache_total": "6",
                "sensor.zendure_proxy_queue_get_coalesced_total": "7",
                "sensor.zendure_proxy_device_1_get_total": "11",
                "sensor.zendure_proxy_device_1_get_last_known_fallback_total": "12",
                "sensor.zendure_proxy_device_1_relay_switches_total": "13",
            }
        )

        self.assertEqual(restored, 6)
        self.assertEqual(metrics.incoming["GET"].total, 5)
        self.assertEqual(metrics.incoming_get_rate_limited_cache_total, 6)
        self.assertEqual(metrics.queue_get_coalesced_requests_total, 7)
        self.assertEqual(metrics.devices[0].get.total, 11)
        self.assertEqual(metrics.devices[0].get_last_known_fallback_total, 12)
        self.assertEqual(metrics.devices[0].relay_switches_total, 13)

    def test_metrics_restore_preserves_large_integer_counters(self) -> None:
        metrics = MetricsRegistry(1)
        huge_counter = 1234567890123456789012345678901234567890

        restored = metrics.restore_counters_from_sensors(
            {
                "sensor.zendure_proxy_incoming_get_total": str(huge_counter),
                "sensor.zendure_proxy_queue_get_coalesced_total": f"{huge_counter}.0",
            }
        )

        self.assertEqual(restored, 2)
        self.assertEqual(metrics.incoming["GET"].total, huge_counter)
        self.assertEqual(metrics.queue_get_coalesced_requests_total, huge_counter)

    def test_metrics_relay_switch_counter_uses_measured_edges(self) -> None:
        metrics = MetricsRegistry(1)

        metrics.record_device_relay_measurement(0, False)
        metrics.record_device_relay_measurement(0, False)
        metrics.record_device_relay_measurement(0, True)
        metrics.record_device_relay_measurement(0, True)
        metrics.record_device_relay_measurement(0, False)
        metrics.record_device_relay_measurement(0, False)

        self.assertEqual(metrics.devices[0].relay_switches_total, 2)

    def test_metrics_exposes_cache_and_fallback_counters(self) -> None:
        metrics = MetricsRegistry(2)

        metrics.record_incoming_get_rate_limited_cache()
        metrics.record_device_get_last_known_fallback(1)
        metrics.record_queue_batch(
            get_count=3,
            post_group_count=2,
            coalesced_gets=2,
            deduplicated_posts=1,
            deduplicated_groups=1,
        )

        snap = metrics.snapshot()

        self.assertEqual(
            snap["queue"]["incoming_get_rate_limited_cache_total"],
            1,
        )
        self.assertEqual(
            snap["devices"][1]["get_last_known_fallback_total"],
            1,
        )
        self.assertEqual(
            snap["queue"]["incoming_get_rate_limited_cache_activity"]["delta"],
            1,
        )
        self.assertGreater(
            snap["queue"]["incoming_get_rate_limited_cache_activity"]["last_hit_ts"],
            0,
        )
        self.assertEqual(snap["queue"]["get_coalesced_requests_activity"]["delta"], 2)
        self.assertEqual(
            snap["queue"]["post_deduplicated_requests_activity"]["delta"],
            1,
        )
        self.assertEqual(
            snap["devices"][1]["get_last_known_fallback_activity"]["delta"],
            1,
        )
        self.assertGreater(
            snap["devices"][1]["get_last_known_fallback_activity"]["last_hit_ts"],
            0,
        )

        sensors = metrics.flat_ha_sensors()
        self.assertEqual(
            sensors["sensor.zendure_proxy_incoming_get_rate_limited_cache_total"][0],
            1,
        )
        self.assertEqual(
            sensors["sensor.zendure_proxy_device_2_get_last_known_fallback_total"][0],
            1,
        )

        prometheus = "\n".join(metrics.prometheus_lines())
        self.assertIn("zendure_proxy_incoming_get_rate_limited_cache_total 1", prometheus)
        self.assertIn(
            'zendure_proxy_device_get_last_known_fallback_total{device="2"} 1',
            prometheus,
        )

    def test_metrics_snapshot_exposes_five_minute_rates_and_samples(self) -> None:
        metrics = MetricsRegistry(1)

        for _ in range(2500):
            metrics.start_incoming("GET")
            metrics.finish_incoming("GET", 25.0, 200)

        snap = metrics.snapshot()

        self.assertEqual(snap["window_s"], 300)
        self.assertEqual(snap["incoming"]["GET"]["latency"]["count"], 2500)
        self.assertAlmostEqual(
            snap["incoming"]["GET"]["window"]["rate_per_s"],
            2500 / 300,
        )

        sensors = metrics.flat_ha_sensors()
        self.assertIn(
            "sensor.zendure_proxy_incoming_get_requests_per_second_5m",
            sensors,
        )
        self.assertIn(
            "sensor.zendure_proxy_incoming_get_latency_samples_5m",
            sensors,
        )

    def test_metrics_dashboard_labels_five_minute_values(self) -> None:
        metrics = MetricsRegistry(1)
        metrics.start_incoming("GET")
        metrics.finish_incoming("GET", 25.0, 504, timeout=True)
        metrics.start_outgoing(0, "GET")
        metrics.finish_outgoing(0, "GET", 40.0, True)

        html = render_metrics_dashboard(
            "Zendure proxy metrics",
            metrics.snapshot(),
            10,
        )

        self.assertIn("Window metrics: last 5 min", html)
        self.assertIn("Req/s 5m", html)
        self.assertIn("Samples 5m", html)
        self.assertIn("Avg 5m ms", html)
        self.assertIn("P95 5m ms", html)
        self.assertIn("Timeouts", html)
        self.assertIn("Last success", html)
        self.assertIn("Last error", html)
        self.assertIn('class="help"', html)
        self.assertIn("POST older requests skipped", html)
        self.assertIn("two skipped old requests and one sent newest request", html)
        self.assertIn("POST key groups deduplicated", html)
        self.assertIn("count as one deduplicated key group", html)
        self.assertIn("GET responses served from rate-limit cache", html)
        self.assertIn("Zendure 1 GET last-known fallbacks", html)
        self.assertIn("+5m", html)
        self.assertIn("Last hit", html)
        self.assertIn("Queue-depth rows are gauges", html)
        self.assertNotIn('http-equiv="refresh"', html)
        self.assertIn('help.addEventListener("mouseenter", pauseRefresh)', html)
        self.assertIn('help.addEventListener("mouseleave", scheduleRefresh)', html)
        self.assertIn("Auto-refresh paused while this help is open.", html)

    def test_execute_get_records_relay_switches_from_fresh_measurements(self) -> None:
        cfg = Config(device_ips=["ip1", "ip2"])
        state = ProxyState(
            device_count=2,
            devices=[DeviceState(ip="ip1"), DeviceState(ip="ip2")],
            startup_ts=100.0,
        )
        metrics = MetricsRegistry(2)
        clients = [
            _MutableGetClient(
                _device(1, "SN1", output_pack_power=0, pack_input_power=0)
            ),
            _MutableGetClient(
                _device(2, "SN2", output_pack_power=0, pack_input_power=20)
            ),
        ]

        asyncio.run(
            execute_get(clients, state, cfg, lambda *args, **kwargs: None, metrics)
        )
        clients[0].response = _device(
            1, "SN1", output_pack_power=0, pack_input_power=15
        )
        clients[1].response = None
        asyncio.run(
            execute_get(clients, state, cfg, lambda *args, **kwargs: None, metrics)
        )
        clients[0].response = _device(
            1, "SN1", output_pack_power=0, pack_input_power=0
        )
        clients[1].response = _device(
            2, "SN2", output_pack_power=0, pack_input_power=20
        )
        asyncio.run(
            execute_get(clients, state, cfg, lambda *args, **kwargs: None, metrics)
        )

        self.assertEqual(metrics.devices[0].relay_switches_total, 2)
        self.assertEqual(metrics.devices[1].relay_switches_total, 0)

    def test_execute_get_records_last_known_fallback_per_device(self) -> None:
        cfg = Config(device_ips=["ip1", "ip2"])
        state = ProxyState(
            device_count=2,
            devices=[
                DeviceState(
                    ip="ip1",
                    sn="SN1",
                    last_response=_device(1, "SN1", output_pack_power=0),
                ),
                DeviceState(ip="ip2", sn="SN2"),
            ],
            startup_ts=100.0,
            last_get_response={"proxyVersion": "test", "packData": []},
        )
        metrics = MetricsRegistry(2)
        clients = [_MutableGetClient(None), _MutableGetClient(_device(2, "SN2"))]

        response = asyncio.run(
            execute_get(clients, state, cfg, lambda *args, **kwargs: None, metrics)
        )

        self.assertEqual(response["proxyHealth"]["reason"], "upstream_partial")
        self.assertEqual(metrics.devices[0].get_last_known_fallback_total, 1)
        self.assertEqual(metrics.devices[1].get_last_known_fallback_total, 0)

    def test_diagnostics_reset_clears_node_red_counters(self) -> None:
        proxy = ZendureProxy.__new__(ZendureProxy)
        proxy._state = ProxyState(
            device_count=2,
            devices=[DeviceState(ip="ip1"), DeviceState(ip="ip2")],
            counter_get_received=3,
            counter_get_replies=2,
            counter_get_timeouts=1,
            counter_config_drop=4,
            counter_serial_missing_drop=5,
            counter_post_received=6,
            counter_post_replies=7,
            counter_missing=[8, 9, 10],
        )

        proxy._reset_node_red_counters()

        self.assertEqual(proxy._state.counter_get_received, 0)
        self.assertEqual(proxy._state.counter_get_replies, 0)
        self.assertEqual(proxy._state.counter_get_timeouts, 0)
        self.assertEqual(proxy._state.counter_config_drop, 0)
        self.assertEqual(proxy._state.counter_serial_missing_drop, 0)
        self.assertEqual(proxy._state.counter_post_received, 0)
        self.assertEqual(proxy._state.counter_post_replies, 0)
        self.assertEqual(proxy._state.counter_missing, [0, 0, 0])

    def test_diagnostics_warnings_show_mismatched_limits_and_stale_get(self) -> None:
        proxy = ZendureProxy.__new__(ZendureProxy)
        proxy._state = ProxyState(
            device_count=2,
            devices=[
                DeviceState(
                    ip="ip1",
                    charge_max_limit=800,
                    inverse_max_power=700,
                    last_response={"properties": {"minSoc": 100, "socSet": 900}},
                ),
                DeviceState(
                    ip="ip2",
                    charge_max_limit=900,
                    inverse_max_power=800,
                    last_response={"properties": {"minSoc": 120, "socSet": 1000}},
                ),
            ],
            latest_get_ts=0,
        )

        warnings = proxy._diagnostics_warnings()

        self.assertIn("chargeMaxLimit differs between devices: [800, 900]", warnings)
        self.assertIn("inverseMaxPower differs between devices: [700, 800]", warnings)
        self.assertIn("minSoc differs between devices: [100, 120]", warnings)
        self.assertIn("socSet differs between devices: [900, 1000]", warnings)
        self.assertIn("No recent GET within 10 seconds", warnings)

    def test_debug_payload_capture_writes_to_existing_file_logger(self) -> None:
        class FakeFileLogger:
            def __init__(self):
                self.lines = []

            def log(self, message: str, level: str) -> None:
                self.lines.append((message, level))

        proxy = ZendureProxy.__new__(ZendureProxy)
        proxy._cfg = types.SimpleNamespace(debug_payload_capture_enabled=True)
        proxy._file_logger = FakeFileLogger()
        proxy.log = lambda *args, **kwargs: None

        proxy._debug_capture_payload("GET", "To Home Assistant", {"ok": True})

        self.assertEqual(proxy._file_logger.lines[0][1], "DEBUG")
        self.assertIn('"debug_message_type": "GET"', proxy._file_logger.lines[0][0])
        self.assertIn('"debug_direction": "To Home Assistant"', proxy._file_logger.lines[0][0])

    def test_passive_devices_get_zero_timestamp_after_get_success(self) -> None:
        proxy = ZendureProxy.__new__(ZendureProxy)
        proxy._state = ProxyState(
            device_count=2,
            devices=[
                DeviceState(ip="ip1", latest_power_cmd=500),
                DeviceState(ip="ip2", latest_power_cmd=0),
            ],
            devices_active_idx=[0],
        )

        proxy._mark_passive_zero_timestamps()

        self.assertEqual(proxy._state.devices[0].latest_power_cmd_zero_ts, 0.0)
        self.assertGreater(proxy._state.devices[1].latest_power_cmd_zero_ts, 0)

    def test_health_recovery_window_controls_eligible_devices(self) -> None:
        cfg = Config(device_ips=["ip1", "ip2"], get_cache_max_age=300.0)
        state = ProxyState(
            device_count=2,
            devices=[DeviceState(ip="ip1"), DeviceState(ip="ip2")],
            startup_ts=100.0,
        )

        self.assertEqual(eligible_device_indices(state, cfg, current_ts=120.0), [0, 1])
        self.assertEqual(eligible_device_indices(state, cfg, current_ts=401.0), [0, 1])

        record_get_results(
            state,
            cfg,
            [_device(1, "SN1"), None],
            current_ts=402.0,
        )
        self.assertEqual(eligible_device_indices(state, cfg, current_ts=422.0), [0])
        self.assertEqual(eligible_device_indices(state, cfg, current_ts=432.0), [0])

        record_get_results(state, cfg, [_device(1, "SN1"), None], current_ts=433.0)
        self.assertEqual(state.devices[1].recovery_started_ts, 0.0)

    def test_degraded_device_becomes_dead_after_power_hold_window(self) -> None:
        cfg = Config(
            device_ips=["ip1"],
            degraded_power_hold_seconds=1800.0,
        )
        state = ProxyState(
            device_count=1,
            devices=[
                DeviceState(
                    ip="ip1",
                    sn="SN1",
                    last_response=_device(1, "SN1", input_limit=500),
                    last_successful_get_ts=100.0,
                    excluded_since_ts=200.0,
                )
            ],
            startup_ts=1.0,
        )

        self.assertEqual(degraded_power_by_index(state, cfg, current_ts=500.0), {0: 500})
        summary = health_summary(state, cfg, current_ts=2001.0)

        self.assertEqual(degraded_power_by_index(state, cfg, current_ts=2001.0), {})
        self.assertEqual(summary["deadCount"], 1)
        self.assertEqual(summary["deadDevices"][0]["serialNumber"], "SN1")

    def test_proxy_health_sensors_report_degraded_pool_and_unavailable_slot(self) -> None:
        response = {
            "proxyVersion": "test",
            "packData": [],
            "properties": {
                "sn_1": "SN1",
                "sn_2": "SN2",
                "ipAddress_1": "ip1",
                "ipAddress_2": "ip2",
                "electricLevel_2": "unavailable",
                "latestPowerCmd_2": "unavailable",
            },
            "proxyHealth": {
                "configuredCount": 2,
                "healthyCount": 1,
                "unhealthyCount": 1,
                "excludedCount": 1,
                "recoveringCount": 0,
                "unhealthyDevices": [
                    {
                        "slot": 2,
                        "serialNumber": "SN2",
                        "ipAddress": "ip2",
                        "lastSuccessfulGetAgeSeconds": 301.0,
                        "lastGetError": "GET returned no response",
                        "recoverySecondsRemaining": 0.0,
                    }
                ],
                "excludedDevices": [
                    {
                        "slot": 2,
                        "serialNumber": "SN2",
                        "ipAddress": "ip2",
                        "lastSuccessfulGetAgeSeconds": 301.0,
                        "lastGetError": "GET returned no response",
                        "recoverySecondsRemaining": 0.0,
                    }
                ],
                "recoveringDevices": [],
            },
        }

        sensors = build_proxy_ha_sensors(response)

        self.assertEqual(sensors["sensor.zendure_2_health"][0], "Degraded")
        self.assertEqual(sensors["sensor.zendure_2_laadpercentage"][0], "unavailable")
        self.assertEqual(sensors["sensor.zendure_2_serienummer"][0], "SN2")
        self.assertEqual(sensors["sensor.proxy_zendure_pool_healthy"][0], "Degraded")

    def test_post_skips_excluded_device_until_recovery_window_finishes(self) -> None:
        cfg = Config(
            device_ips=["ip1", "ip2", "ip3"],
            get_cache_max_age=300.0,
            get_recovery_window=30.0,
            degraded_power_hold_seconds=999999999.0,
        )
        state = ProxyState(
            device_count=3,
            devices=[
                DeviceState(ip="ip1", sn="SN1", electric_level=50),
                DeviceState(
                    ip="ip2",
                    sn="SN2",
                    electric_level=50,
                    last_response=_device(2, "SN2", input_limit=500),
                    last_successful_get_ts=50.0,
                    excluded_since_ts=100.0,
                    recovery_started_ts=0.0,
                ),
                DeviceState(ip="ip3", sn="SN3", electric_level=50),
            ],
            max_power_in=800,
            ac_mode=1,
            startup_ts=1.0,
        )
        clients = [_FakeClient() for _idx in range(3)]

        asyncio.run(
            execute_post(
                {"properties": {"acMode": 1, "inputLimit": 1600}},
                clients,
                state,
                cfg,
                lambda *args, **kwargs: None,
            )
        )

        self.assertGreaterEqual(len(clients[0].posts), 1)
        self.assertEqual(len(clients[1].posts), 0)
        self.assertGreaterEqual(len(clients[2].posts), 1)
        client_1_power_posts = [
            post for post in clients[0].posts
            if "inputLimit" in post["properties"]
        ]
        client_3_power_posts = [
            post for post in clients[2].posts
            if "inputLimit" in post["properties"]
        ]
        self.assertEqual(client_1_power_posts[0]["properties"]["inputLimit"], 800)
        self.assertEqual(client_3_power_posts[0]["properties"]["inputLimit"], 800)
        self.assertEqual(state.input_limit_effective, 1600)
        self.assertEqual(state.devices[1].latest_power_cmd, 0)

        state.devices[1].recovery_started_ts = 1.0
        clients = [_FakeClient() for _idx in range(3)]
        asyncio.run(
            execute_post(
                {"properties": {"acMode": 1, "inputLimit": 2400}},
                clients,
                state,
                Config(
                    device_ips=["ip1", "ip2", "ip3"],
                    get_cache_max_age=300.0,
                    get_recovery_window=0.0,
                ),
                lambda *args, **kwargs: None,
            )
        )

        self.assertGreaterEqual(len(clients[0].posts), 1)
        self.assertGreaterEqual(len(clients[1].posts), 1)
        self.assertGreaterEqual(len(clients[2].posts), 1)
        self.assertTrue(
            any("inputLimit" in post["properties"] for post in clients[1].posts)
        )

    def test_post_sends_full_discharge_command_when_device_is_excluded(self) -> None:
        cfg = Config(
            device_ips=["ip1", "ip2", "ip3"],
            get_cache_max_age=300.0,
            get_recovery_window=30.0,
            degraded_power_hold_seconds=999999999.0,
        )
        state = ProxyState(
            device_count=3,
            devices=[
                DeviceState(ip="ip1", sn="SN1", electric_level=50),
                DeviceState(
                    ip="ip2",
                    sn="SN2",
                    electric_level=50,
                    last_response=_device(
                        2,
                        "SN2",
                        ac_mode=2,
                        input_limit=0,
                        output_limit=500,
                    ),
                    last_successful_get_ts=50.0,
                    excluded_since_ts=100.0,
                    recovery_started_ts=0.0,
                ),
                DeviceState(ip="ip3", sn="SN3", electric_level=50),
            ],
            max_power_out=800,
            ac_mode=2,
            startup_ts=1.0,
        )
        clients = [_FakeClient() for _idx in range(3)]

        asyncio.run(
            execute_post(
                {"properties": {"acMode": 2, "outputLimit": 1600}},
                clients,
                state,
                cfg,
                lambda *args, **kwargs: None,
            )
        )

        self.assertGreaterEqual(len(clients[0].posts), 1)
        self.assertEqual(len(clients[1].posts), 0)
        self.assertGreaterEqual(len(clients[2].posts), 1)
        self.assertEqual(clients[0].posts[0]["properties"]["outputLimit"], 800)
        self.assertEqual(clients[2].posts[0]["properties"]["outputLimit"], 800)
        self.assertEqual(state.output_limit_effective, 1600)
        self.assertEqual(state.devices[1].latest_power_cmd, 0)


def _combined_three_device_response() -> dict:
    results = [_device(1, "SN1"), _device(2, "SN2"), _device(3, "SN3")]
    state = ProxyState(
        device_count=3,
        devices=[
            DeviceState(ip="ip1"),
            DeviceState(ip="ip2"),
            DeviceState(ip="ip3"),
        ],
    )
    state.devices_active_idx = [0, 1, 2]
    state.device_active_count = 3
    state.latest_power_cmd = 300
    for idx, dev in enumerate(state.devices):
        props = results[idx]["properties"]
        dev.electric_level = props["electricLevel"]
        dev.soc_limit = props["socLimit"]
        dev.soc_status = props["socStatus"]
        dev.smart_mode = props["smartMode"]
        dev.sn = results[idx]["sn"]
        dev.charge_max_limit = props["chargeMaxLimit"]
        dev.inverse_max_power = props["inverseMaxPower"]
        dev.latest_power_cmd = 100

    return build_combined_response(
        results,
        state,
        Config(
            device_ips=["ip1", "ip2", "ip3"],
            solar_power_info=True,
            damper_enable=True,
            always_dual_mode=True,
            equal_mode=True,
        ),
    )


class _FakeClient:
    def __init__(self):
        self.posts = []

    async def post(self, payload: dict) -> dict:
        self.posts.append(payload)
        return {"ack": "pong"}


class _DelayedGetClient:
    def __init__(self, response: dict):
        self._response = response
        self._event = asyncio.Event()

    async def get(self) -> dict:
        await self._event.wait()
        return self._response

    def release(self) -> None:
        self._event.set()


class _MutableGetClient:
    def __init__(self, response: dict | None):
        self.response = response

    async def get(self) -> dict | None:
        return self.response


class _NeverResolvingQueue:
    async def enqueue_get(self):
        return asyncio.get_running_loop().create_future()

    async def depths(self):
        return 1, 0


class _ImmediateGetQueue:
    def __init__(self, response: dict):
        self.response = response
        self.enqueue_get_calls = 0

    async def enqueue_get(self):
        self.enqueue_get_calls += 1
        future = asyncio.get_running_loop().create_future()
        future.set_result(self.response)
        return future

    async def depths(self):
        return 0, 0


class _FakeClock:
    def __init__(self, *, base: float, step: float):
        self.base = base
        self.step = step
        self.tick = 0

    def now(self) -> float:
        return self.base + self.tick * self.step


class _ClockedGetQueue:
    def __init__(self, state: ProxyState, cfg: Config, clock: _FakeClock):
        self.state = state
        self.cfg = cfg
        self.clock = clock
        self.enqueue_get_calls = 0

    async def enqueue_get(self):
        self.enqueue_get_calls += 1
        current_ts = self.clock.now()
        self.state.last_upstream_get_ts = current_ts
        self.state.latest_get_ts = current_ts
        response = {
            "proxyVersion": "test",
            "packData": [],
            "properties": {
                "sn_1": "SN1",
                "ipAddress_1": "ip1",
                "freshCounter": self.enqueue_get_calls,
            },
        }
        data = response_with_proxy_health(
            response,
            self.state,
            self.cfg,
            served_from_cache=False,
            reason="fresh",
            refresh_in_progress=False,
            current_ts=current_ts,
        )
        self.state.last_get_response = data
        future = asyncio.get_running_loop().create_future()
        future.set_result(data)
        return future

    async def depths(self):
        return 0, 0


class _FakeMetrics:
    def start_incoming(self, _method: str) -> None:
        return None

    def finish_incoming(
        self,
        _method: str,
        _duration_ms: float,
        _status: int,
        _timeout: bool,
    ) -> None:
        return None

    def record_queue_batch(self, **_kwargs) -> None:
        return None

    def record_incoming_get_rate_limited_cache(self) -> None:
        return None

    def set_incoming_queue_depth(self, _get_depth: int, _post_depth: int) -> None:
        return None


async def _noop_publish(_response: dict, **_kwargs) -> None:
    return None


async def _noop_record_depths() -> None:
    return None


def _device(
    idx: int,
    sn: str,
    *,
    ac_mode: int | None = 1,
    input_limit: int = 100,
    output_limit: int = 0,
    output_pack_power: int = 0,
    pack_input_power: int | None = None,
    grid_input_power: int | None = None,
    output_home_power: int = 0,
) -> dict:
    if grid_input_power is None:
        grid_input_power = 20 + idx
    if pack_input_power is None:
        pack_input_power = 10 + idx
    return {
        "sn": sn,
        "product": f"Product {idx}",
        "packData": [{"socLevel": 50 + idx, "maxTemp": 2831 + idx}],
        "properties": {
            "acMode": ac_mode,
            "inputLimit": input_limit,
            "outputLimit": output_limit,
            "outputPackPower": output_pack_power,
            "packInputPower": pack_input_power,
            "gridInputPower": grid_input_power,
            "outputHomePower": output_home_power,
            "solarInputPower": 0,
            "gridOffPower": 0,
            "minSoc": 100,
            "socSet": 1000,
            "socLimit": idx,
            "electricLevel": 40 + idx,
            "smartMode": 1,
            "BatVolt": 5200,
            "remainOutTime": 0,
            "hyperTmp": 2831 + idx,
            "chargeMaxLimit": 800,
            "inverseMaxPower": 800,
            "packNum": 1,
            "rssi": -40,
            "is_error": 0,
            "socStatus": 0,
            "gridReverse": 0,
            "batCalTime": 12,
            "pvStatus": 1,
            "acStatus": 1,
            "dcStatus": 1,
            "gridOffMode": 2,
            "solarPower1": 1,
            "solarPower2": 2,
            "solarPower3": 3,
            "solarPower4": 4,
        },
    }


if __name__ == "__main__":
    unittest.main()
