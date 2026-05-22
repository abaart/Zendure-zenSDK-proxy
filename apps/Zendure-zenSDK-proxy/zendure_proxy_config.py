"""
Configuration dataclass and loader.

load_config(args) converts the AppDaemon args dict into a typed Config object.
All consumer code accesses settings via attribute access (cfg.server_port, etc.)
instead of dict look-ups.
"""

from __future__ import annotations

from dataclasses import dataclass, field


MAX_DEVICE_COUNT = 10


@dataclass
class DevicePowerLimit:
    charge_max_watts: int | None = None
    discharge_max_watts: int | None = None


@dataclass
class Config:
    # Required
    device_ips: list
    device_power_limits: list[DevicePowerLimit] = field(default_factory=list)
    config_warnings: list[str] = field(default_factory=list)

    # Server
    server_port: int = 8120
    server_host: str = "0.0.0.0"
    zendure_request_timeout: float = 60.0
    separate_get_post_connections: bool = True
    idle_connection_close_seconds: float = 600.0
    ha_get_response_timeout: float = 8.0
    get_cache_max_age: float = 300.0
    get_rate_limit_window: float = 1.0
    get_recovery_window: float = 30.0
    degraded_power_hold_seconds: float = 1800.0

    # Mode-switching thresholds
    single_mode_upper_pct: int = 100
    single_mode_lower_pct: int = 40
    device_change_diff: int = 5

    # Standby
    standby_timer: int = 300
    standby_charging: bool = True
    standby_discharging: bool = True

    # Device-switch transition
    transition_timer: int = 40

    # SoC balancing
    balancing_factor: int = 5
    soc_boundary_min_device_power_watts: int = 100
    soc_boundary_low_power_change_diff: int = 1

    # Dual-mode damper
    damper_enable: bool = False
    damper_timer: int = 120
    damper_amount: int = 200

    # Special modes
    always_dual_mode: bool = False
    equal_mode: bool = False

    # Reserve mode. Config keys keep the anti_pingpong prefix for compatibility.
    anti_pingpong_enable: bool = False
    anti_pingpong_activation_mode: str = "threshold"
    anti_pingpong_window_seconds: int = 180
    anti_pingpong_min_flips: int = 3
    anti_pingpong_hold_seconds: int = 300
    anti_pingpong_min_power_watts: int = 100
    anti_pingpong_reserve_count: int = 1
    anti_pingpong_reserve_power_watts: int = 40
    anti_pingpong_reserve_soc_margin_percent: int = 5
    anti_pingpong_mode_switch_delay_seconds: int = 30
    anti_pingpong_mode_switch_pause_seconds: int = 30
    anti_pingpong_mode_switch_dominance_window_seconds: int = 120
    anti_pingpong_grid_power_entity: str = ""
    anti_pingpong_grid_power_autodiscover: bool = True
    anti_pingpong_grid_power_import_positive: bool = True
    anti_pingpong_smart_window_seconds: int = 300
    anti_pingpong_smart_sample_interval_seconds: int = 1
    anti_pingpong_smart_evaluate_interval_seconds: int = 60
    anti_pingpong_smart_response_time_seconds: float = 3.0
    anti_pingpong_low_power_roundtrip_efficiency: float = 0.40
    anti_pingpong_energy_price_per_kwh: float = 0.30
    anti_pingpong_smart_disable_bad_minutes: int = 2

    # Relay saver mode. Keeps devices briefly above zero after large drops.
    relay_saver_enable: bool = False
    relay_saver_min_drop_watts: int = 900
    relay_saver_min_power_watts: int = 40
    relay_saver_hold_seconds: int = 30

    # Extras
    solar_power_info: bool = False
    manual_mode_repeat: bool = True

    # Rotating file log and AppDaemon UI dashboard
    log_file_enabled: bool = True
    log_file_path: str = ""
    log_file_max_bytes: int = 1_000_000
    log_file_backup_count: int = 5
    log_dashboard_enabled: bool = True
    log_dashboard_route: str = "zendure_proxy_logs"
    log_dashboard_lines: int = 300

    # Metrics dashboard and Home Assistant sensors
    metrics_enabled: bool = True
    metrics_dashboard_enabled: bool = True
    metrics_dashboard_route: str = "zendure_proxy_metrics"
    metrics_dashboard_refresh: int = 10
    metrics_ha_sensors_enabled: bool = True
    metrics_ha_sensors_interval: int = 30

    # Proxy response Home Assistant sensors
    proxy_ha_sensors_enabled: bool = True
    proxy_ha_sensors_skip_existing: bool = True
    proxy_ha_sensors_mqtt_discovery_enabled: bool = True
    proxy_ha_sensors_mqtt_discovery_prefix: str = "homeassistant"
    proxy_ha_sensors_mqtt_state_prefix: str = "zendure_proxy"
    proxy_ha_sensors_mqtt_retain: bool = True

    debug_payload_capture_enabled: bool = False
    diagnostics_dashboard_enabled: bool = True
    diagnostics_dashboard_route: str = "zendure_proxy_diagnostics"


def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def is_placeholder_device_ip(value: str) -> bool:
    """Return True for the placeholder IP strings from the Node-RED flow."""
    return str(value).strip() in {"192.168.x.x", "192.168.x.y"}


def load_config(args: dict) -> Config:
    device_ips, device_power_limits, config_warnings = _load_device_slots(args)
    mode_switch_delay_seconds = int(
        args.get(
            "anti_pingpong_mode_switch_delay_seconds",
            args.get("anti_pingpong_mode_switch_pause_seconds", 30),
        )
    )
    return Config(
        device_ips=device_ips,
        device_power_limits=device_power_limits,
        config_warnings=config_warnings,
        server_port=int(args.get("server_port", 8120)),
        server_host=str(args.get("server_host", "0.0.0.0")),
        zendure_request_timeout=float(args.get("zendure_request_timeout", 60.0)),
        separate_get_post_connections=_bool(
            args.get("separate_get_post_connections", True)
        ),
        idle_connection_close_seconds=float(
            args.get("idle_connection_close_seconds", 600.0)
        ),
        ha_get_response_timeout=float(args.get("ha_get_response_timeout", 8.0)),
        get_cache_max_age=float(args.get("get_cache_max_age", 300.0)),
        get_rate_limit_window=float(args.get("get_rate_limit_window", 1.0)),
        get_recovery_window=float(args.get("get_recovery_window", 30.0)),
        degraded_power_hold_seconds=float(
            args.get("degraded_power_hold_seconds", 1800.0)
        ),
        single_mode_upper_pct=int(args.get("single_mode_upperlimit_percent", 100)),
        single_mode_lower_pct=int(args.get("single_mode_lowerlimit_percent", 40)),
        device_change_diff=int(args.get("single_mode_change_device_diff", 5)),
        standby_timer=int(args.get("single_mode_delayed_standby_timer", 300)),
        standby_charging=_bool(args.get("single_mode_standby_charging_enable", True)),
        standby_discharging=_bool(args.get("single_mode_standby_discharging_enable", True)),
        transition_timer=int(args.get("singlemode_transition_timer", 40)),
        balancing_factor=int(args.get("balancing_factor", 5)),
        soc_boundary_min_device_power_watts=int(
            args.get("soc_boundary_min_device_power_watts", 100)
        ),
        soc_boundary_low_power_change_diff=int(
            args.get("soc_boundary_low_power_change_diff", 1)
        ),
        damper_enable=_bool(args.get("dualmode_damper_enable", False)),
        damper_timer=int(args.get("dualmode_damper_timer", 120)),
        damper_amount=int(args.get("dualmode_damper_amount", 200)),
        always_dual_mode=_bool(args.get("always_dual_mode", False)),
        equal_mode=_bool(args.get("equal_mode", False)),
        anti_pingpong_enable=_bool(args.get("anti_pingpong_enable", False)),
        anti_pingpong_activation_mode=str(
            args.get("anti_pingpong_activation_mode", "threshold")
        ).strip().lower(),
        anti_pingpong_window_seconds=int(
            args.get("anti_pingpong_window_seconds", 180)
        ),
        anti_pingpong_min_flips=int(args.get("anti_pingpong_min_flips", 3)),
        anti_pingpong_hold_seconds=int(args.get("anti_pingpong_hold_seconds", 300)),
        anti_pingpong_min_power_watts=int(
            args.get("anti_pingpong_min_power_watts", 100)
        ),
        anti_pingpong_reserve_count=int(args.get("anti_pingpong_reserve_count", 1)),
        anti_pingpong_reserve_power_watts=int(
            args.get("anti_pingpong_reserve_power_watts", 40)
        ),
        anti_pingpong_reserve_soc_margin_percent=int(
            args.get("anti_pingpong_reserve_soc_margin_percent", 5)
        ),
        anti_pingpong_mode_switch_delay_seconds=mode_switch_delay_seconds,
        anti_pingpong_mode_switch_pause_seconds=mode_switch_delay_seconds,
        anti_pingpong_mode_switch_dominance_window_seconds=int(
            args.get("anti_pingpong_mode_switch_dominance_window_seconds", 120)
        ),
        anti_pingpong_grid_power_entity=str(
            args.get("anti_pingpong_grid_power_entity", "")
        ).strip(),
        anti_pingpong_grid_power_autodiscover=_bool(
            args.get("anti_pingpong_grid_power_autodiscover", True)
        ),
        anti_pingpong_grid_power_import_positive=_bool(
            args.get("anti_pingpong_grid_power_import_positive", True)
        ),
        anti_pingpong_smart_window_seconds=int(
            args.get("anti_pingpong_smart_window_seconds", 300)
        ),
        anti_pingpong_smart_sample_interval_seconds=int(
            args.get("anti_pingpong_smart_sample_interval_seconds", 1)
        ),
        anti_pingpong_smart_evaluate_interval_seconds=int(
            args.get("anti_pingpong_smart_evaluate_interval_seconds", 60)
        ),
        anti_pingpong_smart_response_time_seconds=float(
            args.get("anti_pingpong_smart_response_time_seconds", 3.0)
        ),
        anti_pingpong_low_power_roundtrip_efficiency=float(
            args.get("anti_pingpong_low_power_roundtrip_efficiency", 0.40)
        ),
        anti_pingpong_energy_price_per_kwh=float(
            args.get("anti_pingpong_energy_price_per_kwh", 0.30)
        ),
        anti_pingpong_smart_disable_bad_minutes=int(
            args.get("anti_pingpong_smart_disable_bad_minutes", 2)
        ),
        relay_saver_enable=_bool(args.get("relay_saver_enable", False)),
        relay_saver_min_drop_watts=int(args.get("relay_saver_min_drop_watts", 900)),
        relay_saver_min_power_watts=int(args.get("relay_saver_min_power_watts", 40)),
        relay_saver_hold_seconds=int(args.get("relay_saver_hold_seconds", 30)),
        solar_power_info=_bool(args.get("solar_power_info", False)),
        manual_mode_repeat=_bool(args.get("manual_mode_repeat", True)),
        log_file_enabled=_bool(args.get("log_file_enabled", True)),
        log_file_path=str(args.get("log_file_path", "")).strip(),
        log_file_max_bytes=int(args.get("log_file_max_bytes", 1_000_000)),
        log_file_backup_count=int(args.get("log_file_backup_count", 5)),
        log_dashboard_enabled=_bool(args.get("log_dashboard_enabled", True)),
        log_dashboard_route=str(args.get("log_dashboard_route", "zendure_proxy_logs")).strip(),
        log_dashboard_lines=int(args.get("log_dashboard_lines", 300)),
        metrics_enabled=_bool(args.get("metrics_enabled", True)),
        metrics_dashboard_enabled=_bool(args.get("metrics_dashboard_enabled", True)),
        metrics_dashboard_route=str(args.get("metrics_dashboard_route", "zendure_proxy_metrics")).strip(),
        metrics_dashboard_refresh=int(args.get("metrics_dashboard_refresh", 10)),
        metrics_ha_sensors_enabled=_bool(args.get("metrics_ha_sensors_enabled", True)),
        metrics_ha_sensors_interval=int(args.get("metrics_ha_sensors_interval", 30)),
        proxy_ha_sensors_enabled=_bool(args.get("proxy_ha_sensors_enabled", True)),
        proxy_ha_sensors_skip_existing=_bool(
            args.get("proxy_ha_sensors_skip_existing", True)
        ),
        proxy_ha_sensors_mqtt_discovery_enabled=_bool(
            args.get("proxy_ha_sensors_mqtt_discovery_enabled", True)
        ),
        proxy_ha_sensors_mqtt_discovery_prefix=str(
            args.get("proxy_ha_sensors_mqtt_discovery_prefix", "homeassistant")
        ).strip(),
        proxy_ha_sensors_mqtt_state_prefix=str(
            args.get("proxy_ha_sensors_mqtt_state_prefix", "zendure_proxy")
        ).strip(),
        proxy_ha_sensors_mqtt_retain=_bool(
            args.get("proxy_ha_sensors_mqtt_retain", True)
        ),
        debug_payload_capture_enabled=_bool(
            args.get("debug_payload_capture_enabled", False)
        ),
        diagnostics_dashboard_enabled=_bool(
            args.get("diagnostics_dashboard_enabled", True)
        ),
        diagnostics_dashboard_route=str(
            args.get("diagnostics_dashboard_route", "zendure_proxy_diagnostics")
        ).strip(),
    )


def _load_device_slots(args: dict) -> tuple[list[str], list[DevicePowerLimit], list[str]]:
    slots = [
        {"ip": "", "limits": DevicePowerLimit()}
        for _idx in range(MAX_DEVICE_COUNT)
    ]
    warnings: list[str] = []

    for idx in range(1, MAX_DEVICE_COUNT + 1):
        raw_ip = str(args.get(f"ip_zendure_{idx}", "")).strip()
        if raw_ip and not is_placeholder_device_ip(raw_ip):
            slots[idx - 1]["ip"] = raw_ip
        slots[idx - 1]["limits"] = DevicePowerLimit(
            charge_max_watts=_cap_from_args(
                args,
                f"zendure_{idx}_charge_max_watts",
                warnings,
            ),
            discharge_max_watts=_cap_from_args(
                args,
                f"zendure_{idx}_discharge_max_watts",
                warnings,
            ),
        )

    raw_devices = args.get("devices")
    if raw_devices not in (None, ""):
        if not isinstance(raw_devices, list):
            warnings.append("devices must be a YAML list; devices value ignored")
        else:
            for idx, item in enumerate(raw_devices):
                slot = idx + 1
                if slot > MAX_DEVICE_COUNT:
                    warnings.append(
                        f"devices[{idx}] ignored because max device count is {MAX_DEVICE_COUNT}"
                    )
                    continue
                if not isinstance(item, dict):
                    warnings.append(f"devices[{idx}] must be a mapping; entry ignored")
                    continue
                raw_ip = str(item.get("ip", "")).strip()
                if not raw_ip or is_placeholder_device_ip(raw_ip):
                    continue
                slots[idx]["ip"] = raw_ip
                slots[idx]["limits"] = DevicePowerLimit(
                    charge_max_watts=_cap_from_mapping(
                        item,
                        "charge_max_watts",
                        f"devices[{idx}].charge_max_watts",
                        warnings,
                    ),
                    discharge_max_watts=_cap_from_mapping(
                        item,
                        "discharge_max_watts",
                        f"devices[{idx}].discharge_max_watts",
                        warnings,
                    ),
                )

    device_ips: list[str] = []
    device_power_limits: list[DevicePowerLimit] = []
    for slot in slots:
        ip = str(slot["ip"]).strip()
        if not ip:
            continue
        device_ips.append(ip)
        device_power_limits.append(slot["limits"])

    return device_ips, device_power_limits, warnings


def _cap_from_args(args: dict, key: str, warnings: list[str]) -> int | None:
    if key not in args:
        return None
    return _parse_positive_int(args.get(key), key, warnings)


def _cap_from_mapping(
    item: dict,
    key: str,
    label: str,
    warnings: list[str],
) -> int | None:
    if key not in item:
        return None
    return _parse_positive_int(item.get(key), label, warnings)


def _parse_positive_int(value, label: str, warnings: list[str]) -> int | None:
    if value in (None, ""):
        return None
    try:
        cap = int(float(value))
    except (TypeError, ValueError):
        warnings.append(f"{label} must be a positive watt value; value ignored")
        return None
    if cap <= 0:
        warnings.append(f"{label} must be greater than 0 watts; value ignored")
        return None
    return cap
