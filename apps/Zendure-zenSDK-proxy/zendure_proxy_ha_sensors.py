"""Home Assistant sensor projection for the combined proxy response."""

from __future__ import annotations

from typing import Any


SensorMap = dict[str, tuple[Any, dict[str, Any]]]


def build_proxy_ha_sensors(response: dict, battery_order_raw: str | None = None) -> SensorMap:
    props = response.get("properties", {})
    pack_data = response.get("packData") or []
    sensors: SensorMap = {}

    def add(entity_id: str, state: Any, friendly_name: str, **attrs: Any) -> None:
        sensors[entity_id] = (state, {"friendly_name": friendly_name, **attrs})

    for idx in range(1, 4):
        add(
            f"sensor.zendure_{idx}_laadpercentage",
            props.get(f"electricLevel_{idx}", 0),
            f"Zendure {idx} Laadpercentage",
            device_class="battery",
            unit_of_measurement="%",
            state_class="measurement",
        )
        add(
            f"sensor.vermogensopdracht_zendure_{idx}",
            props.get(f"latestPowerCmd_{idx}", 0),
            f"Vermogensopdracht Zendure {idx}",
            unit_of_measurement="W",
            state_class="measurement",
            device_class="power",
        )
        add(
            f"sensor.zendure_{idx}_vermogen_aansturing",
            _directed_power(
                props.get(f"gridInputPower_{idx}", 0),
                props.get(f"outputHomePower_{idx}", 0),
            ),
            f"Zendure {idx} Vermogen Aansturing",
            unit_of_measurement="W",
            state_class="measurement",
            device_class="power",
        )
        add(
            f"sensor.zendure_{idx}_kalibratie_bezig",
            _map_int(props.get(f"socStatus_{idx}", 0), {0: "Nee", 1: "Kalibreren"}),
            f"Zendure {idx} Kalibratie bezig",
            icon="mdi:battery-heart-variant",
        )
        add(
            f"sensor.zendure_{idx}_opslagmodus",
            _map_int(
                props.get(f"smartMode_{idx}", 0),
                {1: "Opslaan in RAM", 0: "Opslaan in Flash"},
            ),
            f"Zendure {idx} Opslagmodus",
            icon="mdi:floppy",
        )
        soc_limit = _map_int(
            props.get(f"socLimit_{idx}", 0),
            {0: "Normale werking", 1: "Laadlimiet bereikt", 2: "Ontlaadlimiet bereikt"},
        )
        add(
            f"sensor.zendure_{idx}_soc_limiet_status",
            soc_limit,
            f"Zendure {idx} SOC-limiet Status",
            icon=_soc_limit_icon(soc_limit),
        )
        add(
            f"sensor.zendure_{idx}_omvormer_temperatuur",
            _zendure_temp(props.get(f"hyperTmp_{idx}", 2731)),
            f"Zendure {idx} Omvormer Temperatuur",
            unit_of_measurement="°C",
            state_class="measurement",
            device_class="temperature",
            icon="mdi:thermometer",
        )
        offgrid = _map_int(
            props.get(f"gridOffMode_{idx}", 2),
            {0: "Normaal", 1: "Eco", 2: "Uitgeschakeld"},
        )
        add(
            f"sensor.zendure_{idx}_offgrid_modus",
            offgrid,
            f"Zendure {idx} Offgrid Modus",
            icon=_offgrid_icon(offgrid),
        )
        add(
            f"sensor.zendure_{idx}_serienummer",
            props.get(f"sn_{idx}", ""),
            f"Zendure {idx} Serienummer",
            icon="mdi:identifier",
        )
        add(
            f"sensor.zendure_{idx}_ip_adres",
            props.get(f"ipAddress_{idx}", "unknown"),
            f"Zendure {idx} IP Adres",
            icon="mdi:ip",
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


def _soc_limit_icon(state: str) -> str:
    return {
        "Normale werking": "mdi:battery-medium",
        "Laadlimiet bereikt": "mdi:battery-high",
        "Ontlaadlimiet bereikt": "mdi:battery-low",
    }.get(state, "mdi:battery-outline")


def _offgrid_icon(state: str) -> str:
    return "mdi:power-plug-off" if state == "Uitgeschakeld" else "mdi:power-plug"


def _battery_order(raw: str | None) -> list[int] | None:
    if raw in (None, "", "unknown", "unavailable"):
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
