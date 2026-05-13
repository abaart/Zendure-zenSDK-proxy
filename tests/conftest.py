from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from zendure_proxy.config import AppConfig, DeviceConfig, ProxyConfig, ServerConfig, ZendureConfig
from zendure_proxy.server import create_app


def sample_payload(sn: str, soc: int = 50, input_limit: int = 0, output_limit: int = 0) -> dict[str, Any]:
    return {
        "timestamp": 1,
        "messageId": 1,
        "sn": sn,
        "version": 2,
        "product": "solarFlow2400AC",
        "properties": {
            "electricLevel": soc,
            "inputLimit": input_limit,
            "outputLimit": output_limit,
            "outputPackPower": input_limit,
            "packInputPower": output_limit,
            "gridInputPower": input_limit,
            "outputHomePower": output_limit,
            "gridOffPower": 0,
            "solarInputPower": 0,
            "minSoc": 100,
            "socSet": 1000,
            "socLimit": 0,
            "socStatus": 0,
            "smartMode": 1,
            "BatVolt": 4920,
            "remainOutTime": 100,
            "hyperTmp": 2871,
            "chargeMaxLimit": 2400,
            "inverseMaxPower": 2400,
            "packNum": 4,
            "rssi": -40,
            "is_error": 0,
            "gridReverse": 2,
            "gridOffMode": 2,
            "acMode": 1,
        },
        "packData": [{"sn": f"{sn}-pack", "socLevel": soc, "maxTemp": 2871}],
    }


@pytest.fixture
def app_config() -> AppConfig:
    return AppConfig(
        server=ServerConfig(port=1880),
        zendure=ZendureConfig(
            timeout_seconds=0.1,
            cache_ttl_seconds=60,
            devices=[
                DeviceConfig(name="zendure1", host="dev1.local"),
                DeviceConfig(name="zendure2", host="dev2.local"),
                DeviceConfig(name="zendure3", host="dev3.local"),
                DeviceConfig(name="zendure4", host="dev4.local"),
            ],
        ),
        proxy=ProxyConfig(),
    )


@pytest.fixture
def client(app_config: AppConfig) -> TestClient:
    with TestClient(create_app(app_config)) as test_client:
        yield test_client

