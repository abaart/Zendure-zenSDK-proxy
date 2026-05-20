"""Pure reserve mode helpers for relay-friendly power distribution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zendure_proxy_config import Config
    from zendure_proxy_state import ProxyState


@dataclass
class AntiPingpongSplit:
    active: bool
    service_idx: list[int]
    reserve_idx: list[int]
    service_power: int
    reserve_power_by_idx: dict[int, int]
    reason: str


def activation_mode(cfg: Config) -> str:
    mode = str(getattr(cfg, "anti_pingpong_activation_mode", "threshold")).lower()
    return mode if mode in {"threshold", "smart"} else "threshold"


def record_power_direction(
    state: ProxyState,
    cfg: Config,
    signed_power: int,
    is_repeat: bool,
    now_ts: float,
) -> None:
    if is_repeat or not getattr(cfg, "anti_pingpong_enable", False):
        return

    _record_power_sample(state, cfg, signed_power, now_ts)

    min_power = max(0, int(getattr(cfg, "anti_pingpong_min_power_watts", 100)))
    if abs(signed_power) < min_power:
        return

    sign = 1 if signed_power > 0 else -1
    previous = getattr(state, "anti_pingpong_last_sign", 0)
    if previous and sign != previous:
        state.anti_pingpong_flip_times.append(now_ts)
    state.anti_pingpong_last_sign = sign

    window = max(1, int(getattr(cfg, "anti_pingpong_window_seconds", 180)))
    state.anti_pingpong_flip_times = [
        ts for ts in state.anti_pingpong_flip_times if now_ts - ts <= window
    ]


def dominant_power_sign(state: ProxyState, cfg: Config, now_ts: float) -> int:
    window = max(
        1,
        int(getattr(cfg, "anti_pingpong_mode_switch_dominance_window_seconds", 120)),
    )
    window_start = now_ts - window
    samples = [
        (ts, value)
        for ts, value in getattr(state, "anti_pingpong_power_samples", [])
        if ts >= window_start and ts <= now_ts
    ]
    samples.sort()
    if not samples:
        return 0

    weighted_sum = 0.0
    previous_ts, previous_value = samples[0]
    previous_ts = max(previous_ts, window_start)
    for sample_ts, sample_value in samples[1:]:
        weighted_sum += previous_value * max(0.0, sample_ts - previous_ts)
        previous_ts = sample_ts
        previous_value = sample_value
    weighted_sum += previous_value * max(0.0, now_ts - previous_ts)

    if weighted_sum > 0:
        return 1
    if weighted_sum < 0:
        return -1
    return 0


def threshold_active(
    state: ProxyState,
    cfg: Config,
    signed_power: int,
    now_ts: float,
) -> bool:
    if not getattr(cfg, "anti_pingpong_enable", False):
        _clear_active(state, "disabled")
        return False
    if activation_mode(cfg) != "threshold":
        return bool(state.anti_pingpong_active)
    if signed_power == 0 and now_ts >= state.anti_pingpong_until_ts:
        _clear_active(state, "threshold_expired")
        return False

    min_flips = max(1, int(getattr(cfg, "anti_pingpong_min_flips", 3)))
    if len(state.anti_pingpong_flip_times) >= min_flips:
        hold_seconds = max(1, int(getattr(cfg, "anti_pingpong_hold_seconds", 300)))
        state.anti_pingpong_active = True
        state.anti_pingpong_until_ts = max(
            state.anti_pingpong_until_ts,
            now_ts + hold_seconds,
        )
        state.anti_pingpong_last_reason = "threshold_positive"

    if state.anti_pingpong_active and now_ts < state.anti_pingpong_until_ts:
        return True

    _clear_active(state, "threshold_expired")
    return False


def select_anti_pingpong_split(
    state: ProxyState,
    cfg: Config,
    ac_mode: int,
    eligible: list[int],
    requested_power: int,
    max_power: int,
) -> AntiPingpongSplit:
    if ac_mode not in (1, 2):
        return _inactive("unsupported_ac_mode")
    if len(eligible) < 2:
        return _inactive("not_enough_devices")
    if requested_power <= 0:
        return _inactive("zero_power")

    reserve_count = min(
        max(1, int(getattr(cfg, "anti_pingpong_reserve_count", 1))),
        len(eligible) - 1,
    )
    reserve_candidates = _reserve_candidates(state, cfg, ac_mode, eligible)
    if not reserve_candidates:
        return _inactive("no_reserve_soc_margin")

    active_set = set(getattr(state, "devices_active_idx", []))
    reserve_pool = [idx for idx in reserve_candidates if idx not in active_set]
    if len(reserve_pool) < reserve_count:
        reserve_pool = reserve_candidates
    reserve_idx = reserve_pool[:reserve_count]

    service_candidates = _service_candidates(state, ac_mode, eligible, reserve_idx)
    if not service_candidates:
        return _inactive("no_service_device")

    service_idx = [
        idx for idx in getattr(state, "devices_active_idx", [])
        if idx in service_candidates and idx not in reserve_idx
    ]
    if not service_idx:
        service_idx = [service_candidates[0]]

    service_capacity = _service_capacity(state, ac_mode, service_idx, max_power)
    reserve_power = max(0, int(getattr(cfg, "anti_pingpong_reserve_power_watts", 30)))
    reserve_total = reserve_power * len(reserve_idx)
    needed_power = requested_power + reserve_total

    for idx in service_candidates:
        if service_capacity >= needed_power:
            break
        if idx not in service_idx:
            service_idx.append(idx)
            service_capacity = _service_capacity(state, ac_mode, service_idx, max_power)

    if service_capacity < needed_power:
        return _inactive("service_capacity")

    return AntiPingpongSplit(
        active=True,
        service_idx=service_idx,
        reserve_idx=reserve_idx,
        service_power=needed_power,
        reserve_power_by_idx={idx: reserve_power for idx in reserve_idx},
        reason="active",
    )


def apply_mode_switch_delay(
    state: ProxyState,
    cfg: Config,
    desired_payloads: dict[int, dict],
    now_ts: float,
) -> dict[int, dict]:
    delay_seconds = _mode_switch_delay_seconds(cfg)
    delay_power = max(
        0,
        int(getattr(cfg, "anti_pingpong_reserve_power_watts", 30)),
    )
    delayed: list[int] = []
    adjusted: dict[int, dict] = {}

    for idx, payload in desired_payloads.items():
        dev = state.devices[idx]
        desired_ac_mode = _int(payload.get("acMode", 0))
        current_ac_mode = dev.latest_ac_mode_cmd
        if current_ac_mode <= 0:
            _mark_ac_mode(dev, desired_ac_mode, now_ts)
            adjusted[idx] = payload
            continue

        wants_switch = desired_ac_mode in (1, 2) and desired_ac_mode != current_ac_mode
        age = now_ts - dev.latest_ac_mode_change_ts
        if wants_switch and age < delay_seconds:
            delayed.append(idx)
            adjusted[idx] = _delay_payload(current_ac_mode, dev.soc_limit, delay_power)
            continue

        if wants_switch:
            _mark_ac_mode(dev, desired_ac_mode, now_ts)
        adjusted[idx] = payload

    state.anti_pingpong_paused_idx = delayed
    return adjusted


def smart_sample_grid_power(
    state: ProxyState,
    cfg: Config,
    grid_power_watts: float,
    now_ts: float,
) -> None:
    window = max(1, int(getattr(cfg, "anti_pingpong_smart_window_seconds", 300)))
    state.anti_pingpong_grid_samples.append((now_ts, float(grid_power_watts)))
    state.anti_pingpong_grid_samples = [
        (ts, value)
        for ts, value in state.anti_pingpong_grid_samples
        if now_ts - ts <= window
    ]


def smart_evaluate_window(
    state: ProxyState,
    cfg: Config,
    reserve_capacity_watts: int,
    now_ts: float,
) -> bool:
    samples = [
        (ts, value)
        for ts, value in state.anti_pingpong_grid_samples
        if now_ts - ts <= getattr(cfg, "anti_pingpong_smart_window_seconds", 300)
    ]
    samples.sort()
    if len(samples) < 2 or reserve_capacity_watts <= 0:
        _store_smart_result(state, 0.0, _smart_loss_kwh(cfg), cfg)
        state.anti_pingpong_smart_bad_minutes += 1
        if state.anti_pingpong_smart_bad_minutes >= getattr(
            cfg, "anti_pingpong_smart_disable_bad_minutes", 2
        ):
            _clear_active(state, "smart_no_data")
        return False

    response_seconds = max(
        0.0,
        float(getattr(cfg, "anti_pingpong_smart_response_time_seconds", 3.0)),
    )
    gain_kwh = 0.0
    last_import_start: float | None = None

    previous_ts, previous_power = samples[0]
    for sample_ts, power in samples[1:]:
        dt = max(0.0, sample_ts - previous_ts)
        if previous_power <= 0 < power:
            last_import_start = sample_ts
        if (
            power > 0
            and last_import_start is not None
            and sample_ts - last_import_start <= response_seconds
        ):
            gain_kwh += min(power, reserve_capacity_watts) * dt / 3_600_000.0
        previous_ts, previous_power = sample_ts, power

    loss_kwh = _smart_loss_kwh(cfg)
    _store_smart_result(state, gain_kwh, loss_kwh, cfg)
    state.anti_pingpong_smart_last_eval_ts = now_ts

    if state.anti_pingpong_smart_net_eur > 0:
        state.anti_pingpong_active = True
        state.anti_pingpong_smart_bad_minutes = 0
        state.anti_pingpong_last_reason = "smart_positive"
        return True

    state.anti_pingpong_smart_bad_minutes += 1
    if state.anti_pingpong_smart_bad_minutes >= getattr(
        cfg, "anti_pingpong_smart_disable_bad_minutes", 2
    ):
        _clear_active(state, "smart_negative")
    return False


def reserve_discharge_capacity_watts(
    state: ProxyState,
    cfg: Config,
    eligible: list[int],
) -> int:
    candidates = _reserve_candidates(state, cfg, 1, eligible)
    reserve_count = max(1, int(getattr(cfg, "anti_pingpong_reserve_count", 1)))
    max_out = getattr(state, "max_power_out", 800) or 800
    capacity = 0
    for idx in candidates[:reserve_count]:
        dev = state.devices[idx]
        capacity += min(max_out, dev.inverse_max_power or max_out)
    return capacity


def clear_command_state(state: ProxyState) -> None:
    state.anti_pingpong_service_idx = []
    state.anti_pingpong_reserve_idx = []
    state.anti_pingpong_paused_idx = []
    state.anti_pingpong_reserve_power_watts = 0


def _reserve_candidates(
    state: ProxyState,
    cfg: Config,
    ac_mode: int,
    eligible: list[int],
) -> list[int]:
    devs = state.devices
    margin = max(0, int(getattr(cfg, "anti_pingpong_reserve_soc_margin_percent", 5)))
    min_soc_pct = state.min_soc / 10.0
    soc_set_pct = state.soc_set / 10.0
    if ac_mode == 1:
        candidates = [
            idx for idx in eligible
            if devs[idx].soc_limit != 2
            and devs[idx].electric_level >= min_soc_pct + margin
        ]
        return sorted(candidates, key=lambda idx: devs[idx].electric_level, reverse=True)
    if ac_mode == 2:
        candidates = [
            idx for idx in eligible
            if devs[idx].soc_limit != 1
            and devs[idx].electric_level <= soc_set_pct - margin
        ]
        return sorted(candidates, key=lambda idx: devs[idx].electric_level)
    return []


def _record_power_sample(
    state: ProxyState,
    cfg: Config,
    signed_power: int,
    now_ts: float,
) -> None:
    window = max(
        1,
        int(getattr(cfg, "anti_pingpong_mode_switch_dominance_window_seconds", 120)),
    )
    state.anti_pingpong_power_samples.append((now_ts, signed_power))
    state.anti_pingpong_power_samples = [
        (ts, value)
        for ts, value in state.anti_pingpong_power_samples
        if now_ts - ts <= window
    ]


def _service_candidates(
    state: ProxyState,
    ac_mode: int,
    eligible: list[int],
    reserve_idx: list[int],
) -> list[int]:
    devs = state.devices
    blocked = set(reserve_idx)
    if ac_mode == 1:
        candidates = [
            idx for idx in eligible if idx not in blocked and devs[idx].soc_limit != 1
        ]
        return sorted(candidates, key=lambda idx: devs[idx].electric_level)
    if ac_mode == 2:
        candidates = [
            idx for idx in eligible if idx not in blocked and devs[idx].soc_limit != 2
        ]
        return sorted(candidates, key=lambda idx: devs[idx].electric_level, reverse=True)
    return []


def _service_capacity(
    state: ProxyState,
    ac_mode: int,
    service_idx: list[int],
    max_power: int,
) -> int:
    total = 0
    for idx in service_idx:
        dev = state.devices[idx]
        if ac_mode == 1:
            total += min(max_power, dev.charge_max_limit or max_power)
        elif ac_mode == 2:
            total += min(max_power, dev.inverse_max_power or max_power)
    return total


def _mode_switch_delay_seconds(cfg: Config) -> int:
    delay_seconds = getattr(cfg, "anti_pingpong_mode_switch_delay_seconds", None)
    pause_seconds = getattr(cfg, "anti_pingpong_mode_switch_pause_seconds", None)
    if delay_seconds == 30 and pause_seconds not in (None, 30):
        delay_seconds = pause_seconds
    return max(
        0,
        int(30 if delay_seconds is None else delay_seconds),
    )


def _delay_payload(ac_mode: int, soc_limit: int, delay_power: int) -> dict:
    payload = {"acMode": ac_mode, "inputLimit": 0, "outputLimit": 0}
    if ac_mode == 1 and soc_limit != 1:
        payload["inputLimit"] = delay_power
    elif ac_mode == 2 and soc_limit != 2:
        payload["outputLimit"] = delay_power
    return payload


def _mark_ac_mode(dev, ac_mode: int, now_ts: float) -> None:
    if ac_mode in (1, 2):
        if dev.latest_ac_mode_cmd != ac_mode:
            dev.latest_ac_mode_change_ts = now_ts
        dev.latest_ac_mode_cmd = ac_mode


def _smart_loss_kwh(cfg: Config) -> float:
    window_seconds = max(
        1,
        int(getattr(cfg, "anti_pingpong_smart_window_seconds", 300)),
    )
    reserve_count = max(1, int(getattr(cfg, "anti_pingpong_reserve_count", 1)))
    reserve_power = max(0, int(getattr(cfg, "anti_pingpong_reserve_power_watts", 30)))
    efficiency = float(getattr(cfg, "anti_pingpong_low_power_roundtrip_efficiency", 0.40))
    efficiency = min(max(efficiency, 0.0), 1.0)
    loss_watts = reserve_count * reserve_power * (1.0 - efficiency)
    return loss_watts * window_seconds / 3_600_000.0


def _store_smart_result(
    state: ProxyState,
    gain_kwh: float,
    loss_kwh: float,
    cfg: Config,
) -> None:
    price = float(getattr(cfg, "anti_pingpong_energy_price_per_kwh", 0.30))
    state.anti_pingpong_smart_gain_kwh = gain_kwh
    state.anti_pingpong_smart_loss_kwh = loss_kwh
    state.anti_pingpong_smart_net_eur = (gain_kwh - loss_kwh) * price


def _clear_active(state: ProxyState, reason: str) -> None:
    state.anti_pingpong_active = False
    state.anti_pingpong_service_idx = []
    state.anti_pingpong_reserve_idx = []
    state.anti_pingpong_paused_idx = []
    state.anti_pingpong_reserve_power_watts = 0
    state.anti_pingpong_last_reason = reason


def _inactive(reason: str) -> AntiPingpongSplit:
    return AntiPingpongSplit(
        active=False,
        service_idx=[],
        reserve_idx=[],
        service_power=0,
        reserve_power_by_idx={},
        reason=reason,
    )


def _int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default
