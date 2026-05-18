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
        standby_charging=bool(args.get("single_mode_standby_charging_enable", True)),
        standby_discharging=bool(args.get("single_mode_standby_discharging_enable", True)),
        transition_timer=int(args.get("singlemode_transition_timer", 40)),
        balancing_factor=int(args.get("balancing_factor", 5)),
        damper_enable=bool(args.get("dualmode_damper_enable", False)),
        damper_timer=int(args.get("dualmode_damper_timer", 120)),
        damper_amount=int(args.get("dualmode_damper_amount", 200)),
        always_dual_mode=bool(args.get("always_dual_mode", False)),
        equal_mode=bool(args.get("equal_mode", False)),
        solar_power_info=bool(args.get("solar_power_info", False)),
        manual_mode_repeat=bool(args.get("manual_mode_repeat", True)),
    )
