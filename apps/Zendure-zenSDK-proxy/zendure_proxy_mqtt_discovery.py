"""MQTT discovery payloads for Home Assistant proxy sensors."""

from __future__ import annotations

from typing import Any


def mqtt_sensor_topics(
    entity_id: str,
    discovery_prefix: str,
    state_prefix: str,
) -> tuple[str, str, str]:
    object_id = _object_id(entity_id)
    discovery = f"{discovery_prefix}/sensor/zendure_proxy/{object_id}/config"
    state = f"{state_prefix}/sensor/{object_id}/state"
    attrs = f"{state_prefix}/sensor/{object_id}/attributes"
    return discovery, state, attrs


def mqtt_sensor_config(
    entity_id: str,
    attributes: dict[str, Any],
    discovery_prefix: str,
    state_prefix: str,
) -> dict[str, Any]:
    object_id = _object_id(entity_id)
    _discovery_topic, state_topic, attrs_topic = mqtt_sensor_topics(
        entity_id, discovery_prefix, state_prefix
    )
    payload: dict[str, Any] = {
        "name": attributes.get("friendly_name", object_id.replace("_", " ").title()),
        "unique_id": f"zendure_proxy_{object_id}",
        "default_entity_id": entity_id,
        "state_topic": state_topic,
        "json_attributes_topic": attrs_topic,
        "force_update": True,
        "device": {
            "identifiers": ["zendure_proxy"],
            "name": "Zendure zenSDK Proxy",
            "manufacturer": "Zendure",
            "model": "zenSDK Proxy",
        },
    }
    for key in ("unit_of_measurement", "device_class", "state_class", "icon"):
        if key in attributes:
            payload[key] = attributes[key]
    return payload


def _object_id(entity_id: str) -> str:
    return entity_id.split(".", 1)[-1].replace("-", "_")
