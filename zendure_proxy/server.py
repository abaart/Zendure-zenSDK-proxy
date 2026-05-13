from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from .aggregate import ReportInput, build_virtual_report
from .cache import DeviceResponseCache
from .client import DeviceRequestError, DeviceTimeoutError, ZendureClient
from .config import AppConfig, load_config
from .distribute import build_device_writes
from .logging import configure_logging
from .metrics import HTTP_DURATION, HTTP_REQUESTS, DEVICE_CACHE_AGE, DEVICE_CACHE_HITS, prometheus_response
from .state import ProxyState

log = structlog.get_logger(__name__)


class AppRuntime:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.state = ProxyState()
        self.cache = DeviceResponseCache()
        self.client = ZendureClient(config.zendure.timeout_seconds)

    @property
    def devices(self):
        devices = self.config.zendure.enabled_devices
        self.state.sync_devices([device.name for device in devices])
        return devices


def create_app(config: AppConfig | None = None) -> FastAPI:
    config = config or load_config()
    configure_logging(config.server.log_level)
    runtime = AppRuntime(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.runtime = runtime
        yield
        await runtime.client.close()

    app = FastAPI(title="Zendure zenSDK Proxy", version="0.1.0", lifespan=lifespan)
    app.state.runtime = runtime

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        started = time.perf_counter()
        status = "500"
        try:
            response = await call_next(request)
            status = str(response.status_code)
            return response
        finally:
            duration = time.perf_counter() - started
            path = request.url.path
            HTTP_REQUESTS.labels(request.method, path, status).inc()
            HTTP_DURATION.labels(request.method, path).observe(duration)
            log.info(
                "proxy_request",
                method=request.method,
                path=path,
                status_code=int(status),
                duration_ms=round(duration * 1000, 2),
            )

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "device_count": len(runtime.devices)}

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(prometheus_response(), media_type="text/plain; version=0.0.4")

    @app.get("/properties/report")
    async def properties_report() -> JSONResponse:
        devices = runtime.devices
        if not devices:
            raise HTTPException(status_code=500, detail="no Zendure devices configured")

        async def get_device(device):
            try:
                response = await runtime.client.request_device(
                    device, "GET", "/properties/report"
                )
                payload = response.payload
                if not isinstance(payload.get("properties"), dict):
                    raise DeviceRequestError(
                        device.name,
                        "GET",
                        "/properties/report",
                        "malformed_json",
                        f"{device.name} response has no properties object",
                    )
                runtime.cache.store(device.name, payload)
                return ReportInput(device=device, payload=payload, stale=False)
            except DeviceRequestError as exc:
                cached = runtime.cache.get_fresh(
                    device.name, runtime.config.zendure.cache_ttl_seconds
                )
                if cached is None:
                    raise exc
                payload, age = cached
                DEVICE_CACHE_HITS.labels(device.name).inc()
                DEVICE_CACHE_AGE.labels(device.name).set(age)
                log.warning(
                    "zendure_get_cache_used",
                    device=device.name,
                    error_type=exc.error_type,
                    cache_age_seconds=round(age, 2),
                )
                return ReportInput(
                    device=device,
                    payload=payload,
                    stale=True,
                    cache_age_seconds=age,
                )

        try:
            reports = await asyncio.gather(*(get_device(device) for device in devices))
        except DeviceTimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except DeviceRequestError as exc:
            status_code = 504 if exc.error_type == "timeout" else 502
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc

        payload = build_virtual_report(reports, runtime.state, runtime.config.proxy)
        return JSONResponse(payload)

    @app.post("/properties/write")
    async def properties_write(request: Request) -> JSONResponse:
        devices = runtime.devices
        if not devices:
            raise HTTPException(status_code=500, detail="no Zendure devices configured")
        incoming = await request.json()
        if not isinstance(incoming, dict) or not isinstance(incoming.get("properties"), dict):
            raise HTTPException(status_code=400, detail="body must contain properties object")

        try:
            writes = build_device_writes(
                incoming, devices, runtime.state, runtime.config.proxy
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not writes:
            return JSONResponse({"success": True, "code": 0, "local": True})

        async def post_write(write):
            return await runtime.client.request_device(
                write.device, "POST", "/properties/write", write.payload
            )

        try:
            responses = await asyncio.gather(*(post_write(write) for write in writes))
        except DeviceTimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except DeviceRequestError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        return JSONResponse(
            {
                "success": True,
                "code": 0,
                "deviceResponses": [
                    {"device": response.device.name, "status": response.status_code}
                    for response in responses
                ],
            }
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.detail},
        )

    return app


app = create_app()

