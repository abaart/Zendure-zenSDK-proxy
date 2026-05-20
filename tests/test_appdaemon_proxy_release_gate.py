from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import types
import unittest


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
from zendure_proxy_get_handler import build_combined_response  # noqa: E402
from zendure_proxy_ha_sensors import build_proxy_ha_sensors  # noqa: E402
from zendure_proxy_metrics import MetricsRegistry  # noqa: E402
from zendure_proxy_mqtt_discovery import mqtt_sensor_config  # noqa: E402
from zendure_proxy_post_handler import execute_post  # noqa: E402
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


class ProxySensorCompatibilityTests(unittest.TestCase):
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

    def test_proxy_sensor_builder_publishes_per_device_modes(self) -> None:
        results = [
            _device(1, "SN1", ac_mode=1, input_limit=500, grid_input_power=500),
            _device(2, "SN2", ac_mode=0, input_limit=0, grid_input_power=0),
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

    def test_post_infers_charge_mode_from_input_limit_without_ac_mode(self) -> None:
        state = ProxyState(
            device_count=3,
            devices=[
                DeviceState(ip="ip1", sn="SN1", electric_level=81),
                DeviceState(ip="ip2", sn="SN2", electric_level=89),
                DeviceState(ip="ip3", sn="SN3", electric_level=81),
            ],
            max_power_in=800,
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
        self.assertEqual([client.posts[0]["properties"]["acMode"] for client in clients], [1, 1, 1])
        self.assertEqual(
            [client.posts[0]["properties"]["inputLimit"] for client in clients],
            [800, 799, 800],
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
                "sensor.zendure_proxy_queue_get_coalesced_total": "7",
                "sensor.zendure_proxy_device_1_get_total": "11",
            }
        )

        self.assertEqual(restored, 3)
        self.assertEqual(metrics.incoming["GET"].total, 5)
        self.assertEqual(metrics.queue_get_coalesced_requests_total, 7)
        self.assertEqual(metrics.devices[0].get.total, 11)


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


def _device(
    idx: int,
    sn: str,
    *,
    ac_mode: int = 1,
    input_limit: int = 100,
    output_limit: int = 0,
    grid_input_power: int | None = None,
    output_home_power: int = 0,
) -> dict:
    if grid_input_power is None:
        grid_input_power = 20 + idx
    return {
        "sn": sn,
        "product": f"Product {idx}",
        "packData": [{"socLevel": 50 + idx, "maxTemp": 2831 + idx}],
        "properties": {
            "acMode": ac_mode,
            "inputLimit": input_limit,
            "outputLimit": output_limit,
            "outputPackPower": 0,
            "packInputPower": 10 + idx,
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
