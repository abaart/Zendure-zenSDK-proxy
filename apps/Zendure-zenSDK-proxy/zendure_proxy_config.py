"""
Configuration dataclass and loader.

load_config(args) converts the AppDaemon args dict into a typed Config object.
All consumer code accesses settings via attribute access (cfg.server_port, etc.)
instead of dict look-ups.
"""

from dataclasses import dataclass


@dataclass
class Config:
    # Required
    device_ips: list

    # Server
    server_port: int = 8120
    server_host: str = "0.0.0.0"

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

    # Dual-mode damper
    damper_enable: bool = False
    damper_timer: int = 120
    damper_amount: int = 200

    # Special modes
    always_dual_mode: bool = False
    equal_mode: bool = False

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


def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def load_config(args: dict) -> Config:
    raw = [
        str(args.get("ip_zendure_1", "")).strip(),
        str(args.get("ip_zendure_2", "")).strip(),
        str(args.get("ip_zendure_3", "")).strip(),
    ]
    return Config(
        device_ips=[ip for ip in raw if ip],
        server_port=int(args.get("server_port", 8120)),
        server_host=str(args.get("server_host", "0.0.0.0")),
        single_mode_upper_pct=int(args.get("single_mode_upperlimit_percent", 100)),
        single_mode_lower_pct=int(args.get("single_mode_lowerlimit_percent", 40)),
        device_change_diff=int(args.get("single_mode_change_device_diff", 5)),
        standby_timer=int(args.get("single_mode_delayed_standby_timer", 300)),
        standby_charging=_bool(args.get("single_mode_standby_charging_enable", True)),
        standby_discharging=_bool(args.get("single_mode_standby_discharging_enable", True)),
        transition_timer=int(args.get("singlemode_transition_timer", 40)),
        balancing_factor=int(args.get("balancing_factor", 5)),
        damper_enable=_bool(args.get("dualmode_damper_enable", False)),
        damper_timer=int(args.get("dualmode_damper_timer", 120)),
        damper_amount=int(args.get("dualmode_damper_amount", 200)),
        always_dual_mode=_bool(args.get("always_dual_mode", False)),
        equal_mode=_bool(args.get("equal_mode", False)),
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
    )
