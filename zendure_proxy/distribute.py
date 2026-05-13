from __future__ import annotations

import copy
import math
import time
from dataclasses import dataclass
from typing import Any

from . import metrics
from .config import DeviceConfig, ProxyConfig
from .state import ProxyState


@dataclass
class DeviceWrite:
    device: DeviceConfig
    payload: dict[str, Any]


def _props(payload: dict[str, Any]) -> dict[str, Any]:
    props = payload.setdefault("properties", {})
    if not isinstance(props, dict):
        raise ValueError("properties must be an object")
    return props


def _split_even(value: int, count: int) -> list[int]:
    if count <= 0:
        return []
    base = math.floor(value / count)
    values = [base] * count
    remainder = value - sum(values)
    for index in range(remainder):
        values[index] += 1
    return values


def _choose_active_devices(
    state: ProxyState,
    devices: list[DeviceConfig],
    direction: str,
    requested_power: int,
    proxy_config: ProxyConfig,
) -> list[DeviceConfig]:
    if requested_power <= 0:
        return []
    if proxy_config.always_dual_mode or proxy_config.equal_mode or state.always_dual_mode or state.equal_mode:
        return devices

    scored: list[tuple[float, DeviceConfig]] = []
    for device in devices:
        device_state = state.devices[device.name]
        if direction == "charge":
            if device_state.soc_limit == 1 or device_state.soc_status == 1:
                continue
            score = device_state.soc
        else:
            if device_state.soc_limit == 2 or device_state.soc_status == 1:
                continue
            score = -device_state.soc
        scored.append((score, device))
    scored.sort(key=lambda item: item[0])
    candidates = [device for _, device in scored] or devices

    per_device_max = []
    for device in candidates:
        device_state = state.devices[device.name]
        limit = (
            device_state.charge_max_limit
            if direction == "charge"
            else device_state.inverse_max_power
        )
        per_device_max.append(max(limit, 1))

    active: list[DeviceConfig] = []
    capacity = 0
    for device, limit in zip(candidates, per_device_max):
        active.append(device)
        capacity += limit
        threshold = limit * proxy_config.single_mode_upperlimit_percent / 100
        if requested_power <= capacity and requested_power <= max(threshold, 1):
            break
    return active


def _weighted_distribution(
    state: ProxyState,
    active: list[DeviceConfig],
    direction: str,
    requested_power: int,
    proxy_config: ProxyConfig,
) -> dict[str, int]:
    if not active:
        return {}
    if proxy_config.equal_mode or state.equal_mode:
        return dict(zip([device.name for device in active], _split_even(requested_power, len(active))))

    weights: dict[str, float] = {}
    limits: dict[str, int] = {}
    for device in active:
        device_state = state.devices[device.name]
        if direction == "charge":
            available = max(device_state.soc_set - device_state.soc, 0)
            limits[device.name] = max(device_state.charge_max_limit, 0)
        else:
            available = max(device_state.soc - device_state.min_soc, 0)
            limits[device.name] = max(device_state.inverse_max_power, 0)
        weights[device.name] = max(available, 0) ** max(proxy_config.balancing_factor, 1)

    if sum(weights.values()) <= 0:
        weights = {device.name: 1 for device in active}

    powers = {device.name: 0.0 for device in active}
    remaining = float(requested_power)
    while remaining > 0.5:
        open_names = [name for name in powers if powers[name] < limits[name]]
        if not open_names:
            break
        total_weight = sum(weights[name] for name in open_names)
        if total_weight <= 0:
            total_weight = len(open_names)
            weights.update({name: 1 for name in open_names})
        distributed = 0.0
        for name in open_names:
            add = remaining * (weights[name] / total_weight)
            add = min(add, limits[name] - powers[name])
            powers[name] += add
            distributed += add
        if distributed <= 0.5:
            break
        remaining -= distributed

    rounded = {name: int(math.floor(power)) for name, power in powers.items()}
    leftover = requested_power - sum(rounded.values())
    for name in sorted(rounded, key=lambda item: limits[item] - rounded[item], reverse=True):
        if leftover <= 0:
            break
        if rounded[name] < limits[name]:
            rounded[name] += 1
            leftover -= 1
    return rounded


def build_device_writes(
    incoming: dict[str, Any],
    devices: list[DeviceConfig],
    state: ProxyState,
    proxy_config: ProxyConfig,
) -> list[DeviceWrite]:
    state.sync_devices([device.name for device in devices])
    properties = _props(incoming)

    if "equalMode" in properties:
        state.equal_mode = bool(properties["equalMode"])
        return []
    if "alwaysDualMode" in properties:
        state.always_dual_mode = bool(properties["alwaysDualMode"])
        return []
    if "dualModeDamper" in properties:
        state.dual_mode_damper = bool(properties["dualModeDamper"])
        return []

    if "acMode" in properties:
        state.ac_mode = int(properties["acMode"])

    writes: dict[str, dict[str, Any]] = {}
    for device in devices:
        payload = copy.deepcopy(incoming)
        payload["sn"] = state.devices[device.name].serial
        writes[device.name] = payload

    active_names: list[str] = []
    latest_powers = {device.name: 0 for device in devices}

    if "inputLimit" in properties:
        requested = int(properties["inputLimit"])
        if requested <= 0:
            for payload in writes.values():
                _props(payload)["inputLimit"] = 0
            state.latest_power_cmd = 0
        else:
            active = _choose_active_devices(state, devices, "charge", requested, proxy_config)
            power_by_device = _weighted_distribution(
                state, active, "charge", requested, proxy_config
            )
            active_names = [device.name for device in active]
            for device in devices:
                power = power_by_device.get(device.name, 0)
                _props(writes[device.name])["inputLimit"] = power
                latest_powers[device.name] = power
            state.latest_power_cmd = requested

    if "outputLimit" in properties:
        requested = int(properties["outputLimit"])
        if requested <= 0:
            for payload in writes.values():
                _props(payload)["outputLimit"] = 0
            state.latest_power_cmd = 0
        else:
            active = _choose_active_devices(state, devices, "discharge", requested, proxy_config)
            power_by_device = _weighted_distribution(
                state, active, "discharge", requested, proxy_config
            )
            active_names = [device.name for device in active]
            for device in devices:
                power = power_by_device.get(device.name, 0)
                _props(writes[device.name])["outputLimit"] = power
                latest_powers[device.name] = -power
            state.latest_power_cmd = -requested

    if "chargeMaxLimit" in properties:
        values = _split_even(int(properties["chargeMaxLimit"]), len(devices))
        for device, value in zip(devices, values):
            _props(writes[device.name])["chargeMaxLimit"] = value

    if "inverseMaxPower" in properties:
        values = _split_even(int(properties["inverseMaxPower"]), len(devices))
        for device, value in zip(devices, values):
            _props(writes[device.name])["inverseMaxPower"] = value

    power_message = "inputLimit" in properties or "outputLimit" in properties
    if power_message:
        previous_active = set(state.active_device_names)
        new_active = set(active_names)
        now = time.monotonic()
        for name in previous_active - new_active:
            state.devices[name].standby_since = now
        for name in new_active:
            state.devices[name].standby_since = None
            state.devices[name].standby = False
        state.active_device_names = active_names

    device_writes: list[DeviceWrite] = []
    for device in devices:
        device_state = state.devices[device.name]
        power = latest_powers[device.name]
        device_state.latest_power_cmd = power
        if power:
            device_state.standby = False
            _props(writes[device.name])["smartMode"] = 1
            if state.ac_mode:
                _props(writes[device.name])["acMode"] = state.ac_mode
        metrics.LATEST_POWER.labels(device.name).set(power)

        if device_state.standby and power_message and power == 0:
            continue
        device_writes.append(DeviceWrite(device=device, payload=writes[device.name]))

    state.latest_power_message_epoch = int(time.time())
    return device_writes
