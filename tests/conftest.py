from __future__ import annotations

from pathlib import Path
import sys
import types
from typing import Any

import pytest


APP_DIR = Path(__file__).resolve().parents[1] / "apps" / "Zendure-zenSDK-proxy"
sys.path.insert(0, str(APP_DIR))


def _install_fake_appdaemon() -> None:
    appdaemon = types.ModuleType("appdaemon")
    plugins = types.ModuleType("appdaemon.plugins")
    hass = types.ModuleType("appdaemon.plugins.hass")
    hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")

    class Hass:
        pass

    hassapi.Hass = Hass
    sys.modules.setdefault("appdaemon", appdaemon)
    sys.modules.setdefault("appdaemon.plugins", plugins)
    sys.modules.setdefault("appdaemon.plugins.hass", hass)
    sys.modules.setdefault("appdaemon.plugins.hass.hassapi", hassapi)


def _install_fake_aiohttp() -> None:
    aiohttp = types.ModuleType("aiohttp")
    web = types.ModuleType("aiohttp.web")

    class Request:
        pass

    class Response:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class Application:
        def __init__(self):
            self.router = types.SimpleNamespace(
                add_get=lambda *args, **kwargs: None,
                add_post=lambda *args, **kwargs: None,
            )

    class AppRunner:
        def __init__(self, app):
            self.app = app

        async def setup(self):
            return None

        async def cleanup(self):
            return None

    class TCPSite:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        async def start(self):
            return None

    class ClientTimeout:
        def __init__(self, total=None):
            self.total = total

    class ClientSession:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        async def close(self):
            return None

    def json_response(data, status=200):
        return Response(data=data, status=status)

    web.Request = Request
    web.Response = Response
    web.Application = Application
    web.AppRunner = AppRunner
    web.TCPSite = TCPSite
    web.json_response = json_response
    aiohttp.web = web
    aiohttp.ClientTimeout = ClientTimeout
    aiohttp.ClientSession = ClientSession
    sys.modules.setdefault("aiohttp", aiohttp)
    sys.modules.setdefault("aiohttp.web", web)


_install_fake_appdaemon()
_install_fake_aiohttp()


class FakeDeviceClient:
    def __init__(self, get_response: dict | None = None):
        self.get_response = get_response
        self.post_payloads: list[dict] = []
        self.get_calls = 0

    async def get(self) -> dict | None:
        self.get_calls += 1
        return self.get_response

    async def post(self, payload: dict) -> dict:
        self.post_payloads.append(payload)
        return {"ack": "pong"}


def device_response(idx: int, sn: str, **overrides: Any) -> dict:
    props = {
        "acMode": 1,
        "inputLimit": 100,
        "outputLimit": 0,
        "outputPackPower": 0,
        "packInputPower": 10 + idx,
        "gridInputPower": 20 + idx,
        "outputHomePower": 0,
        "solarInputPower": 5 * idx,
        "gridOffPower": idx,
        "gridOffMode": 2,
        "minSoc": 100,
        "socSet": 1000,
        "socLimit": 0,
        "electricLevel": 40 + idx,
        "smartMode": 1,
        "BatVolt": 5200 + (idx * 100),
        "remainOutTime": idx * 100,
        "hyperTmp": 2731 + (idx * 10),
        "chargeMaxLimit": 800,
        "inverseMaxPower": 800,
        "packNum": idx,
        "rssi": -40 - idx,
        "is_error": 0,
        "socStatus": 0,
        "gridReverse": idx - 1,
        "pass": 1,
        "batCalTime": 12,
        "pvStatus": idx - 1,
        "acStatus": idx % 2,
        "dcStatus": idx,
        "solarPower1": idx * 10 + 1,
        "solarPower2": idx * 10 + 2,
        "solarPower3": idx * 10 + 3,
        "solarPower4": idx * 10 + 4,
    }
    props.update(overrides.pop("properties", {}))
    props.update(overrides)
    return {
        "sn": sn,
        "product": f"Product {idx}",
        "packData": [{"socLevel": 50 + idx, "maxTemp": 2831 + idx}],
        "properties": props,
    }


@pytest.fixture
def fake_clients():
    return [FakeDeviceClient() for _idx in range(3)]
