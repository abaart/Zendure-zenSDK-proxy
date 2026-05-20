"""
GET handler: fetch data from all devices in parallel, update shared state,
build the merged single-device response that Home Assistant sees.

Equivalent to the Node-RED 'GET Response handling' function node.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any, Callable, Optional

from zendure_proxy_config import Config
from zendure_proxy_device_client import DeviceClient
from zendure_proxy_power import PROXY_VERSION, epoch, now
from zendure_proxy_state import ProxyState


class GatewayTimeoutError(RuntimeError):
    """Raised when strict Node-RED GET compatibility must return HTTP 504."""


async def execute_get(
    clients: list[DeviceClient],
    state: ProxyState,
    cfg: Config,
    logger: Callable,
) -> dict:
    """
    Query /properties/report on every device in parallel (each serialised by
    its own lock), update ProxyState, and return the combined response dict.

    If any device fails to respond the last cached response is returned so HA
    never receives an error.
    """
    results: list[Optional[dict]] = await asyncio.gather(
        *[c.get() for c in clients]
    )

    for i, result in enumerate(results):
        if result is None and i < len(state.counter_missing):
            state.counter_missing[i] += 1

    if any(r is None for r in results):
        logger(
            "One or more devices did not respond; returning cached response",
            level="WARNING",
        )
        if getattr(cfg, "node_red_compat_strict_get_errors", False):
            raise GatewayTimeoutError("Gateway Timeout")
        if state.last_get_response:
            return state.last_get_response
        raise RuntimeError("Devices unreachable and no cached response available")

    for i, data in enumerate(results):
        _update_device_state(i, data, state)

    response = build_combined_response(results, state, cfg)
    state.last_get_response = response
    return response


# ── State update ───────────────────────────────────────────────────────────────

def _update_device_state(idx: int, data: dict, state: ProxyState) -> None:
    """Persist per-device values from a fresh GET response into ProxyState."""
    props = data.get("properties", {})
    dev = state.devices[idx]
    dev.last_response = data
    dev.electric_level = props.get("electricLevel", dev.electric_level)
    dev.soc_status = props.get("socStatus", 0)
    dev.smart_mode = props.get("smartMode", dev.smart_mode)
    dev.soc_limit = props.get("socLimit", 0)
    dev.charge_max_limit = props.get("chargeMaxLimit", dev.charge_max_limit)
    dev.inverse_max_power = props.get("inverseMaxPower", dev.inverse_max_power)
    dev.gridoff_mode = props.get("gridOffMode", dev.gridoff_mode)
    if data.get("sn"):
        dev.sn = data["sn"]

    # Aggregate SoC limits: largest minSoc (most conservative), smallest socSet
    if idx == 0:
        state.min_soc = props.get("minSoc", state.min_soc)
        state.soc_set = props.get("socSet", state.soc_set)
    else:
        state.min_soc = max(state.min_soc, props.get("minSoc", state.min_soc))
        state.soc_set = min(state.soc_set, props.get("socSet", state.soc_set))

    # Per-device max power: smallest common limit across all devices
    chg = [d for d in state.devices if d.charge_max_limit > 0]
    if chg:
        state.max_power_in = min(d.charge_max_limit for d in chg)
    inv = [d for d in state.devices if d.inverse_max_power > 0]
    if inv:
        state.max_power_out = min(d.inverse_max_power for d in inv)


# ── Response builder ───────────────────────────────────────────────────────────

def build_combined_response(
    results: list[dict],
    state: ProxyState,
    cfg: Config,
) -> dict:
    """
    Merge N device responses into a single JSON object that looks like one
    Zendure device to Home Assistant / Gielz automation.
    """
    n = state.device_count
    devs = state.devices

    def _prop(i: int, key: str, default: Any = 0) -> Any:
        return results[i].get("properties", {}).get(key, default)

    def _sum(key: str) -> int:
        return sum(results[i].get("properties", {}).get(key, 0) for i in range(n))

    resp: dict = {
        "sn": f"{n}x Zendure via PROXY",
        "product": results[0].get("product", ""),
        "proxyVersion": PROXY_VERSION,
        "timestamp": epoch(),
        "packData": [],
        "properties": {},
    }
    for data in results:
        resp["packData"].extend(data.get("packData", []))

    props = resp["properties"]
    props["ts"] = epoch()

    # ── acMode: from active devices; flag inconsistency ────────────────────────
    ac_modes = [_prop(i, "acMode") for i in state.devices_active_idx]
    props["acMode"] = ac_modes[0] if ac_modes else 0
    state.ac_mode_inconsistent = len(set(ac_modes)) > 1

    # ── inputLimit / outputLimit: sum with command-correction ──────────────────
    input_raw = _sum("inputLimit")
    props["inputLimit"] = (
        state.input_limit
        if state.input_limit != state.input_limit_effective
        and input_raw == state.input_limit_effective
        else input_raw
    )

    output_raw = _sum("outputLimit")
    props["outputLimit"] = (
        state.output_limit
        if state.output_limit != state.output_limit_effective
        and output_raw == state.output_limit_effective
        else output_raw
    )

    inv_raw = sum(d.inverse_max_power for d in devs)
    props["inverseMaxPower"] = (
        state.inverse_max_power_cmd
        if state.inverse_max_power_cmd != state.inverse_max_power_effective
        and inv_raw == state.inverse_max_power_effective
        else inv_raw
    )

    chg_raw = sum(d.charge_max_limit for d in devs)
    props["chargeMaxLimit"] = (
        state.charge_max_limit_cmd
        if state.charge_max_limit_cmd != state.charge_max_limit_effective
        and chg_raw == state.charge_max_limit_effective
        else chg_raw
    )

    # ── outputPackPower / packInputPower: sum then conflict-resolve ────────────
    pack_out = _sum("outputPackPower")
    pack_in = _sum("packInputPower")
    if pack_in != 0 and pack_out != 0:
        if pack_in > pack_out:
            pack_in -= pack_out
            pack_out = 0
        elif pack_in < pack_out:
            pack_out -= pack_in
            pack_in = 0
        else:
            pack_in = pack_out = 0
    # Ghost-power artefact that appears in idle discharge mode
    if props["acMode"] == 2 and 0 < pack_out < 100:
        pack_out = 0
    props["outputPackPower"] = pack_out
    props["packInputPower"] = pack_in

    # ── gridInputPower / outputHomePower: same conflict-resolution ─────────────
    grid_in = _sum("gridInputPower")
    home_out = _sum("outputHomePower")
    if grid_in != 0 and home_out != 0:
        if grid_in > home_out:
            grid_in -= home_out
            home_out = 0
        elif grid_in < home_out:
            home_out -= grid_in
            grid_in = 0
        else:
            grid_in = home_out = 0
    props["gridInputPower"] = grid_in
    props["outputHomePower"] = home_out

    props["solarInputPower"] = _sum("solarInputPower")
    props["gridOffPower"] = _sum("gridOffPower")

    # ── electricLevel: average with minSoc edge-case correction ───────────────
    soc = [devs[i].electric_level for i in range(n)]
    sc = [devs[i].soc_limit for i in range(n)]
    min_soc_pct = state.min_soc / 10.0

    if n == 1:
        props["electricLevel"] = soc[0]
    elif n == 2:
        eA, eB = float(soc[0]), float(soc[1])
        # If exactly one device hit the discharge limit, clamp it to minSoc so
        # the average doesn't mislead the other device's charging behaviour.
        if (sc[0] == 2) != (sc[1] == 2):
            if sc[0] == 2:
                eA = min_soc_pct
            else:
                eB = min_soc_pct
            rnd = math.ceil if eA <= min_soc_pct + 1 and eB <= min_soc_pct + 1 else math.floor
            props["electricLevel"] = rnd((eA + eB) / 2)
        else:
            props["electricLevel"] = math.floor((eA + eB) / 2)
    else:
        corrected_soc = [float(v) for v in soc]
        limit_count = 0
        for i, limit in enumerate(sc):
            if limit == 2:
                corrected_soc[i] = min_soc_pct
                limit_count += 1
        if (
            limit_count == 2
            and all(v <= min_soc_pct + 2 for v in corrected_soc)
        ):
            props["electricLevel"] = math.ceil(sum(corrected_soc) / n)
        else:
            props["electricLevel"] = math.floor(sum(corrected_soc) / n)

    # ── SoC limits ─────────────────────────────────────────────────────────────
    props["minSoc"] = max(_prop(i, "minSoc", 100) for i in range(n))
    props["socSet"] = min(_prop(i, "socSet", 1000) for i in range(n))

    sc_vals = [_prop(i, "socLimit", 0) for i in range(n)]
    if all(v == 0 for v in sc_vals):
        props["socLimit"] = 0
    elif all(v == 1 for v in sc_vals):
        props["socLimit"] = 1
    elif all(v == 2 for v in sc_vals):
        props["socLimit"] = 2
    else:
        props["socLimit"] = 0  # mixed → treat as normal

    # ── Scalars ────────────────────────────────────────────────────────────────
    smart_modes = [_int(_prop(i, "smartMode", 0)) for i in range(n)]
    if (
        any(getattr(dev, "standby_device", False) for dev in devs)
        or _transition_recent(state, cfg)
        or state.dualmode_damper_active
    ):
        props["smartMode"] = max(smart_modes)
    else:
        props["smartMode"] = math.prod(smart_modes)

    props["hyperTmp"] = max(_prop(i, "hyperTmp", 2731) for i in range(n))
    props["BatVolt"] = math.floor(sum(_prop(i, "BatVolt", 0) for i in range(n)) / n)
    props["remainOutTime"] = math.floor(
        sum(_prop(i, "remainOutTime", 0) for i in range(n)) / n
    )
    props["packNum"] = _sum("packNum")
    props["rssi"] = min(_prop(i, "rssi", 0) for i in range(n))
    props["is_error"] = max(_prop(i, "is_error", 0) for i in range(n))
    props["socStatus"] = min(_prop(i, "socStatus", 0) for i in range(n))

    if all("gridReverse" in results[i].get("properties", {}) for i in range(n)):
        props["gridReverse"] = math.floor(
            sum(_prop(i, "gridReverse", 0) for i in range(n)) / n
        )
    if all("pass" in results[i].get("properties", {}) for i in range(n)):
        pass_values = [_prop(i, "pass", 0) for i in range(n)]
        props["pass"] = pass_values[0] if len(set(pass_values)) == 1 else -1

    for key in ("pvStatus", "acStatus", "dcStatus"):
        if all(key in results[i].get("properties", {}) for i in range(n)):
            props[key] = max(_prop(i, key, 0) for i in range(n))

    if all("batCalTime" in results[i].get("properties", {}) for i in range(n)):
        bat_cal_times = [_prop(i, "batCalTime", 0) for i in range(n)]
        props["batCalTime"] = bat_cal_times[0] if len(set(bat_cal_times)) == 1 else -1
        for i in range(3):
            props[f"batCalTime_{i+1}"] = bat_cal_times[i] if i < n else 0
    else:
        props["batCalTime"] = 0
        for i in range(3):
            props[f"batCalTime_{i+1}"] = 0

    # ── gridOffMode: majority / priority rule ──────────────────────────────────
    gom = [_prop(i, "gridOffMode", None) for i in range(n)]
    if all(v is not None for v in gom):
        cnt0, cnt1, cnt2 = gom.count(0), gom.count(1), gom.count(2)
        if cnt0 > 0:
            props["gridOffMode"] = 0
        elif cnt1 == 1 and cnt1 + cnt2 == n:
            props["gridOffMode"] = 1
        elif len(set(gom)) == 1:
            props["gridOffMode"] = gom[0]
        else:
            props["gridOffMode"] = 2
    else:
        props["gridOffMode"] = 2

    # ── Optional solar fields ──────────────────────────────────────────────────
    if cfg.solar_power_info:
        for idx in range(3):
            source = results[idx].get("properties", {}) if idx < n else {}
            target_base = idx * 6
            for source_idx in range(1, 5):
                target_key = f"solarPower{target_base + source_idx}"
                props[target_key] = source.get(f"solarPower{source_idx}", 0)

    # ── Per-device suffix fields (proxy additions _1 / _2 / _3) ───────────────
    device_power_cmds = []
    for i in range(n):
        s = f"_{i+1}"
        dp = results[i].get("properties", {})
        device_power_cmd = devs[i].latest_power_cmd or _reported_power_cmd(dp)
        device_power_cmds.append(device_power_cmd)
        resp[f"product{s}"] = results[i].get("product", "")
        resp[f"sn{s}"] = devs[i].sn
        props[f"electricLevel{s}"] = devs[i].electric_level
        props[f"latestPowerCmd{s}"] = device_power_cmd
        props[f"acMode{s}"] = dp.get("acMode", 0)
        props[f"inputLimit{s}"] = dp.get("inputLimit", 0)
        props[f"outputLimit{s}"] = dp.get("outputLimit", 0)
        props[f"outputPackPower{s}"] = dp.get("outputPackPower", 0)
        props[f"packInputPower{s}"] = dp.get("packInputPower", 0)
        props[f"outputHomePower{s}"] = dp.get("outputHomePower", 0)
        props[f"gridInputPower{s}"] = dp.get("gridInputPower", 0)
        props[f"socStatus{s}"] = devs[i].soc_status
        props[f"socLimit{s}"] = devs[i].soc_limit
        props[f"smartMode{s}"] = devs[i].smart_mode
        props[f"hyperTmp{s}"] = dp.get("hyperTmp", 2731)
        props[f"sn{s}"] = devs[i].sn
        props[f"ipAddress{s}"] = devs[i].ip
        props[f"gridOffMode{s}"] = dp.get("gridOffMode", 2)

    # Pad absent slots so HA always sees _1/_2/_3
    for i in range(n, 3):
        s = f"_{i+1}"
        resp[f"product{s}"] = ""
        resp[f"sn{s}"] = ""
        for key in (
            "electricLevel", "latestPowerCmd", "outputPackPower",
            "packInputPower", "outputHomePower", "gridInputPower",
            "inputLimit", "outputLimit", "socStatus", "smartMode",
        ):
            props[f"{key}{s}"] = 0
        props[f"acMode{s}"] = None
        props[f"socLimit{s}"] = -1
        props[f"hyperTmp{s}"] = 2731
        props[f"sn{s}"] = ""
        props[f"ipAddress{s}"] = ""
        props[f"gridOffMode{s}"] = 2

    # ── Proxy metadata ─────────────────────────────────────────────────────────
    latest_power_cmd = state.latest_power_cmd or _reported_power_cmd(props)
    props["activeDevice"] = _active_device_mask(
        state,
        latest_power_cmd=latest_power_cmd,
        device_power_cmds=device_power_cmds,
        soc_limit=props["socLimit"],
        output_pack_power=props["outputPackPower"],
        pack_input_power=props["packInputPower"],
    )
    props["equalMode"] = 1 if (state.equal_mode or cfg.equal_mode) else 0
    props["alwaysDualMode"] = 1 if (state.always_dual_mode or cfg.always_dual_mode) else 0
    props["dualModeDamper"] = 1 if (
        state.dualmode_damper_enabled or cfg.damper_enable
    ) else 0
    props["proxyVersion"] = PROXY_VERSION
    props["latestPowerCmd"] = latest_power_cmd
    props["device_active_count"] = state.device_active_count

    # Pass through any device-0 properties not explicitly handled above
    for key, val in results[0].get("properties", {}).items():
        if key not in props:
            props[key] = val

    return resp


def _active_device_mask(
    state: ProxyState,
    *,
    latest_power_cmd: int,
    device_power_cmds: list[int],
    soc_limit: int,
    output_pack_power: int,
    pack_input_power: int,
) -> int:
    charging_limit_powerzero = (
        latest_power_cmd > 0 and soc_limit == 1 and output_pack_power == 0
    )
    discharging_limit_powerzero = (
        latest_power_cmd < 0 and soc_limit == 2 and pack_input_power == 0
    )
    if latest_power_cmd == 0 or charging_limit_powerzero or discharging_limit_powerzero:
        return 0

    if state.device_count == 2 and state.device_active_count == 2:
        return 3

    active_idx = [idx for idx, cmd in enumerate(device_power_cmds[:3]) if cmd != 0]
    if not active_idx:
        active_idx = state.devices_active_idx
    if not active_idx and state.device_count:
        active_idx = [state.single_mode_active_device]
    mask = 0
    for idx in active_idx:
        if 0 <= idx < 3:
            mask |= 1 << idx
    return mask


def _transition_recent(state: ProxyState, cfg: Config) -> bool:
    current_ts = now()
    window = getattr(cfg, "transition_timer", 40) + 10
    return any(
        start > 0 and current_ts - start < window
        for start in (
            state.transition_start_ts,
            state.single_to_dual_transition_start_ts,
            state.forced_dual_transition_start_ts,
        )
    )


def _reported_power_cmd(props: dict) -> int:
    ac_mode = _int(props.get("acMode", 0))
    if ac_mode == 1:
        return max(0, _int(props.get("inputLimit", 0)))
    if ac_mode == 2:
        return -max(0, _int(props.get("outputLimit", 0)))
    return 0


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default
