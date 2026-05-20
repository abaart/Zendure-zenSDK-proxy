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

PROXY_VERSION = "20260520-ad5"


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
        headroom = []
        for i in range(n):
            if power[i] > max_power:
                surplus += power[i] - max_power
                power[i] = float(max_power)
                headroom.append(0.0)
            else:
                headroom.append(float(max_power) - power[i])
        if surplus < 0.5:
            break
        total_headroom = sum(headroom)
        if total_headroom < 0.5:
            break
        for i in range(n):
            if headroom[i] > 0:
                power[i] += surplus * (headroom[i] / total_headroom)

    return [math.floor(p) for p in power]


# ── Active-device count ────────────────────────────────────────────────────────

def calc_active_count(
    state: ProxyState,
    ac_mode: int,
    total_power: int,
    upper: float,
    lower: float,
    force_all: bool,
) -> int:
    """
    Decide how many devices should be active based on power level and SoC state.

    Uses hysteresis (prev count) to avoid rapid mode oscillation.
    """
    n = state.device_count
    devs = state.devices
    min_soc_pct = state.min_soc / 10.0
    prev = state.device_active_count

    if force_all or n == 1:
        return n

    if n == 2:
        # Emergency: if exactly one device is below minSoc while charging,
        # activate both so the depleted one can catch up.
        if ac_mode == 1:
            below = [d.electric_level < min_soc_pct for d in devs[:2]]
            if sum(below) == 1:
                return 2
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
    if state.transition_start_ts <= 0:
        return per_device

    elapsed = now_ts - state.transition_start_ts
    if elapsed >= timer:
        state.transition_start_ts = 0.0
        return per_device

    orig = state.transition_original_device
    new = state.single_mode_active_device

    if orig == new or orig >= len(per_device) or new >= len(per_device):
        state.transition_start_ts = 0.0
        return per_device

    progress = elapsed / timer
    orig_frac = 0.95 if progress < 0.75 else 0.75
    total = sum(per_device)

    result = list(per_device)
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
            n = state.device_count
            return [round(upper / n)] * n
    else:
        state.dualmode_damper_active = False

    return per_device
