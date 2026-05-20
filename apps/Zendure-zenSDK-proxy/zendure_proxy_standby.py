"""
Standby management: put inactive devices into deep sleep (smartMode=0) after
a configurable delay, and wake them again when they become active.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Callable

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
    active_set = set(state.devices_active_idx)
    devs = state.devices
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
        state.device_count == 2
        and ac_mode == 1
        and abs(devs[0].electric_level - devs[1].electric_level)
        < cfg.device_change_diff
    )

    for i, dev in enumerate(devs):
        if i in active_set:
            if dev.standby_task and not dev.standby_task.done():
                dev.standby_task.cancel()
            dev.standby_task = None
            if dev.smart_mode == 0 and per_device[i] > 0:
                await clients[i].post(
                    {"sn": dev.sn, "properties": {"smartMode": 1}}
                )
                dev.smart_mode = 1
            dev.standby_device = False
        else:
            should_standby = state.device_active_count < state.device_count and (
                (ac_mode == 1 and cfg.standby_charging)
                or (ac_mode == 2 and cfg.standby_discharging)
            )
            should_standby = (
                should_standby
                and not transition_recent
                and not stale_get
                and not zero_power
                and not charging_diff_guard
                and dev.latest_power_cmd_zero_ts > 0
            )
            if should_standby and dev.standby_task is None:
                dev.standby_task = asyncio.ensure_future(
                    _delayed_standby(i, state, clients, cfg.standby_timer, logger)
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
) -> None:
    """Wait *delay* seconds then send smartMode=0 to put the device to sleep."""
    try:
        await asyncio.sleep(delay)
        dev = state.devices[idx]
        if idx not in state.devices_active_idx:
            zero_ts = dev.latest_power_cmd_zero_ts
            sent_key = dev.sn or f"device-{idx + 1}"
            if zero_ts > 0 and state.standby_last_sent_by_sn.get(sent_key) == zero_ts:
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
