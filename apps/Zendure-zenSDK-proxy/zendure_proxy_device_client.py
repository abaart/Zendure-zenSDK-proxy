"""
DeviceClient: one persistent HTTP session + exclusive asyncio.Lock per device.

Guarantees at most one in-flight request per physical Zendure device, which
matches the "one HTTP session per device" contract described in the proxy spec.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Optional

import aiohttp


class DeviceClient:
    """Thin wrapper around aiohttp with a per-device lock."""

    def __init__(self, ip: str, logger: Callable):
        self.ip = ip
        self._log = logger
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        )
        self._lock = asyncio.Lock()

    async def get(self) -> Optional[dict]:
        """GET /properties/report, serialised through the device lock."""
        async with self._lock:
            try:
                async with self._session.get(
                    f"http://{self.ip}/properties/report"
                ) as resp:
                    if resp.status == 200:
                        return await resp.json(content_type=None)
                    self._log(
                        f"Device {self.ip} GET HTTP {resp.status}", level="WARNING"
                    )
                    return None
            except Exception as exc:
                self._log(f"Device {self.ip} GET error: {exc}", level="WARNING")
                return None

    async def post(self, payload: dict) -> dict:
        """POST /properties/write, serialised through the device lock."""
        async with self._lock:
            try:
                async with self._session.post(
                    f"http://{self.ip}/properties/write", json=payload
                ) as resp:
                    if resp.status == 200:
                        return await resp.json(content_type=None)
                    self._log(
                        f"Device {self.ip} POST HTTP {resp.status}", level="WARNING"
                    )
                    return {"ack": "pong"}
            except Exception as exc:
                self._log(f"Device {self.ip} POST error: {exc}", level="WARNING")
                return {"ack": "pong"}

    async def close(self):
        await self._session.close()
