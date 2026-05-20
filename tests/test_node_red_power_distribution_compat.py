from __future__ import annotations

import asyncio

from zendure_proxy_config import Config
from zendure_proxy_post_handler import execute_post
from zendure_proxy_power import apply_transition
from zendure_proxy_state import DeviceState, ProxyState

from conftest import FakeDeviceClient


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
