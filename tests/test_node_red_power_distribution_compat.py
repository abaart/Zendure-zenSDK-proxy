from __future__ import annotations

import asyncio

from zendure_proxy_config import Config
from zendure_proxy_post_handler import execute_post
from zendure_proxy_power import apply_transition, now
from zendure_proxy_state import DeviceState, ProxyState

from conftest import FakeDeviceClient


def _measured_response(
    *,
    pack_input_power: int = 0,
    output_pack_power: int = 0,
) -> dict:
    return {
        "properties": {
            "packInputPower": pack_input_power,
            "outputPackPower": output_pack_power,
        },
    }


def test_single_device_switch_transition_uses_node_red_windows() -> None:
    state = ProxyState(
        device_count=2,
        devices=[DeviceState(ip="ip1"), DeviceState(ip="ip2")],
        forced_dual_transition_start_ts=100.0,
        forced_dual_transition_original_device=0,
        single_mode_active_device=1,
    )

    assert apply_transition([0, 1000], state, 110.0, 40) == [950, 50]
    assert apply_transition([0, 1000], state, 125.0, 40) == [750, 250]
    assert apply_transition([0, 1000], state, 130.0, 40) == [500, 500]
    assert apply_transition([0, 1000], state, 135.0, 40) == [250, 750]
    assert apply_transition([0, 1000], state, 141.0, 40) == [0, 1000]
    assert state.forced_dual_transition_start_ts == 0.0


def test_single_to_dual_transition_uses_node_red_windows() -> None:
    state = ProxyState(
        device_count=2,
        devices=[DeviceState(ip="ip1"), DeviceState(ip="ip2")],
        single_to_dual_transition_start_ts=100.0,
        single_to_dual_transition_original_device=0,
    )

    assert apply_transition([500, 500], state, 110.0, 40) == [950, 50]
    assert apply_transition([500, 500], state, 131.0, 40) == [750, 250]


def test_three_device_active_selection_keeps_previous_when_soc_diff_is_small() -> None:
    state = ProxyState(
        device_count=3,
        devices=[
            DeviceState(ip="ip1", sn="SN1", electric_level=50),
            DeviceState(ip="ip2", sn="SN2", electric_level=52),
            DeviceState(ip="ip3", sn="SN3", electric_level=53),
        ],
        device_active_count=2,
        devices_active_idx=[0, 1],
        max_power_in=800,
    )
    clients = [FakeDeviceClient(), FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(
        execute_post(
            {"properties": {"acMode": 1, "inputLimit": 1000}},
            clients,
            state,
            Config(device_ips=["ip1", "ip2", "ip3"], device_change_diff=5),
            lambda *args, **kwargs: None,
        )
    )

    assert state.devices_active_idx == [0, 1]


def test_three_device_high_soc_holds_high_device_at_zero_until_capacity_requires_it() -> None:
    state = ProxyState(
        device_count=3,
        devices=[
            DeviceState(ip="ip1", sn="SN1", electric_level=91),
            DeviceState(ip="ip2", sn="SN2", electric_level=70),
            DeviceState(ip="ip3", sn="SN3", electric_level=70),
        ],
        device_active_count=1,
        devices_active_idx=[0],
        max_power_in=800,
    )
    clients = [FakeDeviceClient(), FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(
        execute_post(
            {"properties": {"acMode": 1, "inputLimit": 1500}},
            clients,
            state,
            Config(device_ips=["ip1", "ip2", "ip3"], device_change_diff=5),
            lambda *args, **kwargs: None,
        )
    )

    assert state.device_active_count == 2
    assert state.devices_active_idx == [1, 2]
    assert [client.post_payloads[0]["properties"]["inputLimit"] for client in clients] == [
        0,
        750,
        750,
    ]


def test_three_device_high_soc_uses_high_device_when_other_devices_are_full() -> None:
    state = ProxyState(
        device_count=3,
        devices=[
            DeviceState(ip="ip1", sn="SN1", electric_level=91),
            DeviceState(ip="ip2", sn="SN2", electric_level=70),
            DeviceState(ip="ip3", sn="SN3", electric_level=70),
        ],
        device_active_count=1,
        devices_active_idx=[0],
        max_power_in=800,
    )
    clients = [FakeDeviceClient(), FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(
        execute_post(
            {"properties": {"acMode": 1, "inputLimit": 1700}},
            clients,
            state,
            Config(device_ips=["ip1", "ip2", "ip3"], device_change_diff=5),
            lambda *args, **kwargs: None,
        )
    )

    assert state.device_active_count == 3
    assert state.devices_active_idx == [1, 2, 0]
    assert [client.post_payloads[0]["properties"]["inputLimit"] for client in clients] == [
        131,
        785,
        784,
    ]


def test_three_device_high_soc_uses_measured_charge_shortfall_before_held_device() -> None:
    previous_ts = now() - 20
    fresh_get_ts = now() - 10
    state = ProxyState(
        device_count=3,
        devices=[
            DeviceState(
                ip="ip1",
                sn="SN1",
                electric_level=91,
                latest_power_cmd=0,
                last_successful_get_ts=fresh_get_ts,
                last_response=_measured_response(pack_input_power=0),
            ),
            DeviceState(
                ip="ip2",
                sn="SN2",
                electric_level=70,
                latest_power_cmd=425,
                last_successful_get_ts=fresh_get_ts,
                last_response=_measured_response(pack_input_power=50),
            ),
            DeviceState(
                ip="ip3",
                sn="SN3",
                electric_level=70,
                latest_power_cmd=425,
                last_successful_get_ts=fresh_get_ts,
                last_response=_measured_response(pack_input_power=425),
            ),
        ],
        device_active_count=1,
        devices_active_idx=[0],
        max_power_in=800,
        latest_power_message_ts=previous_ts,
    )
    clients = [FakeDeviceClient(), FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(
        execute_post(
            {"properties": {"acMode": 1, "inputLimit": 850}},
            clients,
            state,
            Config(device_ips=["ip1", "ip2", "ip3"], device_change_diff=5),
            lambda *args, **kwargs: None,
        )
    )

    assert state.device_active_count == 2
    assert state.devices_active_idx == [1, 2]
    assert [client.post_payloads[0]["properties"]["inputLimit"] for client in clients] == [
        0,
        425,
        800,
    ]


def test_three_device_low_soc_discharge_does_not_use_measured_output_shortfall() -> None:
    previous_ts = now() - 20
    fresh_get_ts = now() - 10
    state = ProxyState(
        device_count=3,
        devices=[
            DeviceState(
                ip="ip1",
                sn="SN1",
                electric_level=9,
                latest_power_cmd=0,
                last_successful_get_ts=fresh_get_ts,
                last_response=_measured_response(output_pack_power=0),
            ),
            DeviceState(
                ip="ip2",
                sn="SN2",
                electric_level=35,
                latest_power_cmd=-425,
                last_successful_get_ts=fresh_get_ts,
                last_response=_measured_response(output_pack_power=50),
            ),
            DeviceState(
                ip="ip3",
                sn="SN3",
                electric_level=35,
                latest_power_cmd=-425,
                last_successful_get_ts=fresh_get_ts,
                last_response=_measured_response(output_pack_power=425),
            ),
        ],
        device_active_count=1,
        devices_active_idx=[0],
        max_power_out=800,
        min_soc=100,
        latest_power_message_ts=previous_ts,
    )
    clients = [FakeDeviceClient(), FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(
        execute_post(
            {"properties": {"acMode": 2, "outputLimit": 850}},
            clients,
            state,
            Config(device_ips=["ip1", "ip2", "ip3"], device_change_diff=5),
            lambda *args, **kwargs: None,
        )
    )

    assert state.device_active_count == 2
    assert state.devices_active_idx == [1, 2]
    assert [client.post_payloads[0]["properties"]["outputLimit"] for client in clients] == [
        0,
        425,
        425,
    ]


def test_three_device_low_soc_charge_does_not_use_measured_charge_shortfall() -> None:
    previous_ts = now() - 20
    fresh_get_ts = now() - 10
    state = ProxyState(
        device_count=3,
        devices=[
            DeviceState(
                ip="ip1",
                sn="SN1",
                electric_level=8,
                latest_power_cmd=425,
                last_successful_get_ts=fresh_get_ts,
                last_response=_measured_response(pack_input_power=50),
            ),
            DeviceState(
                ip="ip2",
                sn="SN2",
                electric_level=8,
                latest_power_cmd=425,
                last_successful_get_ts=fresh_get_ts,
                last_response=_measured_response(pack_input_power=425),
            ),
            DeviceState(ip="ip3", sn="SN3", electric_level=9),
        ],
        device_active_count=3,
        devices_active_idx=[0, 1, 2],
        max_power_in=800,
        min_soc=100,
        latest_power_message_ts=previous_ts,
    )
    clients = [FakeDeviceClient(), FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(
        execute_post(
            {"properties": {"acMode": 1, "inputLimit": 850}},
            clients,
            state,
            Config(device_ips=["ip1", "ip2", "ip3"], device_change_diff=5),
            lambda *args, **kwargs: None,
        )
    )

    assert state.device_active_count == 3
    assert state.devices_active_idx == [0, 1, 2]
    assert [client.post_payloads[0]["properties"]["inputLimit"] for client in clients] == [
        320,
        320,
        210,
    ]


def test_three_device_low_soc_charge_spread_above_one_percent_uses_lowest_only() -> None:
    previous_ts = now() - 20
    fresh_get_ts = now() - 10
    state = ProxyState(
        device_count=3,
        devices=[
            DeviceState(
                ip="ip1",
                sn="SN1",
                electric_level=4,
                latest_power_cmd=150,
                last_successful_get_ts=fresh_get_ts,
                last_response=_measured_response(pack_input_power=50),
            ),
            DeviceState(ip="ip2", sn="SN2", electric_level=8),
            DeviceState(ip="ip3", sn="SN3", electric_level=9),
        ],
        device_active_count=3,
        devices_active_idx=[0, 1, 2],
        max_power_in=800,
        min_soc=100,
        latest_power_message_ts=previous_ts,
    )
    clients = [FakeDeviceClient(), FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(
        execute_post(
            {"properties": {"acMode": 1, "inputLimit": 150}},
            clients,
            state,
            Config(device_ips=["ip1", "ip2", "ip3"], device_change_diff=5),
            lambda *args, **kwargs: None,
        )
    )

    assert state.device_active_count == 1
    assert state.devices_active_idx == [0]
    assert [client.post_payloads[0]["properties"]["inputLimit"] for client in clients] == [
        150,
        0,
        0,
    ]


def test_three_device_high_soc_low_power_adds_device_after_measured_shortfall() -> None:
    previous_ts = now() - 20
    fresh_get_ts = now() - 10
    state = ProxyState(
        device_count=3,
        devices=[
            DeviceState(ip="ip1", sn="SN1", electric_level=91),
            DeviceState(
                ip="ip2",
                sn="SN2",
                electric_level=70,
                latest_power_cmd=150,
                last_successful_get_ts=fresh_get_ts,
                last_response=_measured_response(pack_input_power=50),
            ),
            DeviceState(ip="ip3", sn="SN3", electric_level=71),
        ],
        device_active_count=1,
        devices_active_idx=[1],
        single_mode_active_device=1,
        max_power_in=800,
        latest_power_message_ts=previous_ts,
    )
    clients = [FakeDeviceClient(), FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(
        execute_post(
            {"properties": {"acMode": 1, "inputLimit": 150}},
            clients,
            state,
            Config(device_ips=["ip1", "ip2", "ip3"], device_change_diff=5),
            lambda *args, **kwargs: None,
        )
    )

    assert state.device_active_count == 2
    assert state.devices_active_idx == [1, 2]
    assert [client.post_payloads[0]["properties"]["inputLimit"] for client in clients] == [
        0,
        150,
        100,
    ]


def test_three_device_high_soc_low_power_uses_one_device() -> None:
    state = ProxyState(
        device_count=3,
        devices=[
            DeviceState(ip="ip1", sn="SN1", electric_level=91),
            DeviceState(ip="ip2", sn="SN2", electric_level=70),
            DeviceState(ip="ip3", sn="SN3", electric_level=71),
        ],
        device_active_count=3,
        devices_active_idx=[0, 1, 2],
        single_mode_active_device=2,
        max_power_in=800,
    )
    clients = [FakeDeviceClient(), FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(
        execute_post(
            {"properties": {"acMode": 1, "inputLimit": 150}},
            clients,
            state,
            Config(device_ips=["ip1", "ip2", "ip3"], device_change_diff=5),
            lambda *args, **kwargs: None,
        )
    )

    assert state.device_active_count == 1
    assert state.devices_active_idx == [1]
    assert [client.post_payloads[0]["properties"]["inputLimit"] for client in clients] == [
        0,
        150,
        0,
    ]


def test_three_device_high_soc_mid_power_uses_two_devices_above_minimum() -> None:
    state = ProxyState(
        device_count=3,
        devices=[
            DeviceState(ip="ip1", sn="SN1", electric_level=91),
            DeviceState(ip="ip2", sn="SN2", electric_level=70),
            DeviceState(ip="ip3", sn="SN3", electric_level=71),
        ],
        device_active_count=3,
        devices_active_idx=[0, 1, 2],
        single_mode_active_device=2,
        max_power_in=800,
    )
    clients = [FakeDeviceClient(), FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(
        execute_post(
            {"properties": {"acMode": 1, "inputLimit": 250}},
            clients,
            state,
            Config(device_ips=["ip1", "ip2", "ip3"], device_change_diff=5),
            lambda *args, **kwargs: None,
        )
    )

    assert state.device_active_count == 2
    assert state.devices_active_idx == [1, 2]
    assert [client.post_payloads[0]["properties"]["inputLimit"] for client in clients] == [
        0,
        133,
        117,
    ]
