from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DeviceState:
    name: str
    index: int
    serial: str = ""
    soc: float = 50.0
    min_soc: float = 10.0
    soc_set: float = 100.0
    soc_limit: int = 0
    soc_status: int = 0
    smart_mode: int = 0
    charge_max_limit: int = 2400
    inverse_max_power: int = 2400
    latest_power_cmd: int = 0
    standby: bool = False
    standby_since: float | None = None
    last_report: dict[str, Any] = field(default_factory=dict)


class ProxyState:
    def __init__(self) -> None:
        self.devices: dict[str, DeviceState] = {}
        self.latest_power_cmd: int = 0
        self.active_device_names: list[str] = []
        self.ac_mode: int = 0
        self.equal_mode: bool = False
        self.always_dual_mode: bool = False
        self.dual_mode_damper: bool = False
        self.latest_get_epoch: int = 0
        self.latest_power_message_epoch: int = 0

    def sync_devices(self, names: list[str]) -> None:
        current = set(self.devices)
        wanted = set(names)
        for missing in current - wanted:
            del self.devices[missing]
        for index, name in enumerate(names, start=1):
            if name not in self.devices:
                self.devices[name] = DeviceState(name=name, index=index)
            else:
                self.devices[name].index = index

    def update_from_report(self, device_name: str, payload: dict[str, Any]) -> None:
        props = payload.get("properties", {})
        device = self.devices[device_name]
        device.last_report = payload
        device.serial = str(payload.get("sn") or device.serial)
        device.soc = float(props.get("electricLevel", device.soc))
        device.min_soc = float(props.get("minSoc", device.min_soc * 10)) / 10
        device.soc_set = float(props.get("socSet", device.soc_set * 10)) / 10
        device.soc_limit = int(props.get("socLimit", device.soc_limit))
        device.soc_status = int(props.get("socStatus", device.soc_status))
        device.smart_mode = int(props.get("smartMode", device.smart_mode))
        device.charge_max_limit = int(props.get("chargeMaxLimit", device.charge_max_limit))
        device.inverse_max_power = int(props.get("inverseMaxPower", device.inverse_max_power))
        self.latest_get_epoch = int(time.time())

    def active_mask(self) -> int:
        mask = 0
        for name in self.active_device_names:
            device = self.devices.get(name)
            if device is not None:
                mask |= 1 << (device.index - 1)
        return mask

