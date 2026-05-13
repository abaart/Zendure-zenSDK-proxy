from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class CachedPayload:
    payload: dict[str, Any]
    stored_at: float

    def age_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.stored_at)


class DeviceResponseCache:
    def __init__(self) -> None:
        self._items: dict[str, CachedPayload] = {}

    def store(self, device_id: str, payload: dict[str, Any]) -> None:
        self._items[device_id] = CachedPayload(
            payload=copy.deepcopy(payload),
            stored_at=time.monotonic(),
        )

    def get_fresh(
        self, device_id: str, max_age_seconds: float
    ) -> tuple[dict[str, Any], float] | None:
        item = self._items.get(device_id)
        if item is None:
            return None
        age = item.age_seconds()
        if age > max_age_seconds:
            return None
        return copy.deepcopy(item.payload), age

