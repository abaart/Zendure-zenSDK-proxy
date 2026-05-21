"""Home Assistant sensor projection for the combined proxy response."""

from __future__ import annotations

from typing import Any


SensorMap = dict[str, tuple[Any, dict[str, Any]]]


def build_proxy_ha_sensors(response: dict, battery_order_raw: Any = None) -> SensorMap:
    props = response.get("properties", {})
    pack_data = response.get("packData") or []
    health = response.get("proxyHealth") or {}
    configured_count = _int(health.get("configuredCount", 3), 3)
    unhealthy_slots = _health_slots(health, "unhealthyDevices")
    excluded_slots = _health_slots(health, "excludedDevices")
    recovering_slots = _health_slots(health, "recoveringDevices")
    degraded_slots = _health_slots(health, "degradedDevices")
    dead_slots = _health_slots(health, "deadDevices")
    unavailable_slots = excluded_slots | recovering_slots
    sensors: SensorMap = {}

    def add(entity_id: str, state: Any, friendly_name: str, **attrs: Any) -> None:
        sensors[entity_id] = (state, {"friendly_name": friendly_name, **attrs})

    for idx in range(1, 4):
        health_item = _health_item(health, idx)
        health_state = (
            "unavailable"
            if idx > configured_count
            else (
                "Dead"
                if idx in dead_slots
                else ("Degraded" if idx in degraded_slots or idx in unhealthy_slots else "Healthy")
            )
        )
        add(
            f"sensor.zendure_{idx}_health",
            health_state,
            f"Zendure {idx} Health",
            icon="mdi:heart-pulse",
            serial_number=health_item.get("serialNumber")
            or props.get(f"sn_{idx}", ""),
            ip_address=health_item.get("ipAddress")
            or props.get(f"ipAddress_{idx}", "unknown"),
            last_successful_get_age_seconds=health_item.get(
                "lastSuccessfulGetAgeSeconds"
            ),
            excluded_from_power=idx in unavailable_slots,
            recovery_seconds_remaining=health_item.get(
                "recoverySecondsRemaining", 0.0
            ),
        )
        slot_unavailable = idx in unavailable_slots
        add(
            f"sensor.zendure_{idx}_laadpercentage",
            "unavailable" if slot_unavailable else props.get(f"electricLevel_{idx}", 0),
            f"Zendure {idx} Laadpercentage",
            device_class="battery",
            unit_of_measurement="%",
            state_class="measurement",
        )
        add(
            f"sensor.vermogensopdracht_zendure_{idx}",
            "unavailable" if slot_unavailable else props.get(f"latestPowerCmd_{idx}", 0),
            f"Vermogensopdracht Zendure {idx}",
            unit_of_measurement="W",
            state_class="measurement",
            device_class="power",
        )
        directed_power = (
            "unavailable"
            if slot_unavailable
            else _directed_power(
                props.get(f"gridInputPower_{idx}", 0),
                props.get(f"outputHomePower_{idx}", 0),
            )
        )
        add(
            f"sensor.zendure_{idx}_vermogen_aansturing",
            directed_power,
            f"Zendure {idx} Vermogen Aansturing",
            unit_of_measurement="W",
            state_class="measurement",
            device_class="power",
        )
        mode = "unavailable" if slot_unavailable else _power_mode(directed_power)
        add(
            f"sensor.zendure_{idx}_modus",
            mode,
            f"Zendure {idx} Modus",
            icon=_power_mode_icon(mode),
        )
        add(
            f"sensor.zendure_{idx}_relais_stand",
            "unavailable" if slot_unavailable else _relay_state(props.get(f"acMode_{idx}", 0)),
            f"Zendure {idx} Relais Stand",
            icon="mdi:swap-vertical-bold",
        )
        add(
            f"sensor.zendure_{idx}_kalibratie_bezig",
            "unavailable"
            if slot_unavailable
            else _map_int(props.get(f"socStatus_{idx}", 0), {0: "Nee", 1: "Kalibreren"}),
            f"Zendure {idx} Kalibratie bezig",
            icon="mdi:battery-heart-variant",
        )
        add(
            f"sensor.zendure_{idx}_opslagmodus",
            "unavailable"
            if slot_unavailable
            else _map_int(
                    props.get(f"smartMode_{idx}", 0),
                    {1: "Opslaan in RAM", 0: "Opslaan in Flash"},
            ),
            f"Zendure {idx} Opslagmodus",
            icon="mdi:floppy",
        )
        soc_limit = (
            "unavailable"
            if slot_unavailable
            else _map_int(
                props.get(f"socLimit_{idx}", 0),
                {0: "Normale werking", 1: "Laadlimiet bereikt", 2: "Ontlaadlimiet bereikt"},
            )
        )
        add(
            f"sensor.zendure_{idx}_soc_limiet_status",
            soc_limit,
            f"Zendure {idx} SOC-limiet Status",
            icon=_soc_limit_icon(soc_limit),
        )
        add(
            f"sensor.zendure_{idx}_omvormer_temperatuur",
            "unavailable" if slot_unavailable else _zendure_temp(props.get(f"hyperTmp_{idx}", 2731)),
            f"Zendure {idx} Omvormer Temperatuur",
            unit_of_measurement="°C",
            state_class="measurement",
            device_class="temperature",
            icon="mdi:thermometer",
        )
        offgrid = (
            "unavailable"
            if slot_unavailable
            else _map_int(
                props.get(f"gridOffMode_{idx}", 2),
                {0: "Normaal", 1: "Eco", 2: "Uitgeschakeld"},
            )
        )
        add(
            f"sensor.zendure_{idx}_offgrid_modus",
            offgrid,
            f"Zendure {idx} Offgrid Modus",
            icon=_offgrid_icon(offgrid),
        )
        add(
            f"sensor.zendure_{idx}_serienummer",
            props.get(f"sn_{idx}", "") or health_item.get("serialNumber", ""),
            f"Zendure {idx} Serienummer",
            icon="mdi:identifier",
        )
        add(
            f"sensor.zendure_{idx}_ip_adres",
            props.get(f"ipAddress_{idx}", "unknown"),
            f"Zendure {idx} IP Adres",
            icon="mdi:ip",
        )

    unhealthy_serials = [
        item.get("serialNumber", "unknown")
        for item in health.get("unhealthyDevices", [])
    ]
    pool_degraded = (
        _int(health.get("unhealthyCount", 0)) > 0
        or _int(health.get("excludedCount", 0)) > 0
        or _int(health.get("recoveringCount", 0)) > 0
        or _int(health.get("degradedCount", 0)) > 0
        or _int(health.get("deadCount", 0)) > 0
    )
    add(
        "sensor.proxy_zendure_pool_healthy",
        "Degraded" if pool_degraded else "Healthy",
        "Proxy Zendure Pool Healthy",
        icon="mdi:battery-heart",
        configured_count=configured_count,
        healthy_count=health.get("healthyCount", configured_count),
        unhealthy_count=health.get("unhealthyCount", 0),
        excluded_count=health.get("excludedCount", 0),
        recovering_count=health.get("recoveringCount", 0),
        degraded_count=health.get("degradedCount", 0),
        dead_count=health.get("deadCount", 0),
        unhealthy_serial_numbers=unhealthy_serials,
    )

    add(
        "sensor.vermogensopdracht",
        props.get("latestPowerCmd", 0),
        "Vermogensopdracht",
        unit_of_measurement="W",
        state_class="measurement",
        device_class="power",
    )
    add(
        "sensor.zendure_actief_device",
        _active_device(props.get("activeDevice")),
        "Zendure Actief Device",
        icon="mdi:battery",
    )
    add(
        "sensor.anti_pingpong_status",
        _on_off(props.get("antiPingpongActive", 0)),
        "Reserve Mode Status",
        icon="mdi:swap-horizontal-bold",
    )
    add(
        "sensor.anti_pingpong_activatie_modus",
        props.get("antiPingpongActivationMode", "threshold"),
        "Reserve Mode Activatie Modus",
        icon="mdi:tune",
    )
    add(
        "sensor.anti_pingpong_p1_sensor",
        props.get("antiPingpongGridPowerEntity", ""),
        "Reserve Mode P1 Sensor",
        icon="mdi:flash",
    )
    add(
        "sensor.anti_pingpong_p1_sensor_bron",
        props.get("antiPingpongGridPowerEntitySource", ""),
        "Reserve Mode P1 Sensor Bron",
        icon="mdi:source-branch",
    )
    add(
        "sensor.anti_pingpong_reserve_device",
        _active_device(props.get("antiPingpongReserveDevice", 0)),
        "Reserve Mode Reserve Device",
        icon="mdi:battery-clock",
    )
    add(
        "sensor.anti_pingpong_gepauzeerd_device",
        _active_device(
            props.get(
                "antiPingpongDelayedDevice",
                props.get("antiPingpongPausedDevice", 0),
            )
        ),
        "Reserve Mode Vertraagd Device",
        icon="mdi:timer-pause",
    )
    add(
        "sensor.anti_pingpong_reserve_power",
        props.get("antiPingpongReservePower", 0),
        "Reserve Mode Reserve Power",
        unit_of_measurement="W",
        state_class="measurement",
        device_class="power",
    )
    add(
        "sensor.anti_pingpong_service_boost",
        props.get("antiPingpongServiceBoost", 0),
        "Reserve Mode Service Boost",
        unit_of_measurement="W",
        state_class="measurement",
        device_class="power",
    )
    add(
        "sensor.anti_pingpong_smart_winst_kwh",
        props.get("antiPingpongSmartGainKwh", 0),
        "Reserve Mode Smart Winst",
        unit_of_measurement="kWh",
        state_class="measurement",
        device_class="energy",
    )
    add(
        "sensor.anti_pingpong_smart_verlies_kwh",
        props.get("antiPingpongSmartLossKwh", 0),
        "Reserve Mode Smart Verlies",
        unit_of_measurement="kWh",
        state_class="measurement",
        device_class="energy",
    )
    add(
        "sensor.anti_pingpong_smart_netto_euro",
        props.get("antiPingpongSmartNetEur", 0),
        "Reserve Mode Smart Netto Euro",
        unit_of_measurement="€",
        state_class="measurement",
        icon="mdi:currency-eur",
    )
    add(
        "sensor.relay_saver_status",
        _on_off(props.get("relaySaverActive", 0)),
        "Relay Saver Status",
        icon="mdi:electric-switch",
    )
    add(
        "sensor.relay_saver_vertraagd_device",
        _active_device(props.get("relaySaverDelayedDevice", 0)),
        "Relay Saver Vertraagd Device",
        icon="mdi:timer-pause",
    )
    add(
        "sensor.relay_saver_minimumvermogen",
        props.get("relaySaverMinPower", 0),
        "Relay Saver Minimumvermogen",
        unit_of_measurement="W",
        state_class="measurement",
        device_class="power",
    )
    add(
        "sensor.relay_saver_drempel",
        props.get("relaySaverMinDropWatts", 0),
        "Relay Saver Drempel",
        unit_of_measurement="W",
        state_class="measurement",
        device_class="power",
    )
    add(
        "sensor.relay_saver_resterende_seconden",
        props.get("relaySaverRemainingSeconds", 0),
        "Relay Saver Resterende Seconden",
        unit_of_measurement="s",
        state_class="measurement",
        icon="mdi:timer-sand",
    )
    add(
        "sensor.dual_mode_demper_status",
        _on_off(props.get("dualModeDamper", 1 if props.get("dualModeDamper") else 0)),
        "Dual Mode Demper Status",
        icon="mdi:speedometer-medium",
    )
    add(
        "sensor.synchroon_laden_status",
        _on_off(props.get("equalMode", 0)),
        "Synchroon Laden Status",
        icon="mdi:battery-sync",
    )
    add(
        "sensor.beide_actief_status",
        _on_off(props.get("alwaysDualMode", 0)),
        "Beide Actief Status",
        icon="mdi:format-columns",
    )
    add(
        "sensor.zendure_proxy_versie",
        response.get("proxyVersion") or props.get("proxyVersion", "unknown"),
        "Zendure Proxy Versie",
        icon="mdi:call-split",
    )

    battery_order = _battery_order(battery_order_raw)
    for battery in range(7, 19):
        pack = _pack_at(pack_data, battery_order, battery)
        add(
            f"sensor.zendure_2400_ac_batterij_{battery}_laadpercentage",
            pack.get("socLevel", "unknown") if pack else "unknown",
            f"Zendure 2400 AC Batterij {battery} Laadpercentage",
            device_class="battery",
            unit_of_measurement="%",
            state_class="measurement",
        )
        add(
            f"sensor.zendure_2400_ac_batterij_{battery}_temperatuur",
            _zendure_temp(pack.get("maxTemp", 2731)) if pack else "unknown",
            f"Zendure 2400 AC Batterij {battery} Temperatuur",
            unit_of_measurement="°C",
            state_class="measurement",
            device_class="temperature",
            icon="mdi:thermometer",
        )

    return sensors


def _directed_power(grid_input: Any, home_output: Any) -> int:
    charging = _int(grid_input)
    discharging = -_int(home_output)
    return charging if charging != 0 else discharging


def _power_mode(power: Any) -> str:
    value = _int(power)
    if value > 0:
        return "Opladen"
    if value < 0:
        return "Ontladen"
    return "Standby"


def _power_mode_icon(state: str) -> str:
    return {
        "Opladen": "mdi:battery-plus-variant",
        "Ontladen": "mdi:battery-minus-variant",
        "Standby": "mdi:battery-outline",
    }.get(state, "mdi:battery-outline")


def _relay_state(value: Any) -> str:
    if value in (None, "", "unknown", "unavailable"):
        return "Standby"
    return _map_int(value, {0: "Standby", 1: "Oplaadstand", 2: "Ontlaadstand"})


def _zendure_temp(raw: Any) -> float:
    return round((_int(raw, 2731) - 2731) / 10.0, 1)


def _map_int(value: Any, mapping: dict[int, str]) -> str:
    return mapping.get(_int(value, -999), "Onbekend")


def _on_off(value: Any) -> str:
    return _map_int(value, {0: "Uit", 1: "Aan"})


def _active_device(value: Any) -> str:
    return {
        0: "Geen",
        1: "Zendure 1",
        2: "Zendure 2",
        3: "Zendure 1 en 2",
        4: "Zendure 3",
        5: "Zendure 1 en 3",
        6: "Zendure 2 en 3",
        7: "Alle",
    }.get(_int(value, -99), "Onbekend")


def _health_slots(health: dict, key: str) -> set[int]:
    return {
        _int(item.get("slot"), -1)
        for item in health.get(key, [])
        if isinstance(item, dict)
    }


def _health_item(health: dict, slot: int) -> dict:
    for key in ("unhealthyDevices", "excludedDevices", "recoveringDevices"):
        for item in health.get(key, []):
            if isinstance(item, dict) and _int(item.get("slot"), -1) == slot:
                return item
    return {}


def _soc_limit_icon(state: str) -> str:
    return {
        "Normale werking": "mdi:battery-medium",
        "Laadlimiet bereikt": "mdi:battery-high",
        "Ontlaadlimiet bereikt": "mdi:battery-low",
    }.get(state, "mdi:battery-outline")


def _offgrid_icon(state: str) -> str:
    return "mdi:power-plug-off" if state == "Uitgeschakeld" else "mdi:power-plug"


def _battery_order(raw: Any) -> list[int] | None:
    if raw in (None, "", "unknown", "unavailable"):
        return None
    if not isinstance(raw, str):
        return None
    try:
        return [int(part.strip()) - 1 for part in raw.split(";")]
    except ValueError:
        return None


def _pack_at(pack_data: list[dict], battery_order: list[int] | None, battery: int) -> dict | None:
    default_idx = battery - 1
    idx = default_idx
    if battery_order is not None and default_idx < len(battery_order):
        idx = battery_order[default_idx]
    if idx < 0 or idx >= len(pack_data):
        return None
    return pack_data[idx]


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default
