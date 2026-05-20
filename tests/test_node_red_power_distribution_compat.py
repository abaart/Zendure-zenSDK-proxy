from __future__ import annotations

from zendure_proxy_power import apply_transition
from zendure_proxy_state import DeviceState, ProxyState


def test_single_device_switch_transition_uses_node_red_windows() -> None:
    state = ProxyState(
        device_count=2,
        devices=[DeviceState(ip="ip1"), DeviceState(ip="ip2")],
        transition_start_ts=100.0,
        transition_original_device=0,
        single_mode_active_device=1,
    )

    assert apply_transition([0, 1000], state, 110.0, 40) == [950, 50]
    assert apply_transition([0, 1000], state, 131.0, 40) == [750, 250]
    assert apply_transition([0, 1000], state, 141.0, 40) == [0, 1000]
    assert state.transition_start_ts == 0.0
