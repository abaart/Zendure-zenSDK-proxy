from __future__ import annotations

import asyncio

from zendure_proxy_config import Config
from zendure_proxy_post_handler import execute_post
from zendure_proxy_power import distribute_power
from zendure_proxy_state import DeviceState, ProxyState

from conftest import FakeDeviceClient
from node_red_expected import DISTRIBUTION_AFTER_MAX_CLIPPING


def _state(device_count: int) -> ProxyState:
    state = ProxyState(
        device_count=device_count,
        devices=[
            DeviceState(
                ip=f"ip{idx + 1}",
                sn=f"SN{idx + 1}",
                electric_level=50,
                smart_mode=1,
            )
            for idx in range(device_count)
        ],
        max_power_in=800,
        max_power_out=800,
    )
    state.devices_active_idx = [0]
    state.device_active_count = 1
    return state


def test_charge_and_discharge_power_commands_keep_node_red_signs() -> None:
    state = _state(2)
    clients = [FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(
        execute_post(
            {"properties": {"acMode": 2, "outputLimit": 600}},
            clients,
            state,
            Config(device_ips=["ip1", "ip2"]),
            lambda *args, **kwargs: None,
        )
    )

    assert state.latest_power_cmd == -600
    assert all(payload["properties"]["outputLimit"] >= 0 for payload in clients[0].post_payloads)
    assert state.devices[0].latest_power_cmd <= 0
    assert state.devices[1].latest_power_cmd <= 0


def test_limit_posts_are_divided_before_sending_to_devices() -> None:
    state = _state(2)
    clients = [FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(
        execute_post(
            {"properties": {"chargeMaxLimit": 1601, "inverseMaxPower": 1501}},
            clients,
            state,
            Config(device_ips=["ip1", "ip2"]),
            lambda *args, **kwargs: None,
        )
    )

    assert [client.post_payloads[0]["properties"] for client in clients] == [
        {"chargeMaxLimit": 800, "inverseMaxPower": 750},
        {"chargeMaxLimit": 800, "inverseMaxPower": 750},
    ]
    assert state.charge_max_limit_cmd == 1601
    assert state.charge_max_limit_effective == 1600
    assert state.inverse_max_power_cmd == 1501
    assert state.inverse_max_power_effective == 1500


def test_runtime_mode_toggles_update_proxy_state_without_device_posts() -> None:
    state = _state(2)
    clients = [FakeDeviceClient(), FakeDeviceClient()]
    responses = []

    for key in ("equalMode", "alwaysDualMode", "dualModeDamper"):
        responses.append(asyncio.run(
            execute_post(
                {"properties": {key: 1}},
                clients,
                state,
                Config(device_ips=["ip1", "ip2"]),
                lambda *args, **kwargs: None,
            )
        ))

    assert state.equal_mode is True
    assert state.always_dual_mode is True
    assert state.dualmode_damper_enabled is True
    assert [client.post_payloads for client in clients] == [[], []]
    assert responses == [
        {"properties": {"equalMode": 1}},
        {"properties": {"alwaysDualMode": 1}},
        {"properties": {"dualModeDamper": 1}},
    ]


def test_equal_mode_distributes_power_equally_across_active_devices() -> None:
    state = _state(3)
    state.equal_mode = True
    clients = [FakeDeviceClient(), FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(
        execute_post(
            {"properties": {"acMode": 1, "inputLimit": 1500}},
            clients,
            state,
            Config(device_ips=["ip1", "ip2", "ip3"]),
            lambda *args, **kwargs: None,
        )
    )

    assert [client.post_payloads[0]["properties"]["inputLimit"] for client in clients] == [
        500,
        500,
        500,
    ]


def test_invalid_direction_command_does_not_change_active_device() -> None:
    state = _state(2)
    state.ac_mode = 2
    state.single_mode_active_device = 0
    state.devices_active_idx = [0]
    state.devices[0].electric_level = 50
    state.devices[1].electric_level = 90
    clients = [FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(
        execute_post(
            {"properties": {"acMode": 2, "inputLimit": 300}},
            clients,
            state,
            Config(device_ips=["ip1", "ip2"]),
            lambda *args, **kwargs: None,
        )
    )

    assert state.single_mode_active_device == 0
    assert state.devices_active_idx == [0]
    assert [client.post_payloads[0]["properties"] for client in clients] == [
        {"acMode": 2, "inputLimit": 0},
        {"acMode": 2, "inputLimit": 0},
    ]


def test_invalid_direction_preserves_original_power_key_for_discharge() -> None:
    state = _state(2)
    state.ac_mode = 1
    clients = [FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(
        execute_post(
            {"properties": {"acMode": 1, "outputLimit": 300}},
            clients,
            state,
            Config(device_ips=["ip1", "ip2"]),
            lambda *args, **kwargs: None,
        )
    )

    assert [client.post_payloads[0]["properties"] for client in clients] == [
        {"acMode": 1, "outputLimit": 0},
        {"acMode": 1, "outputLimit": 0},
    ]


def test_ac_mode_only_post_forwards_only_ac_mode() -> None:
    state = _state(2)
    clients = [FakeDeviceClient(), FakeDeviceClient()]

    response = asyncio.run(
        execute_post(
            {"properties": {"acMode": 1}},
            clients,
            state,
            Config(device_ips=["ip1", "ip2"]),
            lambda *args, **kwargs: None,
        )
    )

    assert [client.post_payloads[0]["properties"] for client in clients] == [
        {"acMode": 1},
        {"acMode": 1},
    ]
    assert response == [{"ack": "pong"}, {"ack": "pong"}]


def test_smart_mode_one_sets_zero_timestamp_for_passive_devices() -> None:
    state = _state(2)
    state.devices_active_idx = [0]
    state.devices[1].smart_mode = 0
    clients = [FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(
        execute_post(
            {"properties": {"smartMode": 1}},
            clients,
            state,
            Config(device_ips=["ip1", "ip2"]),
            lambda *args, **kwargs: None,
        )
    )

    assert state.devices[1].latest_power_cmd_zero_ts > 0


def test_explicit_zero_power_keys_are_preserved() -> None:
    state = _state(2)
    clients = [FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(
        execute_post(
            {"properties": {"inputLimit": 500, "outputLimit": 0}},
            clients,
            state,
            Config(device_ips=["ip1", "ip2"]),
            lambda *args, **kwargs: None,
        )
    )

    assert [
        set(client.post_payloads[0]["properties"].keys()) for client in clients
    ] == [{"inputLimit", "outputLimit"}, {"inputLimit", "outputLimit"}]
    assert [client.post_payloads[0]["properties"]["outputLimit"] for client in clients] == [0, 0]


def test_ac_mode_inconsistent_adds_ac_mode_to_next_power_post() -> None:
    state = _state(2)
    state.device_active_count = 2
    state.devices_active_idx = [0, 1]
    state.ac_mode_inconsistent = True
    clients = [FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(
        execute_post(
            {"properties": {"inputLimit": 500}},
            clients,
            state,
            Config(device_ips=["ip1", "ip2"]),
            lambda *args, **kwargs: None,
        )
    )

    assert [client.post_payloads[0]["properties"] for client in clients] == [
        {"acMode": 1, "inputLimit": 250},
        {"acMode": 1, "inputLimit": 250},
    ]


def test_power_post_wakes_standby_device_in_same_payload() -> None:
    state = _state(2)
    state.devices[0].electric_level = 50
    state.devices[1].electric_level = 40
    state.devices[1].smart_mode = 0
    state.devices[1].standby_device = True
    state.single_mode_active_device = 1
    state.devices_active_idx = [1]
    clients = [FakeDeviceClient(), FakeDeviceClient()]

    async def run_post() -> None:
        await execute_post(
            {"properties": {"inputLimit": 500}},
            clients,
            state,
            Config(device_ips=["ip1", "ip2"]),
            lambda *args, **kwargs: None,
        )
        await asyncio.sleep(0)

    asyncio.run(run_post())

    assert clients[1].post_payloads == [
        {
            "sn": "SN2",
            "properties": {"acMode": 1, "inputLimit": 500, "smartMode": 1},
        }
    ]
    assert state.devices[1].standby_device is False
    assert state.devices[1].smart_mode == 1


def test_charging_one_device_below_min_soc_uses_low_device_only() -> None:
    state = _state(2)
    state.min_soc = 100
    state.devices[0].electric_level = 8
    state.devices[1].electric_level = 50
    clients = [FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(
        execute_post(
            {"properties": {"acMode": 1, "inputLimit": 1000}},
            clients,
            state,
            Config(device_ips=["ip1", "ip2"]),
            lambda *args, **kwargs: None,
        )
    )

    assert state.device_active_count == 1
    assert state.devices_active_idx == [0]
    assert [client.post_payloads[0]["properties"]["inputLimit"] for client in clients] == [
        800,
        0,
    ]


def test_soc_limit_selects_device_without_limit() -> None:
    state = _state(2)
    state.devices[0].soc_limit = 1
    clients = [FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(
        execute_post(
            {"properties": {"acMode": 1, "inputLimit": 1000}},
            clients,
            state,
            Config(device_ips=["ip1", "ip2"]),
            lambda *args, **kwargs: None,
        )
    )

    assert state.devices_active_idx == [1]

    state = _state(2)
    state.devices[1].soc_limit = 2
    clients = [FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(
        execute_post(
            {"properties": {"acMode": 2, "outputLimit": 1000}},
            clients,
            state,
            Config(device_ips=["ip1", "ip2"]),
            lambda *args, **kwargs: None,
        )
    )

    assert state.devices_active_idx == [0]


def test_high_soc_charging_uses_both_devices() -> None:
    state = _state(2)
    state.devices[0].electric_level = 98
    state.devices[1].electric_level = 94
    clients = [FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(
        execute_post(
            {"properties": {"acMode": 1, "inputLimit": 100}},
            clients,
            state,
            Config(device_ips=["ip1", "ip2"], device_change_diff=5),
            lambda *args, **kwargs: None,
        )
    )

    assert state.device_active_count == 2
    assert state.devices_active_idx == [0, 1]


def test_standby_devices_do_not_receive_zero_or_standalone_wake_posts() -> None:
    state = _state(2)
    state.ac_mode = 2
    state.devices[1].standby_device = True
    clients = [FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(
        execute_post(
            {"properties": {"acMode": 2, "outputLimit": 0}},
            clients,
            state,
            Config(device_ips=["ip1", "ip2"]),
            lambda *args, **kwargs: None,
        )
    )
    asyncio.run(
        execute_post(
            {"properties": {"smartMode": 1}},
            clients,
            state,
            Config(device_ips=["ip1", "ip2"]),
            lambda *args, **kwargs: None,
        )
    )

    assert [payload["properties"] for payload in clients[0].post_payloads] == [
        {"acMode": 2, "outputLimit": 0},
        {"smartMode": 1},
    ]
    assert clients[1].post_payloads == []


def test_standby_devices_do_not_receive_smart_mode_one_with_extra_keys() -> None:
    state = _state(2)
    state.devices[1].standby_device = True
    clients = [FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(
        execute_post(
            {"properties": {"smartMode": 1, "gridOffMode": 2}},
            clients,
            state,
            Config(device_ips=["ip1", "ip2"]),
            lambda *args, **kwargs: None,
        )
    )

    assert [payload["properties"] for payload in clients[0].post_payloads] == [
        {"smartMode": 1, "gridOffMode": 2},
    ]
    assert clients[1].post_payloads == []


def test_distribution_uses_node_red_weighted_redistribution_after_max_clipping() -> None:
    case = DISTRIBUTION_AFTER_MAX_CLIPPING

    assert distribute_power(
        case["total"],
        case["avail"],
        case["max_power"],
        case["balancing_factor"],
    ) == case["expected"]


def test_dual_mode_damper_keeps_single_device_power() -> None:
    state = _state(2)
    state.latest_power_cmd = -700
    state.devices[0].electric_level = 90
    state.devices[1].electric_level = 20
    state.devices_active_idx = [0]
    state.single_mode_active_device = 0
    clients = [FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(
        execute_post(
            {"properties": {"acMode": 2, "outputLimit": 900}},
            clients,
            state,
            Config(
                device_ips=["ip1", "ip2"],
                damper_enable=True,
                damper_amount=200,
                damper_timer=120,
            ),
            lambda *args, **kwargs: None,
        )
    )

    assert [client.post_payloads[0]["properties"]["outputLimit"] for client in clients] == [
        800,
        0,
    ]
