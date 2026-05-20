"""
POST handler: translate an aggregate HA command into per-device commands using
nonlinear SoC-based power distribution.

Equivalent to the Node-RED 'POST Request handling' function node.
"""

from __future__ import annotations

import asyncio
from typing import Callable

from zendure_proxy_config import Config
from zendure_proxy_device_client import DeviceClient
from zendure_proxy_health import degraded_power_by_index, eligible_device_indices
from zendure_proxy_power import apply_transition, calc_active_count, distribute_power, now
from zendure_proxy_standby import manage_standby
from zendure_proxy_state import ProxyState

_POWER_KEYS = {"acMode", "inputLimit", "outputLimit"}
_RUNTIME_MODE_KEYS = {"equalMode", "alwaysDualMode", "dualModeDamper"}


async def execute_post(
    payload: dict,
    clients: list[DeviceClient],
    state: ProxyState,
    cfg: Config,
    logger: Callable,
    *,
    is_repeat: bool = False,
) -> dict:
    """
    Translate one HA POST into individual per-device commands.

    Non-power properties (minSoc, socSet, gridOffMode, …) are forwarded
    as-is to all devices.  Power properties (acMode + inputLimit/outputLimit)
    go through the full distribution algorithm.
    """
    props = dict(payload.get("properties") or {})
    n = state.device_count
    devs = state.devices
    eligible = eligible_device_indices(state, cfg)
    eligible_set = set(eligible)
    degraded_power = degraded_power_by_index(state, cfg)
    runtime_key = _first_runtime_mode_key(props)
    if runtime_key is not None:
        _apply_runtime_mode_prop(runtime_key, props[runtime_key], state)
        return payload

    device_props = {
        key: value for key, value in props.items()
        if key not in _RUNTIME_MODE_KEYS
    }
    power_keys_present = _POWER_KEYS & set(device_props.keys())
    power_value_keys_present = {"inputLimit", "outputLimit"} & set(device_props.keys())

    # ── Non-power properties: forward to all devices ──────────────────────────
    if not power_value_keys_present:
        if not device_props:
            runtime_payload = {"properties": {
                key: props[key] for key in props if key in _RUNTIME_MODE_KEYS
            }}
            return runtime_payload if runtime_payload["properties"] else {"ack": "pong"}

        if power_keys_present == {"acMode"}:
            state.ac_mode = _int(device_props["acMode"])

        outbound_props = dict(device_props)
        _apply_aggregate_limit_props(outbound_props, state, devs, eligible)
        smart_mode_before = [dev.smart_mode for dev in devs]

        responses = []
        for i, client in enumerate(clients):
            if i not in eligible_set:
                continue
            if _suppress_standby_post(devs[i], outbound_props):
                continue
            dp = (
                {"sn": devs[i].sn, "properties": dict(outbound_props)}
                if devs[i].sn
                else {"properties": dict(outbound_props)}
            )
            responses.append(await client.post(dp))
            if "smartMode" in outbound_props:
                devs[i].smart_mode = _int(outbound_props["smartMode"])

        if _int(outbound_props.get("smartMode", -1), -1) == 1:
            passive = eligible_set - set(state.devices_active_idx)
            stamp = now()
            for idx in passive:
                if (
                    0 <= idx < len(devs)
                    and smart_mode_before[idx] == 0
                    and not devs[idx].standby_device
                ):
                    devs[idx].latest_power_cmd_zero_ts = stamp
            await manage_standby(
                state, clients, state.ac_mode, [0] * n, cfg, logger
            )

        return responses if responses else {"ack": "pong"}

    # ── Power command ─────────────────────────────────────────────────────────
    now_ts = now()
    if is_repeat:
        state.latest_power_repeat_ts = now_ts
    else:
        state.latest_power_message_ts = now_ts
        state.latest_power_repeat_ts = 0.0

    props = device_props
    input_limit: int = _int(props.get("inputLimit", 0))
    output_limit: int = _int(props.get("outputLimit", 0))
    ac_mode: int = _power_ac_mode(props, state.ac_mode)
    invalid_direction = (
        (input_limit > 0 and ac_mode == 2)
        or (output_limit > 0 and ac_mode == 1)
    )
    if invalid_direction:
        input_limit = 0
        output_limit = 0

    command_power = input_limit if ac_mode == 1 else (output_limit if ac_mode == 2 else 0)
    latest_power_cmd = _signed_power_cmd(ac_mode, command_power)
    distribution_power = _residual_power_for_healthy(latest_power_cmd, degraded_power)

    max_power = (state.max_power_in if ac_mode == 1 else state.max_power_out) or 800

    upper = cfg.single_mode_upper_pct / 100.0 * max_power
    lower = cfg.single_mode_lower_pct / 100.0 * max_power
    force_all = (
        cfg.equal_mode
        or cfg.always_dual_mode
        or state.equal_mode
        or state.always_dual_mode
    )

    if (
        not invalid_direction
        and (cfg.damper_enable or state.dualmode_damper_enabled)
        and ac_mode == 2
        and len(eligible) >= 2
        and state.latest_power_cmd != 0
    ):
        distribution_power = _apply_single_mode_damper(
            distribution_power, state, upper, now_ts, cfg
        )

    # ── How many devices active? ──────────────────────────────────────────────
    previous_active_count = state.device_active_count
    if not invalid_direction:
        state.device_active_count = calc_active_count(
            state, ac_mode, distribution_power, upper, lower, force_all,
            cfg.device_change_diff,
            eligible_indices=eligible,
        )

    # ── Which specific devices are active? ────────────────────────────────────
    prev_active_device = state.single_mode_active_device
    prev_active_idx = list(state.devices_active_idx)
    if not invalid_direction:
        _select_active_devices(
            state, ac_mode, cfg,
            previous_active_count=previous_active_count,
            eligible_indices=eligible,
        )

    if (
        state.device_active_count > 1
        and previous_active_count == 1
        and prev_active_device in eligible_set
        and state.single_to_dual_transition_start_ts <= 0
        and state.forced_dual_transition_start_ts <= 0
        and any(dev.latest_power_cmd != 0 for dev in devs)
    ):
        state.single_to_dual_transition_start_ts = now_ts
        state.single_to_dual_transition_original_device = prev_active_device

    if (
        state.device_active_count == 1
        and state.single_mode_active_device != prev_active_device
        and prev_active_device in eligible_set
        and previous_active_count == 1
        and not (
            (ac_mode == 1 and any(devs[idx].soc_limit == 1 for idx in eligible))
            or (ac_mode == 2 and any(devs[idx].soc_limit == 2 for idx in eligible))
        )
    ):
        state.transition_start_ts = now_ts
        state.transition_original_device = prev_active_device
        state.forced_dual_transition_start_ts = now_ts
        state.forced_dual_transition_original_device = prev_active_device
        state.device_active_count = min(2, len(eligible))
        state.devices_active_idx = sorted(
            {prev_active_device, state.single_mode_active_device}
        )

    # ── Per-device power distribution ─────────────────────────────────────────
    if invalid_direction:
        per_device = [0] * n
    else:
        per_device = _calc_per_device_power(
            state, ac_mode, distribution_power, max_power, cfg
        )
        per_device = apply_transition(per_device, state, now_ts, cfg.transition_timer)

    outbound_props = dict(props)
    _apply_aggregate_limit_props(outbound_props, state, devs, eligible)

    # ── Send commands to all devices in parallel ───────────────────────────────
    tasks = []
    for i, client in enumerate(clients):
        if i not in eligible_set:
            stuck_power = degraded_power.get(i, 0)
            _record_device_power_command(devs[i], stuck_power, now_ts)
            continue
        pwr = per_device[i]
        wake_standby_device = (
            not invalid_direction
            and pwr != 0
            and getattr(devs[i], "standby_device", False)
        )
        dp = _power_payload_for_device(
            props,
            ac_mode,
            pwr,
            invalid_direction,
            include_ac_mode=state.ac_mode_inconsistent or wake_standby_device,
        )
        for k, v in outbound_props.items():
            if k not in _POWER_KEYS:
                dp[k] = v
        if wake_standby_device:
            dp["acMode"] = ac_mode
            dp["smartMode"] = 1
            devs[i].smart_mode = 1
            devs[i].standby_device = False
            devs[i].latest_power_cmd_zero_ts = 0.0
        device_payload: dict = {"properties": dp}
        if devs[i].sn:
            device_payload["sn"] = devs[i].sn
        if not _suppress_standby_post(devs[i], dp):
            tasks.append(client.post(device_payload))
        _record_device_power_command(devs[i], _signed_power_cmd(ac_mode, pwr), now_ts)

    removed_active = set(prev_active_idx) - set(state.devices_active_idx)
    added_active = set(state.devices_active_idx) - set(prev_active_idx)
    for idx in removed_active:
        if 0 <= idx < len(devs):
            devs[idx].latest_power_cmd_zero_ts = now_ts
    for idx in added_active:
        if 0 <= idx < len(devs):
            devs[idx].latest_power_cmd_zero_ts = 0.0

    responses = await asyncio.gather(*tasks) if tasks else []

    # ── Update aggregate state ─────────────────────────────────────────────────
    state.ac_mode = ac_mode
    state.latest_power_cmd = latest_power_cmd
    if ac_mode == 1:
        state.input_limit = input_limit
        state.input_limit_effective = _effective_input_power(per_device, degraded_power)
    elif ac_mode == 2:
        state.output_limit = output_limit
        state.output_limit_effective = _effective_output_power(per_device, degraded_power)
    if not is_repeat:
        state.last_post_payload = payload

    asyncio.ensure_future(
        manage_standby(state, clients, ac_mode, per_device, cfg, logger)
    )

    return responses if responses else {"ack": "pong"}


# ── Internal helpers ───────────────────────────────────────────────────────────

def _power_ac_mode(props: dict, current_ac_mode: int) -> int:
    if "acMode" in props:
        return _int(props["acMode"])
    return current_ac_mode


def _record_device_power_command(dev, signed_power: int, now_ts: float) -> None:
    previous_power = dev.latest_power_cmd
    dev.latest_power_cmd = signed_power
    if signed_power == 0:
        if previous_power != 0 or dev.latest_power_cmd_zero_ts <= 0:
            dev.latest_power_cmd_zero_ts = now_ts
    else:
        dev.latest_power_cmd_zero_ts = 0.0


def _first_runtime_mode_key(props: dict) -> str | None:
    for key in ("equalMode", "alwaysDualMode", "dualModeDamper"):
        if key in props:
            return key
    return None


def _apply_runtime_mode_prop(key: str, value, state: ProxyState) -> None:
    if key == "equalMode":
        state.equal_mode = bool(_int(value))
    elif key == "alwaysDualMode":
        state.always_dual_mode = bool(_int(value))
    elif key == "dualModeDamper":
        state.dualmode_damper_enabled = bool(_int(value))


def _apply_aggregate_limit_props(
    props: dict,
    state: ProxyState,
    devs: list,
    eligible: list[int],
) -> None:
    n = len(eligible)
    if n <= 0:
        return
    if "chargeMaxLimit" in props:
        command = _int(props["chargeMaxLimit"])
        per = command // n
        state.charge_max_limit_cmd = command
        state.charge_max_limit_effective = per * n
        props["chargeMaxLimit"] = per
        for idx in eligible:
            devs[idx].charge_max_limit = per
        state.max_power_in = per
    if "inverseMaxPower" in props:
        command = _int(props["inverseMaxPower"])
        per = command // n
        state.inverse_max_power_cmd = command
        state.inverse_max_power_effective = per * n
        props["inverseMaxPower"] = per
        for idx in eligible:
            devs[idx].inverse_max_power = per
        state.max_power_out = per


def _power_payload_for_device(
    props: dict,
    ac_mode: int,
    pwr: int,
    invalid_direction: bool,
    *,
    include_ac_mode: bool = False,
) -> dict:
    dp: dict = {}
    if "acMode" in props or include_ac_mode:
        dp["acMode"] = ac_mode

    if invalid_direction:
        for key in ("inputLimit", "outputLimit"):
            if key in props:
                dp[key] = 0
        return dp

    if "inputLimit" in props:
        dp["inputLimit"] = pwr if ac_mode == 1 else 0
    elif ac_mode == 1:
        dp["inputLimit"] = pwr

    if "outputLimit" in props:
        dp["outputLimit"] = pwr if ac_mode == 2 else 0
    elif ac_mode == 2:
        dp["outputLimit"] = pwr

    if not dp:
        dp = {"inputLimit": 0, "outputLimit": 0}
    return dp


def _apply_single_mode_damper(
    power: int,
    state: ProxyState,
    upper: float,
    now_ts: float,
    cfg: Config,
) -> int:
    excess = power - upper
    if excess <= 0:
        state.dualmode_damper_active = False
        return power
    if state.device_active_count != 1:
        return power
    if excess > cfg.damper_amount:
        state.dualmode_damper_active = False
        return power
    if not state.dualmode_damper_active:
        state.dualmode_damper_active = True
        state.dualmode_damper_start_ts = now_ts
    if now_ts - state.dualmode_damper_start_ts < cfg.damper_timer:
        return round(upper)
    return power


def _signed_power_cmd(ac_mode: int, power: int) -> int:
    if ac_mode == 1:
        return power
    if ac_mode == 2:
        return -power
    return 0


def _residual_power_for_healthy(
    requested_power: int,
    degraded_power: dict[int, int],
) -> int:
    residual = requested_power - sum(degraded_power.values())
    if requested_power > 0:
        return max(0, residual)
    if requested_power < 0:
        return max(0, -residual)
    return 0


def _effective_input_power(per_device: list[int], degraded_power: dict[int, int]) -> int:
    degraded_input = sum(power for power in degraded_power.values() if power > 0)
    return sum(per_device) + degraded_input


def _effective_output_power(per_device: list[int], degraded_power: dict[int, int]) -> int:
    degraded_output = sum(-power for power in degraded_power.values() if power < 0)
    return sum(per_device) + degraded_output


def _suppress_standby_post(dev, props: dict) -> bool:
    if not getattr(dev, "standby_device", False):
        return False
    power_zero = (
        ("inputLimit" in props or "outputLimit" in props)
        and _int(props.get("inputLimit", 0)) == 0
        and _int(props.get("outputLimit", 0)) == 0
    )
    wake_only = (
        "smartMode" in props
        and "inputLimit" not in props
        and "outputLimit" not in props
        and _int(props.get("smartMode", 0)) == 1
    )
    return power_zero or wake_only


def _select_active_devices(
    state: ProxyState,
    ac_mode: int,
    cfg: Config,
    *,
    previous_active_count: int | None = None,
    eligible_indices: list[int] | None = None,
) -> None:
    """Choose which specific device indices are active based on SoC."""
    devs = state.devices
    eligible = (
        eligible_indices
        if eligible_indices is not None
        else list(range(state.device_count))
    )
    n = len(eligible)
    if n <= 0:
        state.devices_active_idx_previous = list(state.devices_active_idx)
        state.devices_active_idx = []
        state.device_active_count = 0
        return
    active_count = min(state.device_active_count, n)
    state.device_active_count = active_count
    min_soc_pct = state.min_soc / 10.0

    # Tighten hysteresis threshold near SoC boundaries
    diff_threshold = cfg.device_change_diff
    at_boundary = any(devs[i].soc_limit == 2 for i in eligible) and any(
        devs[i].electric_level < min_soc_pct for i in eligible
    )
    if at_boundary or any(devs[i].soc_limit == 1 for i in eligible):
        diff_threshold = 1

    if active_count >= n:
        state.devices_active_idx_previous = list(state.devices_active_idx)
        state.devices_active_idx = list(eligible)
        return

    previous = [idx for idx in state.devices_active_idx if idx in eligible]

    # Rank by SoC: lowest first for charging, highest first for discharging
    ranked = sorted(
        eligible,
        key=lambda i: devs[i].electric_level,
        reverse=(ac_mode == 2),
    )

    if active_count == 1:
        if n == 2 and ac_mode == 1 and sum(devs[i].soc_limit == 1 for i in eligible) == 1:
            first, second = eligible
            best = first if devs[first].soc_limit != 1 else second
            state.single_mode_active_device = best
        elif n == 2 and ac_mode == 2 and sum(devs[i].soc_limit == 2 for i in eligible) == 1:
            first, second = eligible
            best = first if devs[first].soc_limit != 2 else second
            state.single_mode_active_device = best
        else:
            best = ranked[0]
            current = state.single_mode_active_device
            if current not in eligible:
                current = best
                state.single_mode_active_device = best
            force_reselect = (
                previous_active_count is not None
                and previous_active_count != active_count
            ) or (
                n == 2 and all(devs[i].smart_mode == 0 for i in eligible)
            )
            if (
                abs(devs[best].electric_level - devs[current].electric_level)
                >= diff_threshold
                or force_reselect
            ):
                state.single_mode_active_device = best
        state.devices_active_idx = [state.single_mode_active_device]
    else:
        if n == 3:
            soc_values = [devs[i].electric_level for i in eligible]
            soc_diff = max(soc_values) - min(soc_values)
            should_reselect = (
                soc_diff >= diff_threshold
                or active_count != len(previous)
                or len(previous) != active_count
            )
            if should_reselect:
                state.devices_active_idx = ranked[:active_count]
            elif previous:
                state.devices_active_idx = previous
        else:
            state.devices_active_idx = ranked[:active_count]
    state.devices_active_idx_previous = previous


def _calc_per_device_power(
    state: ProxyState,
    ac_mode: int,
    total_power: int,
    max_power: int,
    cfg: Config,
) -> list[int]:
    """Calculate per-device power (W) using nonlinear SoC balancing."""
    devs = state.devices
    n = state.device_count
    active_idx = state.devices_active_idx
    min_soc_pct = state.min_soc / 10.0
    soc_set_pct = state.soc_set / 10.0

    avail = [0.0] * n
    for i in active_idx:
        dev = devs[i]
        if dev.soc_status == 1:
            avail[i] = 0.0
        elif ac_mode == 1:
            avail[i] = max(0.0, soc_set_pct - dev.electric_level)
        elif ac_mode == 2:
            avail[i] = max(0.0, dev.electric_level - min_soc_pct)

    active_avail = [avail[i] for i in active_idx]
    all_zero = sum(active_avail) == 0

    if cfg.equal_mode or state.equal_mode or all_zero:
        per = min(total_power // max(len(active_idx), 1), max_power)
        power_active = [per] * len(active_idx)
    else:
        power_active = distribute_power(
            total_power, active_avail, max_power, cfg.balancing_factor
        )

    # If headroom is zero for all devices, send 0 to avoid waking standby units
    if all_zero:
        power_active = [0] * len(active_idx)

    result = [0] * n
    for j, i in enumerate(active_idx):
        result[i] = power_active[j]
    return result


def _int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default
