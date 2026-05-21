"""
DeviceClient: persistent HTTP sessions and outgoing queues per device.

With separate_get_post_connections=True, one GET worker/session and one POST
worker/session are used per physical Zendure device. That allows at most one
in-flight GET and at most one in-flight POST for the same Zendure device.

With separate_get_post_connections=False, GET and POST share one worker/session.
That legacy mode allows at most one in-flight request for the same Zendure
device.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import time
from typing import Callable, Optional

import aiohttp


def build_device_url(ip: str, endpoint: str, local_proxy_url: str = "") -> str:
    """Build Zendure device URL, including Node-RED testdevice loopback URLs."""
    endpoint = endpoint.strip("/")
    if str(ip).startswith("testdevice") and local_proxy_url:
        proxy_url = str(local_proxy_url).strip()
        if not proxy_url.startswith(("http://", "https://")):
            proxy_url = f"http://{proxy_url}"
        endpoint_suffix = f"/{endpoint}"
        if proxy_url.endswith(endpoint_suffix):
            proxy_url = proxy_url[: -len(endpoint_suffix)]
        return f"{proxy_url.rstrip('/')}/{ip}/{endpoint}"
    return f"http://{ip}/{endpoint}"


@dataclass
class DeviceRequest:
    method: str
    payload: Optional[dict]
    future: asyncio.Future


class DeviceClient:
    """Thin wrapper around aiohttp with a per-device outgoing queue."""

    def __init__(
        self,
        ip: str,
        logger: Callable,
        metrics=None,
        device_idx: int = 0,
        request_timeout: float = 60.0,
        separate_get_post_connections: bool = True,
        idle_connection_close_seconds: float = 600.0,
    ):
        self.ip = ip
        self._log = logger
        self._metrics = metrics
        self._device_idx = device_idx
        self._local_proxy_url = ""
        self._request_timeout = request_timeout
        self._separate_get_post_connections = separate_get_post_connections
        self._idle_connection_close_seconds = idle_connection_close_seconds
        self._queues: dict[str, asyncio.Queue[DeviceRequest]] = {}
        self._sessions: dict[str, Optional[aiohttp.ClientSession]] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._active_requests: dict[str, int] = {}
        self._last_activity_ts: dict[str, float] = {}

        session_keys = ["GET", "POST"] if separate_get_post_connections else ["SHARED"]
        for key in session_keys:
            self._sessions[key] = self._create_session()
            self._session_locks[key] = asyncio.Lock()
            self._active_requests[key] = 0
            self._last_activity_ts[key] = time.monotonic()

        self._queues["GET"] = asyncio.Queue()
        self._queues["POST"] = (
            asyncio.Queue()
            if separate_get_post_connections
            else self._queues["GET"]
        )
        worker_queues = [self._queues["GET"]]
        if separate_get_post_connections:
            worker_queues.append(self._queues["POST"])
        self._worker_tasks = [
            asyncio.ensure_future(self._worker(queue)) for queue in worker_queues
        ]
        self._idle_task = (
            asyncio.ensure_future(self._idle_session_cleanup())
            if idle_connection_close_seconds > 0
            else None
        )

    async def get(self) -> Optional[dict]:
        """Queue GET /properties/report and await the worker result."""
        return await self._enqueue("GET", None)

    async def post(self, payload: dict) -> dict:
        """Queue POST /properties/write and await the worker result."""
        return await self._enqueue("POST", payload)

    def set_local_proxy_url(self, local_proxy_url: str) -> None:
        """Store the last proxy URL for testdevice loopback requests."""
        self._local_proxy_url = local_proxy_url

    async def _enqueue(self, method: str, payload: Optional[dict]):
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        await self._queues[method].put(
            DeviceRequest(method=method, payload=payload, future=fut)
        )
        self._set_queue_depth()
        return await fut

    async def _worker(self, queue: asyncio.Queue[DeviceRequest]) -> None:
        while True:
            request = await queue.get()
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
                queue.task_done()
                self._set_queue_depth()

    async def _execute_get(self) -> Optional[dict]:
        start = time.monotonic()
        if self._metrics is not None:
            self._metrics.start_outgoing(self._device_idx, "GET")
        success = False
        session = await self._start_request("GET")
        try:
            async with session.get(
                build_device_url(self.ip, "properties/report", self._local_proxy_url)
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
            await self._finish_request("GET")

    async def _execute_post(self, payload: dict) -> dict:
        start = time.monotonic()
        if self._metrics is not None:
            self._metrics.start_outgoing(self._device_idx, "POST")
        success = False
        session = await self._start_request("POST")
        try:
            async with session.post(
                build_device_url(self.ip, "properties/write", self._local_proxy_url),
                json=payload,
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
            await self._finish_request("POST")

    async def close_post_connection(self) -> None:
        """Close the POST HTTP session so the next POST creates a fresh socket."""
        await self._close_session(self._session_key("POST"))

    async def close_idle_connections(self, current_ts: float | None = None) -> None:
        """Close HTTP sessions that have been idle longer than the configured limit."""
        if self._idle_connection_close_seconds <= 0:
            return
        ts = time.monotonic() if current_ts is None else current_ts
        for key in list(self._sessions):
            if self._active_requests.get(key, 0) > 0:
                continue
            last_activity = self._last_activity_ts.get(key, ts)
            if ts - last_activity >= self._idle_connection_close_seconds:
                await self._close_session(key)

    async def close(self):
        if self._idle_task is not None:
            self._idle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._idle_task
        for task in self._worker_tasks:
            task.cancel()
        for task in self._worker_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for queue in self._unique_queues():
            while not queue.empty():
                request = queue.get_nowait()
                if not request.future.done():
                    request.future.cancel()
                queue.task_done()
        self._set_queue_depth()
        for key in list(self._sessions):
            await self._close_session(key, force=True)

    def _set_queue_depth(self) -> None:
        if self._metrics is not None:
            depth = sum(queue.qsize() for queue in self._unique_queues())
            self._metrics.set_outgoing_queue_depth(self._device_idx, depth)

    def _session_key(self, method: str) -> str:
        return method if self._separate_get_post_connections else "SHARED"

    def _create_session(self) -> aiohttp.ClientSession:
        kwargs = {
            "timeout": aiohttp.ClientTimeout(total=self._request_timeout),
        }
        connector_cls = getattr(aiohttp, "TCPConnector", None)
        if connector_cls is not None:
            connector_kwargs = {"limit": 1, "limit_per_host": 1}
            if self._idle_connection_close_seconds > 0:
                connector_kwargs["keepalive_timeout"] = (
                    self._idle_connection_close_seconds
                )
            kwargs["connector"] = connector_cls(**connector_kwargs)
        return aiohttp.ClientSession(**kwargs)

    async def _start_request(self, method: str) -> aiohttp.ClientSession:
        key = self._session_key(method)
        async with self._session_locks[key]:
            session = self._sessions.get(key)
            if session is None:
                session = self._create_session()
                self._sessions[key] = session
            self._active_requests[key] += 1
            return session

    async def _finish_request(self, method: str) -> None:
        key = self._session_key(method)
        async with self._session_locks[key]:
            self._active_requests[key] = max(0, self._active_requests[key] - 1)
            self._last_activity_ts[key] = time.monotonic()

    async def _close_session(self, key: str, *, force: bool = False) -> None:
        session = None
        async with self._session_locks[key]:
            if not force and self._active_requests.get(key, 0) > 0:
                return
            session = self._sessions.get(key)
            self._sessions[key] = None
        if session is not None:
            await session.close()

    async def _idle_session_cleanup(self) -> None:
        interval = min(max(self._idle_connection_close_seconds / 2.0, 1.0), 60.0)
        while True:
            await asyncio.sleep(interval)
            await self.close_idle_connections()

    def _unique_queues(self) -> list[asyncio.Queue[DeviceRequest]]:
        queues = []
        seen = set()
        for queue in self._queues.values():
            if id(queue) in seen:
                continue
            seen.add(id(queue))
            queues.append(queue)
        return queues
