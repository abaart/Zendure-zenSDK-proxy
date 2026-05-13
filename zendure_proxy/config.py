from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 1880
    log_level: str = "INFO"


class DeviceConfig(BaseModel):
    name: str
    host: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.host.strip())

    @property
    def base_url(self) -> str:
        host = self.host.strip()
        if host.startswith("http://") or host.startswith("https://"):
            return host.rstrip("/")
        return f"http://{host}".rstrip("/")


class ZendureConfig(BaseModel):
    timeout_seconds: float = 5.0
    cache_ttl_seconds: float = 60.0
    devices: list[DeviceConfig] = Field(default_factory=list)

    @field_validator("devices")
    @classmethod
    def names_must_be_unique(cls, devices: list[DeviceConfig]) -> list[DeviceConfig]:
        names = [device.name for device in devices if device.enabled]
        if len(names) != len(set(names)):
            raise ValueError("enabled Zendure device names must be unique")
        return devices

    @property
    def enabled_devices(self) -> list[DeviceConfig]:
        return [device for device in self.devices if device.enabled]


class ProxyConfig(BaseModel):
    language: str = "EN"
    solar_power_info: bool = False
    single_mode_upperlimit_percent: float = 100.0
    single_mode_lowerlimit_percent: float = 40.0
    single_mode_change_device_diff: float = 5.0
    single_mode_delayed_standby_timer: int = 300
    dualmode_damper_enable: bool = False
    dualmode_damper_timer: int = 120
    dualmode_damper_amount: int = 200
    balancing_factor: float = 5.0
    always_dual_mode: bool = False
    equal_mode: bool = False


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    zendure: ZendureConfig = Field(default_factory=ZendureConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)


def _deep_update(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def _parse_devices(value: str) -> list[dict[str, str]]:
    devices: list[dict[str, str]] = []
    for part in value.split(","):
        if not part.strip():
            continue
        if "=" in part:
            name, host = part.split("=", 1)
            devices.append({"name": name.strip(), "host": host.strip()})
        else:
            index = len(devices) + 1
            devices.append({"name": f"zendure{index}", "host": part.strip()})
    return devices


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(path or os.getenv("CONFIG_PATH", "config.yaml"))
    raw: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"{config_path} must contain a YAML mapping")
            raw = loaded

    overrides: dict[str, Any] = {}
    if "PORT" in os.environ:
        overrides.setdefault("server", {})["port"] = int(os.environ["PORT"])
    if "LOG_LEVEL" in os.environ:
        overrides.setdefault("server", {})["log_level"] = os.environ["LOG_LEVEL"]
    if "ZENDURE_TIMEOUT_SECONDS" in os.environ:
        overrides.setdefault("zendure", {})["timeout_seconds"] = float(
            os.environ["ZENDURE_TIMEOUT_SECONDS"]
        )
    if "ZENDURE_CACHE_TTL_SECONDS" in os.environ:
        overrides.setdefault("zendure", {})["cache_ttl_seconds"] = float(
            os.environ["ZENDURE_CACHE_TTL_SECONDS"]
        )
    if "ZENDURE_DEVICES" in os.environ:
        overrides.setdefault("zendure", {})["devices"] = _parse_devices(
            os.environ["ZENDURE_DEVICES"]
        )

    return AppConfig.model_validate(_deep_update(raw, overrides))

