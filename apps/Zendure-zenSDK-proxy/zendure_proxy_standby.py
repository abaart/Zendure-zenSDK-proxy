"""
Standby management: put inactive devices into deep sleep (smartMode=0) after
a configurable delay, and wake them again when they become active.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Callable

from zendure_proxy_health import eligible_device_indices
from zendure_proxy_power import now

if TYPE_CHECKING:
    from zendure_proxy_config import Config
    from zendure_proxy_device_client import DeviceClient
    from zendure_proxy_state import ProxyState


async def manage_standby(
    state: ProxyState,
    clients: list[DeviceClient],
    ac_mode: int,
    per_device: list[int],
    cfg: Config,
    logger: Callable,
) -> None:
    """
    For every device:
      * Active   → cancel any pending standby task; wake device if asleep.
      * Inactive → schedule a delayed standby if the mode warrants it.
    """
    eligible = eligible_device_indices(state, cfg)
    eligible_set = set(eligible)
    active_set = set(state.devices_active_idx) & eligible_set
    protected_set = set()
    if getattr(state, "anti_pingpong_active", False):
        protected_set = (
            set(getattr(state, "anti_pingpong_reserve_idx", []))
            | set(getattr(state, "anti_pingpong_paused_idx", []))
        ) & eligible_set
    relay_saver_until = getattr(state, "relay_saver_until_ts_by_idx", {})
    relay_saver_protected = {
        idx for idx in getattr(state, "relay_saver_paused_idx", [])
        if relay_saver_until.get(idx, 0.0) > now()
    } & eligible_set
    protected_set |= relay_saver_protected
    devs = state.devices

    for i, dev in enumerate(devs):
        if i not in eligible_set:
            if dev.standby_task and not dev.standby_task.done():
                dev.standby_task.cancel()
            dev.standby_task = None
            continue
        if i in active_set or i in protected_set:
            if dev.standby_task and not dev.standby_task.done():
                dev.standby_task.cancel()
            dev.standby_task = None
            if dev.smart_mode == 0 and (per_device[i] > 0 or i in protected_set):
                await clients[i].post(
                    {"sn": dev.sn, "properties": {"smartMode": 1}}
                )
                dev.smart_mode = 1
            dev.standby_device = False
        else:
            should_standby = state.device_active_count < len(eligible) and (
                (ac_mode == 1 and cfg.standby_charging)
                or (ac_mode == 2 and cfg.standby_discharging)
            )
            should_standby = should_standby and _standby_allowed(i, state, cfg)
            if should_standby and dev.standby_task is None:
                delay = _standby_delay(dev.latest_power_cmd_zero_ts, cfg.standby_timer)
                dev.standby_task = asyncio.ensure_future(
                    _delayed_standby(
                        i, state, clients, delay, logger, cfg=cfg
                    )
                )
            elif not should_standby and dev.standby_task:
                dev.standby_task.cancel()
                dev.standby_task = None


async def _delayed_standby(
    idx: int,
    state: ProxyState,
    clients: list[DeviceClient],
    delay: float,
    logger: Callable,
    *,
    cfg: Config | None = None,
) -> None:
    """Wait *delay* seconds then send smartMode=0 to put the device to sleep."""
    try:
        await asyncio.sleep(delay)
        dev = state.devices[idx]
        if cfg is not None and not _standby_allowed(idx, state, cfg):
            return
        if idx not in state.devices_active_idx:
            zero_ts = dev.latest_power_cmd_zero_ts
            if cfg is not None and zero_ts > 0 and now() - zero_ts < cfg.standby_timer:
                return
            sent_key = dev.sn or f"device-{idx + 1}"
            if zero_ts > 0 and state.standby_last_sent_by_sn.get(sent_key) == zero_ts:
                return
            if dev.smart_mode == 0:
                return
            await clients[idx].post(
                {
                    "sn": dev.sn,
                    "properties": {
                        "smartMode": 0,
                        "outputLimit": 0,
                        "inputLimit": 0,
                    },
                }
            )
            close_post_connection = getattr(
                clients[idx], "close_post_connection", None
            )
            if close_post_connection is not None:
                await close_post_connection()
            dev.smart_mode = 0
            dev.standby_device = True
            if zero_ts > 0:
                state.standby_last_sent_by_sn[sent_key] = zero_ts
            logger(f"Device {idx+1} put into standby (smartMode=0)")
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger(f"Standby task error for device {idx+1}: {exc}", level="WARNING")
    finally:
        if idx < len(state.devices):
            state.devices[idx].standby_task = None


def _standby_allowed(idx: int, state: ProxyState, cfg: Config) -> bool:
    eligible = eligible_device_indices(state, cfg)
    if len(eligible) < 2:
        return False
    if idx not in eligible:
        return False
    if idx in state.devices_active_idx:
        return False
    if getattr(state, "anti_pingpong_active", False) and idx in (
        set(getattr(state, "anti_pingpong_reserve_idx", []))
        | set(getattr(state, "anti_pingpong_paused_idx", []))
    ):
        return False
    relay_saver_until = getattr(state, "relay_saver_until_ts_by_idx", {})
    if (
        idx in getattr(state, "relay_saver_paused_idx", [])
        and relay_saver_until.get(idx, 0.0) > now()
    ):
        return False
    if idx >= len(state.devices):
        return False

    dev = state.devices[idx]
    now_ts = now()
    transition_recent = any(
        start > 0 and now_ts - start < cfg.transition_timer + 10
        for start in (
            state.transition_start_ts,
            state.single_to_dual_transition_start_ts,
            state.forced_dual_transition_start_ts,
        )
    )
    stale_get = state.latest_get_ts <= 0 or now_ts - state.latest_get_ts > 10
    zero_power = state.latest_power_cmd == 0
    charging_diff_guard = (
        len(eligible) == 2
        and state.latest_power_cmd > 0
        and abs(
            state.devices[eligible[0]].electric_level
            - state.devices[eligible[1]].electric_level
        )
        == cfg.device_change_diff
    )
    direction_enabled = (
        (state.latest_power_cmd > 0 and cfg.standby_charging)
        or (state.latest_power_cmd < 0 and cfg.standby_discharging)
    )
    return (
        state.device_active_count < len(eligible)
        and direction_enabled
        and not transition_recent
        and not stale_get
        and not zero_power
        and not charging_diff_guard
        and dev.latest_power_cmd_zero_ts > 0
        and dev.smart_mode != 0
    )


def _standby_delay(zero_ts: float, standby_timer: int) -> float:
    if zero_ts <= 0:
        return float(standby_timer)
    return max(0.0, float(standby_timer) - (now() - zero_ts))
