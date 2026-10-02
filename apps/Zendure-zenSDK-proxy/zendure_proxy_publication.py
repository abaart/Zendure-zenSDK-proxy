"""Per-entity publication decisions; callers record only successful writes."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any


@dataclass(frozen=True)
class PublicationPolicy:
    change_interval: int = 0
    heartbeat: int = 3600
    state_transitions: bool = False


def publication_policy(entity_id: str) -> PublicationPolicy:
    if entity_id == "sensor.zendure_proxy_versie" or re.fullmatch(
        r"sensor.zendure_\d+_(serienummer|ip_adres)", entity_id
    ):
        return PublicationPolicy(heartbeat=86400)
    if entity_id.endswith("_temperatuur"):
        return PublicationPolicy(600)
    if entity_id == "sensor.proxy_zendure_pool_healthy" or re.fullmatch(
        r"sensor.zendure_\d+_health", entity_id
    ):
        return PublicationPolicy(60, state_transitions=True)
    if entity_id == "sensor.relay_saver_resterende_seconden":
        return PublicationPolicy(10)
    if entity_id.startswith("sensor.anti_pingpong_smart_"):
        return PublicationPolicy(60)
    if entity_id.startswith("sensor.zendure_proxy_"):
        return PublicationPolicy(10 if entity_id.endswith("_depth") else 60)
    return PublicationPolicy()


class SensorPublications:
    def __init__(self) -> None:
        self.published: dict[str, tuple[str, dict[str, Any], float]] = {}

    def due(self, entity_id: str, state: str, attributes: dict, ts: float,
            *, heartbeat_lead_seconds: int = 0) -> bool:
        previous = self.published.get(entity_id)
        if previous is None:
            return True
        old_state, old_attributes, last_ts = previous
        policy = publication_policy(entity_id)
        elapsed = ts - last_ts
        if elapsed >= policy.heartbeat - heartbeat_lead_seconds:
            return True
        if old_state != state:
            if policy.state_transitions:
                return True
            if state in {"unavailable", "unknown"} or old_state in {"unavailable", "unknown"}:
                return True
            if entity_id == "sensor.relay_saver_resterende_seconden" and state in {"0", "0.0"}:
                return True
        changed = state != old_state or self._comparable(attributes) != self._comparable(old_attributes)
        return changed and elapsed >= policy.change_interval

    def record(self, entity_id: str, state: str, attributes: dict, ts: float) -> None:
        self.published[entity_id] = (state, deepcopy(attributes), ts)

    @staticmethod
    def _comparable(attributes: dict) -> dict:
        # Fresh GET timestamps must not turn constant measurements into writes.
        return {key: value for key, value in attributes.items()
                if key not in {"proxy_updated_at", "proxy_last_successful_get_at",
                               "proxy_last_successful_get_at_by_slot"}}
