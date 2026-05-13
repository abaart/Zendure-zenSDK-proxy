from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from .config import DeviceConfig
from . import metrics

log = structlog.get_logger(__name__)


class DeviceRequestError(Exception):
    def __init__(self, device: str, method: str, path: str, error_type: str, message: str):
        super().__init__(message)
        self.device = device
        self.method = method
        self.path = path
        self.error_type = error_type


class DeviceTimeoutError(DeviceRequestError):
    pass


@dataclass
class DeviceResponse:
    device: DeviceConfig
    payload: dict[str, Any]
    status_code: int
    duration_seconds: float


class ZendureClient:
    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

    async def close(self) -> None:
        await self._client.aclose()

    async def request_device(
        self,
        device: DeviceConfig,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> DeviceResponse:
        started = time.perf_counter()
        url = f"{device.base_url}{path}"
        method_upper = method.upper()
        try:
            response = await self._client.request(method_upper, url, json=payload)
            duration = time.perf_counter() - started
            metrics.DEVICE_DURATION.labels(device.name, method_upper, path).observe(duration)
            metrics.DEVICE_REQUESTS.labels(
                device.name, method_upper, path, str(response.status_code)
            ).inc()
            if response.status_code < 200 or response.status_code >= 300:
                metrics.DEVICE_ERRORS.labels(
                    device.name, method_upper, path, "http_status"
                ).inc()
                raise DeviceRequestError(
                    device.name,
                    method_upper,
                    path,
                    "http_status",
                    f"{device.name} returned HTTP {response.status_code}",
                )
            try:
                data = response.json()
            except ValueError as exc:
                metrics.DEVICE_ERRORS.labels(
                    device.name, method_upper, path, "invalid_json"
                ).inc()
                raise DeviceRequestError(
                    device.name,
                    method_upper,
                    path,
                    "invalid_json",
                    f"{device.name} returned invalid JSON",
                ) from exc
            if not isinstance(data, dict):
                metrics.DEVICE_ERRORS.labels(
                    device.name, method_upper, path, "malformed_json"
                ).inc()
                raise DeviceRequestError(
                    device.name,
                    method_upper,
                    path,
                    "malformed_json",
                    f"{device.name} returned a non-object JSON payload",
                )
            log.info(
                "zendure_request",
                device=device.name,
                method=method_upper,
                path=path,
                status_code=response.status_code,
                duration_ms=round(duration * 1000, 2),
            )
            return DeviceResponse(device, data, response.status_code, duration)
        except httpx.TimeoutException as exc:
            duration = time.perf_counter() - started
            metrics.DEVICE_DURATION.labels(device.name, method_upper, path).observe(duration)
            metrics.DEVICE_TIMEOUTS.labels(device.name, method_upper, path).inc()
            metrics.DEVICE_ERRORS.labels(device.name, method_upper, path, "timeout").inc()
            log.warning(
                "zendure_timeout",
                device=device.name,
                method=method_upper,
                path=path,
                duration_ms=round(duration * 1000, 2),
            )
            raise DeviceTimeoutError(
                device.name,
                method_upper,
                path,
                "timeout",
                f"{device.name} timed out",
            ) from exc
        except httpx.HTTPError as exc:
            duration = time.perf_counter() - started
            metrics.DEVICE_DURATION.labels(device.name, method_upper, path).observe(duration)
            metrics.DEVICE_ERRORS.labels(device.name, method_upper, path, "network").inc()
            log.warning(
                "zendure_network_error",
                device=device.name,
                method=method_upper,
                path=path,
                error=str(exc),
                duration_ms=round(duration * 1000, 2),
            )
            raise DeviceRequestError(
                device.name,
                method_upper,
                path,
                "network",
                f"{device.name} network error: {exc}",
            ) from exc

