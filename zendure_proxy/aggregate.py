from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

from .config import DeviceConfig, ProxyConfig
from .state import ProxyState
from . import __version__, metrics


@dataclass
class ReportInput:
    device: DeviceConfig
    payload: dict[str, Any]
    stale: bool
    cache_age_seconds: float | None = None


def _props(payload: dict[str, Any]) -> dict[str, Any]:
    props = payload.get("properties")
    return props if isinstance(props, dict) else {}


def _num(props: dict[str, Any], key: str, default: float = 0) -> float:
    value = props.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(props: dict[str, Any], key: str, default: int = 0) -> int:
    return int(_num(props, key, default))


def _sum(reports: list[ReportInput], key: str) -> int:
    return int(sum(_num(_props(report.payload), key) for report in reports))


def _min(reports: list[ReportInput], key: str, default: int = 0) -> int:
    values = [_num(_props(report.payload), key, math.inf) for report in reports]
    found = [value for value in values if value != math.inf]
    return int(min(found)) if found else default


def _max(reports: list[ReportInput], key: str, default: int = 0) -> int:
    values = [_num(_props(report.payload), key, -math.inf) for report in reports]
    found = [value for value in values if value != -math.inf]
    return int(max(found)) if found else default


def _avg(reports: list[ReportInput], key: str, default: int = 0) -> int:
    values = [_num(_props(report.payload), key, math.nan) for report in reports]
    found = [value for value in values if not math.isnan(value)]
    return int(sum(found) / len(found)) if found else default


def _equal_or_unknown(reports: list[ReportInput], key: str, unknown: int = -1) -> int:
    values = [_int(_props(report.payload), key, unknown) for report in reports]
    if values and all(value == values[0] for value in values):
        return values[0]
    return unknown


def build_virtual_report(
    reports: list[ReportInput],
    state: ProxyState,
    proxy_config: ProxyConfig,
) -> dict[str, Any]:
    if not reports:
        raise ValueError("at least one Zendure report is required")

    now = int(time.time())
    first = reports[0].payload
    properties: dict[str, Any] = {}
    pack_data: list[Any] = []
    product = str(first.get("product", "Zendure"))

    for report in reports:
        state.update_from_report(report.device.name, report.payload)
        pack_data.extend(report.payload.get("packData") or [])
        props = _props(report.payload)
        metrics.STATE_OF_CHARGE.labels(report.device.name).set(
            _num(props, "electricLevel", 0)
        )

    properties["ts"] = now
    properties["acMode"] = _int(_props(first), "acMode", state.ac_mode)
    state.ac_mode = properties["acMode"]
    properties["inputLimit"] = _sum(reports, "inputLimit")
    properties["outputLimit"] = _sum(reports, "outputLimit")
    properties["outputPackPower"] = _sum(reports, "outputPackPower")
    properties["packInputPower"] = _sum(reports, "packInputPower")
    properties["gridInputPower"] = _sum(reports, "gridInputPower")
    properties["outputHomePower"] = _sum(reports, "outputHomePower")
    properties["gridOffPower"] = _sum(reports, "gridOffPower")
    properties["solarInputPower"] = _sum(reports, "solarInputPower")
    properties["minSoc"] = _max(reports, "minSoc")
    properties["socSet"] = _min(reports, "socSet")
    properties["socLimit"] = _max(reports, "socLimit")
    properties["electricLevel"] = _avg(reports, "electricLevel")
    properties["smartMode"] = _min(reports, "smartMode")
    properties["BatVolt"] = _avg(reports, "BatVolt")
    properties["remainOutTime"] = _sum(reports, "remainOutTime")
    properties["hyperTmp"] = _max(reports, "hyperTmp", 2731)
    properties["chargeMaxLimit"] = _sum(reports, "chargeMaxLimit")
    properties["inverseMaxPower"] = _sum(reports, "inverseMaxPower")
    properties["packNum"] = _sum(reports, "packNum")
    properties["rssi"] = _min(reports, "rssi")
    properties["is_error"] = _max(reports, "is_error")
    properties["socStatus"] = _max(reports, "socStatus")
    properties["gridReverse"] = _avg(reports, "gridReverse")
    properties["gridOffMode"] = _max(reports, "gridOffMode", 2)

    for optional_key in ["pvStatus", "acStatus", "dcStatus"]:
        if all(optional_key in _props(report.payload) for report in reports):
            properties[optional_key] = _max(reports, optional_key)
    if all("pass" in _props(report.payload) for report in reports):
        properties["pass"] = _equal_or_unknown(reports, "pass")
    if all("batCalTime" in _props(report.payload) for report in reports):
        properties["batCalTime"] = _equal_or_unknown(reports, "batCalTime", 0)

    if proxy_config.solar_power_info:
        solar_slot = 1
        for report in reports:
            props = _props(report.payload)
            for source_slot in range(1, 5):
                properties[f"solarPower{solar_slot}"] = props.get(
                    f"solarPower{source_slot}", 0
                )
                solar_slot += 1

    output: dict[str, Any] = {
        "timestamp": now,
        "sn": f"{len(reports)}x Zendure via PROXY",
        "version": first.get("version", 2),
        "product": product,
        "proxyVersion": __version__,
        "packData": pack_data,
        "properties": properties,
    }

    properties["latestPowerCmd"] = state.latest_power_cmd
    properties["activeDeviceMask"] = state.active_mask()
    properties["activeDevice"] = state.active_mask()
    properties["equalMode"] = 1 if state.equal_mode else 0
    properties["alwaysDualMode"] = 1 if state.always_dual_mode else 0
    properties["dualModeDamper"] = 1 if state.dual_mode_damper else 0

    for index, report in enumerate(reports, start=1):
        props = _props(report.payload)
        device_state = state.devices[report.device.name]
        output[f"product_{index}"] = report.payload.get("product", "")
        output[f"sn_{index}"] = report.payload.get("sn", "")
        properties[f"socStatus_{index}"] = _int(props, "socStatus")
        properties[f"socLimit_{index}"] = _int(props, "socLimit", -1)
        properties[f"electricLevel_{index}"] = _int(props, "electricLevel")
        properties[f"latestPowerCmd_{index}"] = device_state.latest_power_cmd
        properties[f"smartMode_{index}"] = _int(props, "smartMode", -1)
        properties[f"hyperTmp_{index}"] = _int(props, "hyperTmp", 2731)
        properties[f"outputPackPower_{index}"] = _int(props, "outputPackPower")
        properties[f"packInputPower_{index}"] = _int(props, "packInputPower")
        properties[f"gridInputPower_{index}"] = _int(props, "gridInputPower")
        properties[f"outputHomePower_{index}"] = _int(props, "outputHomePower")
        properties[f"gridOffMode_{index}"] = _int(props, "gridOffMode", 2)
        properties[f"ipAddress_{index}"] = report.device.host
        properties[f"proxyDeviceStale_{index}"] = 1 if report.stale else 0
        properties[f"proxyDeviceLastSuccessAgeSeconds_{index}"] = int(
            report.cache_age_seconds or 0
        )
        if "batCalTime" in props:
            properties[f"batCalTime_{index}"] = _int(props, "batCalTime")

    return output

