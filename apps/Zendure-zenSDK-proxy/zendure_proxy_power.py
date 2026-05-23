"""
Pure power-math functions.  No I/O, no AppDaemon, no aiohttp.

Everything here is deterministic and trivially unit-testable.
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zendure_proxy_state import ProxyState

PROXY_VERSION = "v0.1.22"
SOC_LOCKSTEP_HIGH_THRESHOLD = 90.0


def now() -> float:
    """Monotonic clock in seconds."""
    return time.monotonic()


def epoch() -> int:
    """Current Unix timestamp (integer seconds)."""
    return int(time.time())


# ── Core distribution ──────────────────────────────────────────────────────────

def distribute_power(
    total: int,
    avail: list[float],
    max_power: int,
    balancing_factor: int = 5,
) -> list[int]:
    """
    Distribute *total* watts across devices, weighted nonlinearly by *avail*.

    avail[i] is the headroom of device i (e.g. %-points until full/empty).
    Devices with more headroom receive proportionally more power; the exponent
    *balancing_factor* controls the aggressiveness.  After the proportional
    split the per-device max is enforced and any surplus is redistributed to
    devices that still have headroom.

    Returns a list of integer watts, one per device in the *avail* list.
    """
    n = len(avail)
    avail = [max(0.0, a) for a in avail]

    if n == 0 or sum(avail) == 0:
        return [0] * n

    weights = [a ** balancing_factor for a in avail]
    total_weight = sum(weights)
    if total_weight == 0:
        per = total // n
        return [min(per, max_power)] * n

    power = [float(total) * (w / total_weight) for w in weights]

    for _ in range(n + 1):
        surplus = 0.0
        for i in range(n):
            if power[i] > max_power:
                surplus += power[i] - max_power
                power[i] = float(max_power)

        if surplus < 0.5:
            break

        while surplus >= 0.5:
            open_indices = [
                i for i in range(n)
                if power[i] < max_power and weights[i] > 0
            ]
            total_open_weight = sum(weights[i] for i in open_indices)
            if total_open_weight <= 0:
                break

            distributed = 0.0
            for i in open_indices:
                addition = surplus * (weights[i] / total_open_weight)
                actual = min(addition, float(max_power) - power[i])
                power[i] += actual
                distributed += actual
            if distributed < 0.5:
                break
            surplus -= distributed

    return [math.floor(p) for p in power]


# ── Active-device count ────────────────────────────────────────────────────────

def soc_boundary_lockstep_active(
    state: ProxyState,
    eligible_indices: list[int] | None = None,
) -> bool:
    """Return True when SoC boundary protection should change distribution."""
    indices = (
        eligible_indices
        if eligible_indices is not None
        else list(range(state.device_count))
    )
    min_soc_pct = state.min_soc / 10.0
    for idx in indices:
        if idx < 0 or idx >= len(state.devices):
            continue
        level = state.devices[idx].electric_level
        if level < min_soc_pct or level > SOC_LOCKSTEP_HIGH_THRESHOLD:
            return True
    return False


def calc_active_count(
    state: ProxyState,
    ac_mode: int,
    total_power: int,
    upper: float,
    lower: float,
    force_all: bool,
    device_change_diff: int = 5,
    eligible_indices: list[int] | None = None,
    soc_boundary_min_device_power_watts: int = 100,
    soc_boundary_active: bool = False,
    soc_boundary_measured_capacities: list[int] | None = None,
) -> int:
    """
    Decide how many devices should be active based on power level and SoC state.

    Uses hysteresis (prev count) to avoid rapid mode oscillation.
    """
    indices = eligible_indices if eligible_indices is not None else list(range(state.device_count))
    n = len(indices)
    devs = [state.devices[idx] for idx in indices]
    min_soc_pct = state.min_soc / 10.0
    prev = min(state.device_active_count, max(n, 1))

    if n <= 0:
        return 0

    if n == 1:
        return n

    if soc_boundary_active or soc_boundary_lockstep_active(state, indices):
        min_power = max(0, int(soc_boundary_min_device_power_watts))
        if total_power <= 0 or min_power <= 0:
            return n
        if soc_boundary_measured_capacities:
            ranked_positions = sorted(
                range(n),
                key=lambda pos: state.devices[indices[pos]].electric_level,
                reverse=(ac_mode == 2),
            )
            ranked_capacities = [
                soc_boundary_measured_capacities[pos]
                for pos in ranked_positions
            ]
            target = max(1, total_power // min_power)
            active_count = min(n, target)
            while (
                active_count < n
                and sum(ranked_capacities[:active_count]) < total_power
            ):
                active_count += 1
            return max(1, active_count)
        return max(1, min(n, total_power // min_power))

    if force_all:
        return n

    if n == 2:
        if ac_mode == 1:
            below = [d.electric_level < min_soc_pct for d in devs[:2]]
            if sum(below) == 1:
                return 1
            if sum(d.soc_limit == 1 for d in devs[:2]) == 1:
                return 1
            high_soc = any(d.electric_level >= 98 for d in devs[:2])
            avg_soc = math.ceil(sum(d.electric_level for d in devs[:2]) / 2)
            if high_soc and avg_soc >= 98 - device_change_diff:
                return 2
        if ac_mode == 2:
            below = [d.electric_level <= min_soc_pct for d in devs[:2]]
            if sum(below) == 1:
                return 1
            if sum(d.soc_limit == 2 for d in devs[:2]) == 1:
                return 1
        if total_power == 0:
            return prev
        if total_power < lower:
            return 1
        if total_power > upper * 2:
            return 2
        if prev == 1 and total_power > upper:
            return 2
        if prev == 2 and total_power < lower:
            return 1
        return prev

    # n == 3
    dual_upper = upper * 2
    dual_lower = lower * 2
    if total_power == 0:
        return prev
    if total_power < lower:
        return 1
    if total_power > dual_upper:
        return 3
    if prev == 1 and total_power > upper:
        return 2
    if prev == 3 and total_power < dual_lower:
        return 2
    return prev


# ── Smooth transition ──────────────────────────────────────────────────────────

def apply_transition(
    per_device: list[int],
    state: ProxyState,
    now_ts: float,
    timer: int,
) -> list[int]:
    """
    When the active single-mode device changes, blend power between the old
    and new device over *timer* seconds to avoid abrupt power spikes.

    Phase 1 (0 – 75 %): 95 % to original device, 5 % to new.
    Phase 2 (75 – 100 %): 75 % to original device, 25 % to new.
    """
    forced_start = getattr(state, "forced_dual_transition_start_ts", 0.0)
    single_to_dual_start = getattr(state, "single_to_dual_transition_start_ts", 0.0)
    legacy_start = state.transition_start_ts

    if forced_start <= 0 and single_to_dual_start <= 0 and legacy_start <= 0:
        return per_device

    if forced_start > 0:
        return _apply_forced_dual_transition(per_device, state, now_ts, timer)

    start = single_to_dual_start or legacy_start
    elapsed = now_ts - start
    if elapsed >= timer:
        state.single_to_dual_transition_start_ts = 0.0
        state.transition_start_ts = 0.0
        return per_device

    orig = (
        state.single_to_dual_transition_original_device
        if single_to_dual_start > 0
        else state.transition_original_device
    )
    if orig >= len(per_device):
        state.single_to_dual_transition_start_ts = 0.0
        state.transition_start_ts = 0.0
        return per_device

    total = sum(per_device)
    other_count = max(1, len([power for i, power in enumerate(per_device) if i != orig]))
    progress = elapsed / timer
    orig_frac = 0.95 if progress < 0.75 else 0.75
    remainder = total - round(total * orig_frac)

    result = [0] * len(per_device)
    result[orig] = round(total * orig_frac)
    for i in range(len(result)):
        if i != orig:
            result[i] = round(remainder / other_count)
    return result


def _apply_forced_dual_transition(
    per_device: list[int],
    state: ProxyState,
    now_ts: float,
    timer: int,
) -> list[int]:
    start = state.forced_dual_transition_start_ts
    elapsed = now_ts - start
    if elapsed >= timer:
        state.forced_dual_transition_start_ts = 0.0
        state.transition_start_ts = 0.0
        return per_device

    orig = state.forced_dual_transition_original_device
    new = state.single_mode_active_device

    if orig == new or orig >= len(per_device) or new >= len(per_device):
        state.forced_dual_transition_start_ts = 0.0
        return per_device

    progress = elapsed / timer
    if progress < 0.57:
        orig_frac = 0.95
    elif progress < 0.71:
        orig_frac = 0.75
    elif progress < 0.85:
        orig_frac = 0.50
    else:
        orig_frac = 0.25
    total = sum(per_device)

    result = [0] * len(per_device)
    result[orig] = round(total * orig_frac)
    result[new] = round(total * (1.0 - orig_frac))
    return result


# ── Dual-mode damper ───────────────────────────────────────────────────────────

def apply_damper(
    per_device: list[int],
    state: ProxyState,
    total_power: int,
    upper: float,
    now_ts: float,
    damper_amount: int,
    damper_timer: int,
) -> list[int]:
    """
    Prevent mode flapping when power hovers just above the single-mode threshold.

    If the excess over *upper* is within *damper_amount* W, hold at the
    single-device limit for *damper_timer* seconds before actually switching
    to dual mode.
    """
    excess = total_power - upper

    if excess <= 0:
        state.dualmode_damper_active = False
        return per_device

    if 0 < excess <= damper_amount:
        if not state.dualmode_damper_active:
            state.dualmode_damper_active = True
            state.dualmode_damper_start_ts = now_ts
        if now_ts - state.dualmode_damper_start_ts < damper_timer:
            active_idx = (
                state.devices_active_idx
                if state.devices_active_idx
                else [state.single_mode_active_device]
            )
            result = [0] * state.device_count
            if active_idx:
                result[active_idx[0]] = round(upper)
            return result
    else:
        state.dualmode_damper_active = False

    return per_device
