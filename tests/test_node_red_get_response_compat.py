from __future__ import annotations

from zendure_proxy_config import Config
from zendure_proxy_get_handler import _update_device_state, build_combined_response
from zendure_proxy_ha_sensors import build_proxy_ha_sensors
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


def test_three_device_grid_off_mode_matches_node_red_two_eco_devices() -> None:
    results = [
        device_response(1, "SN1", properties={"gridOffMode": 1}),
        device_response(2, "SN2", properties={"gridOffMode": 1}),
        device_response(3, "SN3", properties={"gridOffMode": 2}),
    ]

    response = build_combined_response(
        results,
        _state_for(results),
        Config(device_ips=["ip1", "ip2", "ip3"]),
    )

    assert response["properties"]["gridOffMode"] == 1
    assert response["properties"]["gridOffMode_1"] == 1
    assert response["properties"]["gridOffMode_2"] == 1
    assert response["properties"]["gridOffMode_3"] == 2


def test_absent_devices_use_node_red_unknown_smart_mode_value() -> None:
    results = [device_response(1, "SN1", properties={"smartMode": 1})]

    response = build_combined_response(
        results,
        _state_for(results),
        Config(device_ips=["ip1"]),
    )

    assert response["properties"]["smartMode_1"] == 1
    assert response["properties"]["smartMode_2"] == -1
    assert response["properties"]["smartMode_3"] == -1


def test_ten_device_combined_response_and_proxy_sensors_use_ten_bit_masks() -> None:
    results = [
        device_response(idx, f"SN{idx}", properties={"inputLimit": 100})
        for idx in range(1, 11)
    ]
    state = _state_for(results, active_idx=list(range(10)))
    state.device_active_count = 10
    state.latest_power_cmd = 1000
    for dev in state.devices:
        dev.latest_power_cmd = 100
    state.devices[9].configured_charge_max_watts = 500

    response = build_combined_response(
        results,
        state,
        Config(device_ips=[f"ip{idx}" for idx in range(1, 11)]),
    )
    sensors = build_proxy_ha_sensors(response)

    assert response["sn"] == "10x Zendure via PROXY"
    assert response["sn_10"] == "SN10"
    assert response["properties"]["activeDevice"] == 1023
    assert response["properties"]["latestPowerCmd_10"] == 100
    assert response["properties"]["effectiveChargeMax_10"] == 500
    assert sensors["sensor.zendure_10_health"][0] == "Healthy"
    assert sensors["sensor.zendure_actief_device"][0] == "Alle"


def test_anti_pingpong_metadata_reports_net_and_device_power() -> None:
    results = [
        device_response(1, "SN1", properties={"acMode": 1, "inputLimit": 530}),
        device_response(2, "SN2", properties={"acMode": 2, "outputLimit": 30}),
    ]
    state = _state_for(results, active_idx=[0])
    state.anti_pingpong_active = True
    state.anti_pingpong_service_idx = [0]
    state.anti_pingpong_reserve_idx = [1]
    state.anti_pingpong_paused_idx = [1]
    state.anti_pingpong_reserve_power_watts = 30
    state.anti_pingpong_grid_power_entity_resolved = "sensor.homewizard_p1_vermogen"
    state.anti_pingpong_grid_power_entity_source = "sensor.homewizard_p1_vermogen"
    state.anti_pingpong_smart_gain_kwh = 0.001
    state.anti_pingpong_smart_loss_kwh = 0.0002
    state.anti_pingpong_smart_net_eur = 0.00024
    state.input_limit = 500
    state.input_limit_effective = 500
    state.latest_power_cmd = 500
    state.devices[0].latest_power_cmd = 530
    state.devices[1].latest_power_cmd = -30

    response = build_combined_response(
        results,
        state,
        Config(
            device_ips=["ip1", "ip2"],
            anti_pingpong_enable=True,
            anti_pingpong_activation_mode="smart",
        ),
    )

    props = response["properties"]
    assert props["inputLimit"] == 500
    assert props["outputLimit"] == 0
    assert props["latestPowerCmd_1"] == 530
    assert props["latestPowerCmd_2"] == -30
    assert props["antiPingpongActive"] == 1
    assert props["antiPingpongReserveDevice"] == 2
    assert props["antiPingpongDelayedDevice"] == 2
    assert props["antiPingpongModeSwitchDelaySeconds"] == 30
    assert props["antiPingpongModeSwitchDominanceWindowSeconds"] == 120
    assert props["antiPingpongGridPowerEntity"] == "sensor.homewizard_p1_vermogen"
    assert props["antiPingpongSmartGainKwh"] == 0.001
    assert props["antiPingpongSmartLossKwh"] == 0.0002
    assert props["antiPingpongSmartNetEur"] == 0.00024
