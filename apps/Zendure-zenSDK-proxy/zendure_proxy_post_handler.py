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
from zendure_proxy_power import apply_damper, apply_transition, calc_active_count, distribute_power, now
from zendure_proxy_standby import manage_standby
from zendure_proxy_state import ProxyState

_POWER_KEYS = {"acMode", "inputLimit", "outputLimit"}


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
    props = payload.get("properties") or {}
    n = state.device_count
    devs = state.devices

    # ── Non-power properties: forward to all devices ──────────────────────────
    if not (_POWER_KEYS & set(props.keys())):
        responses = []
        for i, client in enumerate(clients):
            dp = (
                {"sn": devs[i].sn, "properties": dict(props)}
                if devs[i].sn
                else {"properties": dict(props)}
            )
            responses.append(await client.post(dp))

        # chargeMaxLimit and inverseMaxPower are aggregate values; divide by n
        if "chargeMaxLimit" in props:
            per = props["chargeMaxLimit"] // n
            state.charge_max_limit_cmd = props["chargeMaxLimit"]
            state.charge_max_limit_effective = per * n
            for dev in devs:
                dev.charge_max_limit = per
            state.max_power_in = per
        if "inverseMaxPower" in props:
            per = props["inverseMaxPower"] // n
            state.inverse_max_power_cmd = props["inverseMaxPower"]
            state.inverse_max_power_effective = per * n
            for dev in devs:
                dev.inverse_max_power = per
            state.max_power_out = per

        return responses[0] if responses else {"ack": "pong"}

    # ── Power command ─────────────────────────────────────────────────────────
    now_ts = now()
    if not is_repeat:
        state.latest_power_message_ts = now_ts

    input_limit: int = props.get("inputLimit", 0)
    output_limit: int = props.get("outputLimit", 0)
    ac_mode: int = _power_ac_mode(props, state.ac_mode, input_limit, output_limit)
    total_power = input_limit if ac_mode == 1 else (output_limit if ac_mode == 2 else 0)
    latest_power_cmd = _signed_power_cmd(ac_mode, total_power)

    max_power = (state.max_power_in if ac_mode == 1 else state.max_power_out) or 800

    upper = cfg.single_mode_upper_pct / 100.0 * max_power
    lower = cfg.single_mode_lower_pct / 100.0 * max_power
    force_all = cfg.equal_mode or cfg.always_dual_mode

    # ── How many devices active? ──────────────────────────────────────────────
    state.device_active_count = calc_active_count(
        state, ac_mode, total_power, upper, lower, force_all
    )

    # ── Which specific devices are active? ────────────────────────────────────
    prev_active_device = state.single_mode_active_device
    _select_active_devices(state, ac_mode, cfg)

    if state.device_active_count == 1 and state.single_mode_active_device != prev_active_device:
        state.transition_start_ts = now_ts
        state.transition_original_device = prev_active_device

    # ── Per-device power distribution ─────────────────────────────────────────
    per_device = _calc_per_device_power(state, ac_mode, total_power, max_power, cfg)
    per_device = apply_transition(per_device, state, now_ts, cfg.transition_timer)

    if cfg.damper_enable and ac_mode == 2 and n >= 2:
        per_device = apply_damper(
            per_device, state, total_power, upper, now_ts,
            cfg.damper_amount, cfg.damper_timer,
        )

    # ── Send commands to all devices in parallel ───────────────────────────────
    tasks = []
    for i, client in enumerate(clients):
        pwr = per_device[i]
        if ac_mode == 1:
            dp = {"acMode": 1, "inputLimit": pwr}
        elif ac_mode == 2:
            dp = {"acMode": 2, "outputLimit": pwr}
        else:
            dp = {"inputLimit": 0, "outputLimit": 0}
        for k, v in props.items():
            if k not in _POWER_KEYS:
                dp[k] = v
        device_payload: dict = {"properties": dp}
        if devs[i].sn:
            device_payload["sn"] = devs[i].sn
        tasks.append(client.post(device_payload))
        devs[i].latest_power_cmd = _signed_power_cmd(ac_mode, pwr)
        if pwr == 0:
            devs[i].latest_power_cmd_zero_ts = now_ts

    responses = await asyncio.gather(*tasks)

    # ── Update aggregate state ─────────────────────────────────────────────────
    state.ac_mode = ac_mode
    state.latest_power_cmd = latest_power_cmd
    if ac_mode == 1:
        state.input_limit = input_limit
        state.input_limit_effective = sum(per_device)
    elif ac_mode == 2:
        state.output_limit = output_limit
        state.output_limit_effective = sum(per_device)
    if not is_repeat:
        state.last_post_payload = payload

    asyncio.ensure_future(
        manage_standby(state, clients, ac_mode, per_device, cfg, logger)
    )

    return responses[0] if responses else {"ack": "pong"}


# ── Internal helpers ───────────────────────────────────────────────────────────

def _power_ac_mode(
    props: dict,
    current_ac_mode: int,
    input_limit: int,
    output_limit: int,
) -> int:
    if "acMode" in props:
        return props["acMode"]
    if input_limit > 0 and output_limit <= 0:
        return 1
    if output_limit > 0 and input_limit <= 0:
        return 2
    return current_ac_mode


def _signed_power_cmd(ac_mode: int, power: int) -> int:
    if ac_mode == 1:
        return power
    if ac_mode == 2:
        return -power
    return 0


def _select_active_devices(state: ProxyState, ac_mode: int, cfg: Config) -> None:
    """Choose which specific device indices are active based on SoC."""
    devs = state.devices
    n = state.device_count
    active_count = state.device_active_count
    min_soc_pct = state.min_soc / 10.0

    # Tighten hysteresis threshold near SoC boundaries
    diff_threshold = cfg.device_change_diff
    at_boundary = any(d.soc_limit == 2 for d in devs) and any(
        d.electric_level < min_soc_pct for d in devs
    )
    if at_boundary or any(d.soc_limit == 1 for d in devs):
        diff_threshold = 1

    if active_count >= n:
        state.devices_active_idx = list(range(n))
        return

    # Rank by SoC: lowest first for charging, highest first for discharging
    ranked = sorted(
        range(n),
        key=lambda i: devs[i].electric_level,
        reverse=(ac_mode == 2),
    )

    if active_count == 1:
        best = ranked[0]
        current = state.single_mode_active_device
        if abs(devs[best].electric_level - devs[current].electric_level) >= diff_threshold:
            state.single_mode_active_device = best
        state.devices_active_idx = [state.single_mode_active_device]
    else:
        state.devices_active_idx = ranked[:active_count]


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

    if cfg.equal_mode or all_zero:
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
