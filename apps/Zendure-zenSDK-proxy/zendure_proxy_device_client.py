"""
DeviceClient: one persistent HTTP session + one outgoing queue per device.

Guarantees at most one in-flight request per physical Zendure device. Incoming
Home Assistant requests can arrive concurrently; outgoing Zendure requests are
serialized by the worker task owned by this DeviceClient.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import time
from typing import Callable, Optional

import aiohttp


@dataclass
class DeviceRequest:
    method: str
    payload: Optional[dict]
    future: asyncio.Future


class DeviceClient:
    """Thin wrapper around aiohttp with a per-device outgoing queue."""

    def __init__(self, ip: str, logger: Callable, metrics=None, device_idx: int = 0):
        self.ip = ip
        self._log = logger
        self._metrics = metrics
        self._device_idx = device_idx
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        )
        self._queue: asyncio.Queue[DeviceRequest] = asyncio.Queue()
        self._worker_task = asyncio.ensure_future(self._worker())

    async def get(self) -> Optional[dict]:
        """Queue GET /properties/report and await the worker result."""
        return await self._enqueue("GET", None)

    async def post(self, payload: dict) -> dict:
        """Queue POST /properties/write and await the worker result."""
        return await self._enqueue("POST", payload)

    async def _enqueue(self, method: str, payload: Optional[dict]):
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        await self._queue.put(DeviceRequest(method=method, payload=payload, future=fut))
        self._set_queue_depth()
        return await fut

    async def _worker(self) -> None:
        while True:
            request = await self._queue.get()
            self._set_queue_depth()
            try:
                if request.method == "GET":
                    result = await self._execute_get()
                else:
                    result = await self._execute_post(request.payload or {})
                if not request.future.done():
                    request.future.set_result(result)
            except asyncio.CancelledError:
                if not request.future.done():
                    request.future.cancel()
                raise
            except Exception as exc:
                if not request.future.done():
                    if request.method == "GET":
                        request.future.set_result(None)
                    else:
                        request.future.set_result({"ack": "pong"})
                self._log(
                    f"Device {self.ip} queue worker error: {exc}",
                    level="WARNING",
                )
            finally:
                self._queue.task_done()
                self._set_queue_depth()

    async def _execute_get(self) -> Optional[dict]:
        start = time.monotonic()
        if self._metrics is not None:
            self._metrics.start_outgoing(self._device_idx, "GET")
        success = False
        try:
            async with self._session.get(
                f"http://{self.ip}/properties/report"
            ) as resp:
                if resp.status == 200:
                    success = True
                    return await resp.json(content_type=None)
                self._log(
                    f"Device {self.ip} GET HTTP {resp.status}", level="WARNING"
                )
                return None
        except Exception as exc:
            self._log(f"Device {self.ip} GET error: {exc}", level="WARNING")
            return None
        finally:
            if self._metrics is not None:
                self._metrics.finish_outgoing(
                    self._device_idx,
                    "GET",
                    (time.monotonic() - start) * 1000.0,
                    success,
                )

    async def _execute_post(self, payload: dict) -> dict:
        start = time.monotonic()
        if self._metrics is not None:
            self._metrics.start_outgoing(self._device_idx, "POST")
        success = False
        try:
            async with self._session.post(
                f"http://{self.ip}/properties/write", json=payload
            ) as resp:
                if resp.status == 200:
                    success = True
                    return await resp.json(content_type=None)
                self._log(
                    f"Device {self.ip} POST HTTP {resp.status}", level="WARNING"
                )
                return {"ack": "pong"}
        except Exception as exc:
            self._log(f"Device {self.ip} POST error: {exc}", level="WARNING")
            return {"ack": "pong"}
        finally:
            if self._metrics is not None:
                self._metrics.finish_outgoing(
                    self._device_idx,
                    "POST",
                    (time.monotonic() - start) * 1000.0,
                    success,
                )

    async def close(self):
        self._worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._worker_task
        while not self._queue.empty():
            request = self._queue.get_nowait()
            if not request.future.done():
                request.future.cancel()
            self._queue.task_done()
        self._set_queue_depth()
        await self._session.close()

    def _set_queue_depth(self) -> None:
        if self._metrics is not None:
            self._metrics.set_outgoing_queue_depth(self._device_idx, self._queue.qsize())
