from __future__ import annotations

import time

from zendure_proxy.cache import DeviceResponseCache


def test_cache_returns_fresh_payload() -> None:
    cache = DeviceResponseCache()
    cache.store("zendure1", {"ok": True})

    cached = cache.get_fresh("zendure1", 60)

    assert cached is not None
    payload, age = cached
    assert payload == {"ok": True}
    assert age >= 0


def test_cache_ignores_expired_payload() -> None:
    cache = DeviceResponseCache()
    cache.store("zendure1", {"ok": True})
    cache._items["zendure1"].stored_at = time.monotonic() - 61

    assert cache.get_fresh("zendure1", 60) is None

