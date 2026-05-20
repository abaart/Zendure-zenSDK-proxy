from __future__ import annotations

from zendure_proxy_config import Config
from zendure_proxy_get_handler import _update_device_state, build_combined_response
from zendure_proxy_power import now
from zendure_proxy_state import DeviceState, ProxyState

from conftest import device_response
from node_red_expected import (
    THREE_DEVICE_ELECTRIC_LEVEL_WITH_TWO_EMPTY,
    TWO_DEVICE_AGGREGATES,
)


def _state_for(results: list[dict], active_idx: list[int] | None = None) -> ProxyState:
    state = ProxyState(
        device_count=len(results),
        devices=[
            DeviceState(ip=f"ip{idx + 1}", sn=result["sn"])
            for idx, result in enumerate(results)
        ],
    )
    state.devices_active_idx = active_idx or list(range(len(results)))
    state.device_active_count = len(state.devices_active_idx)
    for idx, result in enumerate(results):
        _update_device_state(idx, result, state)
    return state


def test_combined_response_uses_node_red_aggregate_rules_for_two_devices() -> None:
    results = [
        device_response(
            1,
            "SN1",
            properties={
                "BatVolt": 5200,
                "remainOutTime": 100,
                "hyperTmp": 2741,
                "socStatus": 0,
                "gridReverse": 0,
                "pass": 1,
                "batCalTime": 123,
                "pvStatus": 0,
                "acStatus": 1,
                "dcStatus": 0,
            },
        ),
        device_response(
            2,
            "SN2",
            properties={
                "BatVolt": 5300,
                "remainOutTime": 200,
                "hyperTmp": 2761,
                "socStatus": 1,
                "gridReverse": 2,
                "pass": 2,
                "batCalTime": 124,
                "pvStatus": 2,
                "acStatus": 0,
                "dcStatus": 3,
            },
        ),
    ]
    response = build_combined_response(
        results,
        _state_for(results),
        Config(device_ips=["ip1", "ip2"], solar_power_info=True),
    )

    assert response["sn"] == "2x Zendure via PROXY"
    assert response["sn_1"] == "SN1"
    assert response["sn_2"] == "SN2"
    assert response["sn_3"] == ""
    assert response["product"] == "Product 1"
    assert response["product_1"] == "Product 1"
    assert response["product_2"] == "Product 2"
    assert response["product_3"] == ""
    assert len(response["packData"]) == 2
    assert "timestamp" in response
    assert "proxyVersion" in response

    for key, value in TWO_DEVICE_AGGREGATES.items():
        assert response["properties"][key] == value

    for key in (
        "latestPowerCmd_1",
        "latestPowerCmd_2",
        "socLimit_1",
        "socLimit_2",
        "gridOffMode_1",
        "gridOffMode_2",
        "ipAddress_1",
        "ipAddress_2",
        "outputPackPower_1",
        "packInputPower_1",
        "gridInputPower_1",
        "outputHomePower_1",
    ):
        assert key in response["properties"]


def test_three_device_electric_level_matches_node_red_empty_limit_correction() -> None:
    results = [
        device_response(1, "SN1", properties={"electricLevel": 8, "socLimit": 2}),
        device_response(2, "SN2", properties={"electricLevel": 9, "socLimit": 2}),
        device_response(3, "SN3", properties={"electricLevel": 11, "socLimit": 0}),
    ]

    response = build_combined_response(
        results,
        _state_for(results),
        Config(device_ips=["ip1", "ip2", "ip3"]),
    )

    assert response["properties"]["electricLevel"] == (
        THREE_DEVICE_ELECTRIC_LEVEL_WITH_TWO_EMPTY
    )


def test_smart_mode_uses_max_when_a_device_is_in_proxy_standby() -> None:
    results = [
        device_response(1, "SN1", properties={"smartMode": 1}),
        device_response(2, "SN2", properties={"smartMode": 0}),
        device_response(3, "SN3", properties={"smartMode": 1}),
    ]
    state = _state_for(results, active_idx=[0, 2])
    state.devices[1].standby_device = True

    response = build_combined_response(
        results,
        state,
        Config(device_ips=["ip1", "ip2", "ip3"]),
    )

    assert response["properties"]["smartMode"] == 1


def test_smart_mode_uses_max_during_single_to_dual_transition_window() -> None:
    results = [
        device_response(1, "SN1", properties={"smartMode": 1}),
        device_response(2, "SN2", properties={"smartMode": 0}),
    ]
    state = _state_for(results, active_idx=[0, 1])
    state.device_active_count = 2
    state.single_to_dual_transition_start_ts = now()

    response = build_combined_response(
        results,
        state,
        Config(device_ips=["ip1", "ip2"]),
    )

    assert response["properties"]["smartMode"] == 1


def test_two_device_dual_mode_reports_both_active_even_with_one_zero_command() -> None:
    results = [
        device_response(1, "SN1"),
        device_response(2, "SN2"),
    ]
    state = _state_for(results, active_idx=[0, 1])
    state.device_active_count = 2
    state.latest_power_cmd = 500
    state.devices[0].latest_power_cmd = 500
    state.devices[1].latest_power_cmd = 0

    response = build_combined_response(
        results,
        state,
        Config(device_ips=["ip1", "ip2"]),
    )

    assert response["properties"]["activeDevice"] == 3
