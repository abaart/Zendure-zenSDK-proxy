"""Per-device health and cache metadata helpers for the Zendure proxy."""

from __future__ import annotations

import copy
from typing import Any

from zendure_proxy_config import Config
from zendure_proxy_power import now
from zendure_proxy_state import ProxyState


DYNAMIC_SLOT_KEYS = (
    "electricLevel",
    "latestPowerCmd",
    "outputPackPower",
    "packInputPower",
    "outputHomePower",
    "gridInputPower",
    "inputLimit",
    "outputLimit",
    "socStatus",
    "acMode",
    "socLimit",
    "smartMode",
    "hyperTmp",
    "gridOffMode",
)


def refresh_device_health(
    state: ProxyState,
    cfg: Config,
    *,
    current_ts: float | None = None,
) -> None:
    ts = now() if current_ts is None else current_ts
    startup_ts = state.startup_ts or ts
    for dev in state.devices:
        if _dead_age(dev, startup_ts, ts) > cfg.degraded_power_hold_seconds:
            if dev.dead_since_ts <= 0:
                dev.dead_since_ts = ts
            if dev.excluded_since_ts <= 0:
                dev.excluded_since_ts = ts

        if dev.excluded_since_ts > 0:
            if (
                dev.recovery_started_ts > 0
                and ts - dev.recovery_started_ts >= cfg.get_recovery_window
            ):
                dev.excluded_since_ts = 0.0
                dev.recovery_started_ts = 0.0
                dev.dead_since_ts = 0.0
                dev.last_get_error = ""
                dev.latest_get_included = True
            else:
                dev.latest_get_included = False
                if dev.dead_since_ts > 0:
                    dev.latest_power_cmd = 0
            continue

        dev.latest_get_included = True


def record_get_results(
    state: ProxyState,
    cfg: Config,
    results: list[dict | None],
    *,
    current_ts: float | None = None,
) -> None:
    ts = now() if current_ts is None else current_ts
    refresh_device_health(state, cfg, current_ts=ts)
    for idx, dev in enumerate(state.devices):
        result = results[idx] if idx < len(results) else None
        if result is None:
            dev.last_failed_get_ts = ts
            dev.last_get_error = "GET returned no response"
            dev.recovery_started_ts = 0.0
            if dev.excluded_since_ts <= 0:
                dev.excluded_since_ts = ts
            dev.latest_get_included = dev.excluded_since_ts <= 0
            continue

        if dev.excluded_since_ts > 0 and dev.recovery_started_ts <= 0:
            dev.recovery_started_ts = ts
        dev.last_successful_get_ts = ts
        dev.last_get_error = ""
        if result.get("sn"):
            dev.sn = result["sn"]
        if (
            dev.excluded_since_ts > 0
            and dev.recovery_started_ts > 0
            and ts - dev.recovery_started_ts >= cfg.get_recovery_window
        ):
            dev.excluded_since_ts = 0.0
            dev.recovery_started_ts = 0.0
            dev.dead_since_ts = 0.0
        dev.latest_get_included = dev.excluded_since_ts <= 0
    refresh_device_health(state, cfg, current_ts=ts)


def eligible_device_indices(
    state: ProxyState,
    cfg: Config,
    *,
    current_ts: float | None = None,
) -> list[int]:
    ts = now() if current_ts is None else current_ts
    refresh_device_health(state, cfg, current_ts=ts)
    return [
        idx for idx, dev in enumerate(state.devices)
        if idx < state.device_count and dev.excluded_since_ts <= 0
    ]


def degraded_power_by_index(
    state: ProxyState,
    cfg: Config,
    *,
    current_ts: float | None = None,
) -> dict[int, int]:
    ts = now() if current_ts is None else current_ts
    refresh_device_health(state, cfg, current_ts=ts)
    powers: dict[int, int] = {}
    for idx, dev in enumerate(state.devices[:state.device_count]):
        if dev.excluded_since_ts <= 0 or dev.dead_since_ts > 0:
            continue
        powers[idx] = _last_known_power(dev)
    return powers


def health_summary(
    state: ProxyState,
    cfg: Config,
    *,
    current_ts: float | None = None,
) -> dict[str, Any]:
    ts = now() if current_ts is None else current_ts
    refresh_device_health(state, cfg, current_ts=ts)
    unhealthy = []
    excluded = []
    recovering = []
    degraded = []
    dead = []
    healthy_count = 0
    for idx, dev in enumerate(state.devices[:state.device_count]):
        is_excluded = dev.excluded_since_ts > 0
        is_recovering = is_excluded and dev.recovery_started_ts > 0
        is_dead = dev.dead_since_ts > 0
        is_degraded = is_excluded and not is_dead
        latest_failed = (
            dev.last_failed_get_ts > 0
            and dev.last_failed_get_ts >= dev.last_successful_get_ts
        )
        is_unhealthy = is_excluded or latest_failed
        item = _device_item(idx, dev, cfg, ts)
        if is_unhealthy:
            unhealthy.append(item)
        else:
            healthy_count += 1
        if is_excluded:
            excluded.append(item)
        if is_degraded:
            degraded.append(item)
        if is_dead:
            dead.append(item)
        if is_recovering:
            recovering.append(item)

    return {
        "configuredCount": state.device_count,
        "healthyCount": healthy_count,
        "unhealthyCount": len(unhealthy),
        "excludedCount": len(excluded),
        "recoveringCount": len(recovering),
        "degradedCount": len(degraded),
        "deadCount": len(dead),
        "unhealthyDevices": unhealthy,
        "excludedDevices": excluded,
        "degradedDevices": degraded,
        "deadDevices": dead,
        "recoveringDevices": recovering,
    }


def response_with_proxy_health(
    response: dict,
    state: ProxyState,
    cfg: Config,
    *,
    served_from_cache: bool,
    reason: str,
    refresh_in_progress: bool,
    current_ts: float | None = None,
) -> dict:
    ts = now() if current_ts is None else current_ts
    result = copy.deepcopy(response)
    summary = health_summary(state, cfg, current_ts=ts)
    cache_age = (
        ts - state.latest_get_ts
        if state.latest_get_ts > 0
        else None
    )
    result["proxyHealth"] = {
        "servedFromCache": served_from_cache,
        "reason": reason,
        "cacheAgeSeconds": round(cache_age, 3) if cache_age is not None else None,
        "cacheMaxAgeSeconds": cfg.get_cache_max_age,
        "refreshInProgress": refresh_in_progress,
        **summary,
    }
    _mark_unavailable_slots(result, summary)
    return result


def cache_is_usable(
    state: ProxyState,
    cfg: Config,
    *,
    current_ts: float | None = None,
) -> bool:
    ts = now() if current_ts is None else current_ts
    return (
        state.last_get_response is not None
        and state.latest_get_ts > 0
        and ts - state.latest_get_ts <= cfg.get_cache_max_age
    )


def recovery_seconds_remaining(item: dict[str, Any]) -> float:
    value = item.get("recoverySecondsRemaining")
    return float(value) if isinstance(value, (int, float)) else 0.0


def _device_item(idx: int, dev, cfg: Config, ts: float) -> dict[str, Any]:
    last_age = (
        round(ts - dev.last_successful_get_ts, 3)
        if dev.last_successful_get_ts > 0
        else None
    )
    recovery_remaining = 0.0
    if dev.excluded_since_ts > 0 and dev.recovery_started_ts > 0:
        recovery_remaining = max(
            0.0,
            cfg.get_recovery_window - (ts - dev.recovery_started_ts),
        )
    return {
        "slot": idx + 1,
        "serialNumber": dev.sn or "unknown",
        "ipAddress": dev.ip,
        "lastSuccessfulGetAgeSeconds": last_age,
        "lastGetError": dev.last_get_error,
        "recoverySecondsRemaining": round(recovery_remaining, 3),
        "lastKnownPower": _last_known_power(dev),
        "dead": dev.dead_since_ts > 0,
    }


def _mark_unavailable_slots(response: dict, summary: dict[str, Any]) -> None:
    props = response.setdefault("properties", {})
    unavailable_slots = {
        item["slot"]
        for item in (
            summary.get("excludedDevices", [])
            + summary.get("recoveringDevices", [])
            + summary.get("deadDevices", [])
        )
    }
    for slot in unavailable_slots:
        suffix = f"_{slot}"
        for key in DYNAMIC_SLOT_KEYS:
            props[f"{key}{suffix}"] = "unavailable"


def _dead_age(dev, startup_ts: float, ts: float) -> float:
    if dev.last_successful_get_ts > 0:
        return ts - dev.last_successful_get_ts
    if dev.excluded_since_ts > 0 or dev.last_failed_get_ts > 0:
        return ts - startup_ts
    return 0.0


def _last_known_power(dev) -> int:
    props = (dev.last_response or {}).get("properties", {})
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
