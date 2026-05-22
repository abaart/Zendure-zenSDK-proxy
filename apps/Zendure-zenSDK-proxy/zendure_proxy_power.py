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

PROXY_VERSION = "v0.1.19"


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
    max_power: int | list[int],
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
    caps = _cap_list(max_power, n)
    avail = [max(0.0, a) for a in avail]

    if n == 0 or sum(avail) == 0:
        return [0] * n

    weights = [(a ** balancing_factor) * max(0, caps[i]) for i, a in enumerate(avail)]
    total_weight = sum(weights)
    if total_weight == 0:
        return split_equal_power(total, caps)

    power = [float(total) * (w / total_weight) for w in weights]

    for _ in range(n + 1):
        surplus = 0.0
        for i in range(n):
            if power[i] > caps[i]:
                surplus += power[i] - caps[i]
                power[i] = float(caps[i])

        if surplus < 0.5:
            break

        while surplus >= 0.5:
            open_indices = [
                i for i in range(n)
                if power[i] < caps[i] and weights[i] > 0
            ]
            total_open_weight = sum(weights[i] for i in open_indices)
            if total_open_weight <= 0:
                break

            distributed = 0.0
            for i in open_indices:
                addition = surplus * (weights[i] / total_open_weight)
                actual = min(addition, float(caps[i]) - power[i])
                power[i] += actual
                distributed += actual
            if distributed < 0.5:
                break
            surplus -= distributed

    return [math.floor(p) for p in power]


def split_equal_power(total: int, max_power: int | list[int]) -> list[int]:
    caps = _cap_list(max_power, len(max_power) if isinstance(max_power, list) else 1)
    n = len(caps)
    if n <= 0:
        return []
    remaining = max(0, int(total))
    result = [0] * n
    open_indices = [idx for idx, cap in enumerate(caps) if cap > 0]

    while remaining > 0 and open_indices:
        share = max(1, remaining // len(open_indices))
        distributed = 0
        next_open: list[int] = []
        for idx in open_indices:
            room = caps[idx] - result[idx]
            if room <= 0:
                continue
            addition = min(share, room, remaining - distributed)
            if addition <= 0:
                next_open.append(idx)
                continue
            result[idx] += addition
            distributed += addition
            if result[idx] < caps[idx]:
                next_open.append(idx)
            if distributed >= remaining:
                break
        if distributed <= 0:
            break
        remaining -= distributed
        open_indices = next_open
    return result


def _cap_list(max_power: int | list[int], n: int) -> list[int]:
    if isinstance(max_power, list):
        values = list(max_power)
    else:
        values = [max_power] * n
    if len(values) < n:
        values.extend([0] * (n - len(values)))
    return [max(0, _int(value)) for value in values[:n]]


def _direction_cap(dev, ac_mode: int) -> int:
    if ac_mode == 1:
        return max(0, _int(getattr(dev, "effective_charge_max_watts", 0)))
    if ac_mode == 2:
        return max(0, _int(getattr(dev, "effective_discharge_max_watts", 0)))
    return 0


def _device_has_direction_headroom(state: ProxyState, idx: int, ac_mode: int) -> bool:
    if idx < 0 or idx >= len(state.devices):
        return False
    dev = state.devices[idx]
    if getattr(dev, "soc_status", 0) == 1:
        return False
    if _direction_cap(dev, ac_mode) <= 0:
        return False
    min_soc_pct = state.min_soc / 10.0
    soc_set_pct = state.soc_set / 10.0
    if ac_mode == 1:
        return dev.soc_limit != 1 and dev.electric_level < soc_set_pct
    if ac_mode == 2:
        return dev.soc_limit != 2 and dev.electric_level > min_soc_pct
    return False


def _rank_for_direction(state: ProxyState, ac_mode: int, indices: list[int]) -> list[int]:
    if ac_mode == 2:
        return sorted(indices, key=lambda idx: state.devices[idx].electric_level, reverse=True)
    return sorted(indices, key=lambda idx: state.devices[idx].electric_level)


def _int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


# ── Active-device count ────────────────────────────────────────────────────────

def calc_active_count(
    state: ProxyState,
    ac_mode: int,
    total_power: int,
    upper: float,
    lower: float,
    force_all: bool,
    device_change_diff: int = 5,
    eligible_indices: list[int] | None = None,
) -> int:
    """
    Decide how many devices should be active based on power level and SoC state.

    Uses hysteresis (prev count) to avoid rapid mode oscillation.
    """
    indices = eligible_indices if eligible_indices is not None else list(range(state.device_count))
    if not indices:
        return 0

    if force_all:
        return len(indices)

    usable = [
        idx for idx in indices
        if _device_has_direction_headroom(state, idx, ac_mode)
    ]
    selection = usable if usable else indices
    ranked = _rank_for_direction(state, ac_mode, selection)
    n = len(ranked)
    prev = min(max(1, state.device_active_count), n)

    if n == 1:
        return n

    if total_power == 0:
        return prev

    min_soc_pct = state.min_soc / 10.0
    if ac_mode == 1:
        below_min = [
            idx for idx in ranked
            if state.devices[idx].electric_level < min_soc_pct
        ]
        if below_min and len(below_min) < n:
            return max(1, min(len(below_min), n))
        high_soc = any(state.devices[idx].electric_level >= 98 for idx in ranked)
        avg_soc = math.ceil(
            sum(state.devices[idx].electric_level for idx in ranked) / n
        )
        if high_soc and avg_soc >= 98 - device_change_diff:
            return n

    caps = [_direction_cap(state.devices[idx], ac_mode) for idx in ranked]
    max_cap = max(caps) if caps else 0
    if max_cap <= 0:
        return 0
    upper_scale = max(0.0, upper / max_cap)
    lower_scale = max(0.0, lower / max_cap)

    active_count = prev
    while active_count < n:
        active_cap = sum(caps[:active_count])
        if total_power <= active_cap * upper_scale:
            break
        active_count += 1

    while active_count > 1:
        smaller_cap = sum(caps[: active_count - 1])
        if total_power >= smaller_cap * lower_scale:
            break
        active_count -= 1

    return max(0, min(active_count, n))


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
