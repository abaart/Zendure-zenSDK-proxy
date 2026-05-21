"""
POST handler: translate an aggregate HA command into per-device commands using
nonlinear SoC-based power distribution.

Equivalent to the Node-RED 'POST Request handling' function node.
"""

from __future__ import annotations

import asyncio
from typing import Callable

from zendure_proxy_anti_pingpong import (
    activation_mode,
    apply_mode_switch_delay,
    clear_command_state,
    dominant_power_sign,
    record_power_direction,
    select_anti_pingpong_split,
    threshold_active,
)
from zendure_proxy_config import Config
from zendure_proxy_device_client import DeviceClient
from zendure_proxy_health import eligible_device_indices
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
    distribution_power = command_power
    record_power_direction(state, cfg, latest_power_cmd, is_repeat, now_ts)

    max_power = (state.max_power_in if ac_mode == 1 else state.max_power_out) or 800

    upper = cfg.single_mode_upper_pct / 100.0 * max_power
    lower = cfg.single_mode_lower_pct / 100.0 * max_power
    force_all = (
        cfg.equal_mode
        or cfg.always_dual_mode
        or state.equal_mode
        or state.always_dual_mode
    )
    anti_activation_active = _anti_pingpong_activation_active(
        state, cfg, latest_power_cmd, invalid_direction, force_all, now_ts
    )

    if (
        not invalid_direction
        and not anti_activation_active
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
        and any(devs[idx].latest_power_cmd != 0 for idx in eligible)
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
    anti_payloads: dict[int, dict] | None = None
    anti_abs_power: list[int] = [0] * n
    anti_signed_power: list[int] = [0] * n
    if anti_activation_active:
        split = select_anti_pingpong_split(
            state, cfg, ac_mode, eligible, distribution_power, max_power
        )
        if split.active:
            state.anti_pingpong_service_idx = list(split.service_idx)
            state.anti_pingpong_reserve_idx = list(split.reserve_idx)
            state.anti_pingpong_reserve_power_watts = sum(
                split.reserve_power_by_idx.values()
            )
            state.anti_pingpong_last_reason = split.reason
            state.devices_active_idx = list(split.service_idx)
            state.device_active_count = len(split.service_idx)
            per_device = _calc_per_device_power(
                state, ac_mode, split.service_power, max_power, cfg
            )
            anti_payloads = _anti_pingpong_payloads(
                state,
                cfg,
                ac_mode,
                per_device,
                split.reserve_power_by_idx,
                now_ts,
            )
            anti_signed_power = [
                _payload_signed_power(anti_payloads.get(i, {}))
                for i in range(n)
            ]
            anti_abs_power = [abs(power) for power in anti_signed_power]
        else:
            clear_command_state(state)
            state.anti_pingpong_last_reason = split.reason
    else:
        clear_command_state(state)

    if anti_payloads is None:
        anti_payloads = _dominant_mode_delay_payloads(
            state, cfg, ac_mode, per_device, invalid_direction, force_all, now_ts
        )
        if anti_payloads:
            anti_signed_power = [
                _payload_signed_power(anti_payloads.get(i, {}))
                for i in range(n)
            ]
            anti_abs_power = [abs(power) for power in anti_signed_power]

    desired_signed_power = _desired_signed_power_by_device(
        ac_mode, per_device, anti_payloads, anti_signed_power
    )
    relay_payloads = _relay_saver_payloads(
        state, cfg, desired_signed_power, anti_payloads, invalid_direction, now_ts
    )
    relay_signed_power = [
        _payload_signed_power(relay_payloads.get(i, {}))
        for i in range(n)
    ]
    relay_abs_power = [abs(power) for power in relay_signed_power]

    # ── Send commands to all devices in parallel ───────────────────────────────
    tasks = []
    for i, client in enumerate(clients):
        if i not in eligible_set:
            _record_device_power_command(devs[i], 0, now_ts)
            continue
        anti_owns_device = anti_payloads is not None and i in anti_payloads
        relay_owns_device = i in relay_payloads
        if anti_owns_device:
            pwr = anti_abs_power[i]
        elif relay_owns_device:
            pwr = relay_abs_power[i]
        elif anti_payloads is not None:
            pwr = anti_abs_power[i]
        else:
            pwr = per_device[i]
        wake_standby_device = (
            not invalid_direction
            and (
                pwr != 0
                or i in state.anti_pingpong_reserve_idx
                or i in state.anti_pingpong_paused_idx
                or i in state.relay_saver_paused_idx
            )
            and getattr(devs[i], "standby_device", False)
        )
        if anti_owns_device:
            dp = dict(anti_payloads[i])
        elif relay_owns_device:
            dp = dict(relay_payloads[i])
        else:
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
            dp["smartMode"] = 1
            devs[i].smart_mode = 1
            devs[i].standby_device = False
            devs[i].latest_power_cmd_zero_ts = 0.0
        _record_ac_mode_command(devs[i], dp, now_ts)
        device_payload: dict = {"properties": dp}
        if devs[i].sn:
            device_payload["sn"] = devs[i].sn
        if not _suppress_standby_post(devs[i], dp):
            tasks.append(client.post(device_payload))
        if anti_owns_device:
            signed_power = anti_signed_power[i]
        elif relay_owns_device:
            signed_power = relay_signed_power[i]
        else:
            signed_power = _signed_power_cmd(ac_mode, pwr)
        _record_device_power_command(devs[i], signed_power, now_ts)

    removed_active = set(prev_active_idx) - set(state.devices_active_idx)
    added_active = set(state.devices_active_idx) - set(prev_active_idx)
    for idx in removed_active:
        if 0 <= idx < len(devs) and idx not in state.relay_saver_paused_idx:
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
        state.input_limit_effective = input_limit if anti_payloads is not None else sum(per_device)
    elif ac_mode == 2:
        state.output_limit = output_limit
        state.output_limit_effective = output_limit if anti_payloads is not None else sum(per_device)
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


def _anti_pingpong_activation_active(
    state: ProxyState,
    cfg: Config,
    latest_power_cmd: int,
    invalid_direction: bool,
    force_all: bool,
    now_ts: float,
) -> bool:
    if (
        invalid_direction
        or force_all
        or not getattr(cfg, "anti_pingpong_enable", False)
        or latest_power_cmd == 0
    ):
        return False
    if activation_mode(cfg) == "smart":
        return bool(state.anti_pingpong_active)
    return threshold_active(state, cfg, latest_power_cmd, now_ts)


def _anti_pingpong_payloads(
    state: ProxyState,
    cfg: Config,
    ac_mode: int,
    per_device: list[int],
    reserve_power_by_idx: dict[int, int],
    now_ts: float,
) -> dict[int, dict]:
    desired: dict[int, dict] = {}
    for idx in state.anti_pingpong_service_idx:
        desired[idx] = _full_power_payload(ac_mode, per_device[idx])
    reserve_ac_mode = 2 if ac_mode == 1 else 1
    for idx, reserve_power in reserve_power_by_idx.items():
        desired[idx] = _full_power_payload(reserve_ac_mode, reserve_power)
    return apply_mode_switch_delay(state, cfg, desired, now_ts)


def _dominant_mode_delay_payloads(
    state: ProxyState,
    cfg: Config,
    ac_mode: int,
    per_device: list[int],
    invalid_direction: bool,
    force_all: bool,
    now_ts: float,
) -> dict[int, dict] | None:
    if (
        invalid_direction
        or force_all
        or not getattr(cfg, "anti_pingpong_enable", False)
        or getattr(state, "anti_pingpong_active", False)
        or ac_mode not in (1, 2)
    ):
        return None

    desired_sign = 1 if ac_mode == 1 else -1
    dominant_sign = dominant_power_sign(state, cfg, now_ts)
    if dominant_sign == 0 or dominant_sign == desired_sign:
        return None

    delay_ac_mode = 1 if dominant_sign > 0 else 2
    delay_power = max(0, int(getattr(cfg, "anti_pingpong_reserve_power_watts", 30)))
    payloads: dict[int, dict] = {}
    delayed_idx: list[int] = []
    for idx, power in enumerate(per_device):
        if power <= 0:
            continue
        payloads[idx] = _delay_payload_for_device(
            delay_ac_mode,
            state.devices[idx].soc_limit,
            delay_power,
        )
        delayed_idx.append(idx)

    if not payloads:
        return None

    state.anti_pingpong_paused_idx = delayed_idx
    state.anti_pingpong_last_reason = (
        "dominant_charge_delay" if dominant_sign > 0 else "dominant_discharge_delay"
    )
    return payloads


def _full_power_payload(ac_mode: int, power: int) -> dict:
    if ac_mode == 1:
        return {"acMode": 1, "inputLimit": power, "outputLimit": 0}
    if ac_mode == 2:
        return {"acMode": 2, "inputLimit": 0, "outputLimit": power}
    return {"acMode": 0, "inputLimit": 0, "outputLimit": 0}


def _delay_payload_for_device(ac_mode: int, soc_limit: int, power: int) -> dict:
    if ac_mode == 1:
        return {"acMode": 1, "inputLimit": 0 if soc_limit == 1 else power, "outputLimit": 0}
    if ac_mode == 2:
        return {"acMode": 2, "inputLimit": 0, "outputLimit": 0 if soc_limit == 2 else power}
    return {"acMode": 0, "inputLimit": 0, "outputLimit": 0}


def _payload_signed_power(payload: dict) -> int:
    ac_mode = _int(payload.get("acMode", 0))
    if ac_mode == 1:
        return max(0, _int(payload.get("inputLimit", 0)))
    if ac_mode == 2:
        return -max(0, _int(payload.get("outputLimit", 0)))
    return 0


def _desired_signed_power_by_device(
    ac_mode: int,
    per_device: list[int],
    anti_payloads: dict[int, dict] | None,
    anti_signed_power: list[int],
) -> list[int]:
    if anti_payloads is not None:
        return [
            anti_signed_power[idx] if idx in anti_payloads else 0
            for idx in range(len(per_device))
        ]
    return [_signed_power_cmd(ac_mode, power) for power in per_device]


def _relay_saver_payloads(
    state: ProxyState,
    cfg: Config,
    desired_signed_power: list[int],
    anti_payloads: dict[int, dict] | None,
    invalid_direction: bool,
    now_ts: float,
) -> dict[int, dict]:
    state.relay_saver_paused_idx = []
    if invalid_direction or not getattr(cfg, "relay_saver_enable", False):
        _clear_relay_saver_state(state)
        return {}

    anti_owned = set(anti_payloads or {})
    min_drop = max(0, int(getattr(cfg, "relay_saver_min_drop_watts", 900)))
    min_power = max(0, int(getattr(cfg, "relay_saver_min_power_watts", 30)))
    hold_seconds = max(0.0, float(getattr(cfg, "relay_saver_hold_seconds", 30)))
    payloads: dict[int, dict] = {}
    paused_idx: list[int] = []
    reason = ""

    for idx in list(getattr(state, "relay_saver_until_ts_by_idx", {}).keys()):
        if idx < 0 or idx >= state.device_count:
            _clear_relay_saver_device(state, idx)

    for idx in range(state.device_count):
        if idx in anti_owned:
            _clear_relay_saver_device(state, idx)
            continue

        desired = desired_signed_power[idx] if idx < len(desired_signed_power) else 0
        desired_sign = _power_sign(desired)
        until_ts = state.relay_saver_until_ts_by_idx.get(idx, 0.0)
        hold_sign = state.relay_saver_sign_by_idx.get(idx, 0)
        expired_hold = False

        if until_ts > now_ts and hold_sign != 0:
            if desired_sign == hold_sign and abs(desired) > min_power:
                _clear_relay_saver_device(state, idx)
                continue
            payloads[idx] = _relay_saver_payload_for_sign(
                hold_sign, state.devices[idx].soc_limit, min_power
            )
            paused_idx.append(idx)
            reason = reason or "hold_active"
            continue

        if until_ts > 0:
            _clear_relay_saver_device(state, idx)
            reason = reason or "expired"
            expired_hold = True

        previous = _int(getattr(state.devices[idx], "latest_power_cmd", 0))
        previous_sign = _power_sign(previous)
        if expired_hold or previous_sign == 0:
            continue

        crosses_zero = desired_sign == 0 or desired_sign != previous_sign
        if not crosses_zero or abs(previous - desired) < min_drop:
            continue

        if hold_seconds <= 0:
            continue

        state.relay_saver_until_ts_by_idx[idx] = now_ts + hold_seconds
        state.relay_saver_sign_by_idx[idx] = previous_sign
        payloads[idx] = _relay_saver_payload_for_sign(
            previous_sign, state.devices[idx].soc_limit, min_power
        )
        paused_idx.append(idx)
        reason = (
            "large_drop_to_zero"
            if desired_sign == 0
            else "large_sign_change"
        )

    state.relay_saver_paused_idx = paused_idx
    active_until = any(
        until_ts > now_ts
        for until_ts in state.relay_saver_until_ts_by_idx.values()
    )
    if paused_idx or reason:
        state.relay_saver_last_reason = reason
    elif not active_until:
        state.relay_saver_last_reason = ""
    return payloads


def _relay_saver_payload_for_sign(sign: int, soc_limit: int, power: int) -> dict:
    ac_mode = 1 if sign > 0 else 2
    return _delay_payload_for_device(ac_mode, soc_limit, power)


def _clear_relay_saver_state(state: ProxyState) -> None:
    state.relay_saver_paused_idx = []
    state.relay_saver_until_ts_by_idx = {}
    state.relay_saver_sign_by_idx = {}
    state.relay_saver_last_reason = ""


def _clear_relay_saver_device(state: ProxyState, idx: int) -> None:
    state.relay_saver_until_ts_by_idx.pop(idx, None)
    state.relay_saver_sign_by_idx.pop(idx, None)


def _power_sign(power: int) -> int:
    if power > 0:
        return 1
    if power < 0:
        return -1
    return 0


def _record_ac_mode_command(dev, payload: dict, now_ts: float) -> None:
    ac_mode = _int(payload.get("acMode", 0))
    if ac_mode not in (1, 2):
        return
    if dev.latest_ac_mode_cmd != ac_mode:
        dev.latest_ac_mode_change_ts = now_ts
    dev.latest_ac_mode_cmd = ac_mode


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
