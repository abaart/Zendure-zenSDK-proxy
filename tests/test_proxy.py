from __future__ import annotations

import json

import httpx
import respx
from fastapi.testclient import TestClient

from tests.conftest import sample_payload


@respx.mock
def test_get_report_combines_four_devices(client: TestClient) -> None:
    for index in range(1, 5):
        respx.get(f"http://dev{index}.local/properties/report").mock(
            return_value=httpx.Response(
                200, json=sample_payload(f"sn{index}", soc=40 + index)
            )
        )

    response = client.get("/properties/report")

    assert response.status_code == 200
    body = response.json()
    assert body["sn"] == "4x Zendure via PROXY"
    assert body["sn_4"] == "sn4"
    assert body["properties"]["electricLevel_4"] == 44
    assert len(body["packData"]) == 4


@respx.mock
def test_get_report_uses_cache_on_timeout(client: TestClient) -> None:
    for index in range(1, 5):
        respx.get(f"http://dev{index}.local/properties/report").mock(
            return_value=httpx.Response(
                200, json=sample_payload(f"sn{index}", soc=40 + index)
            )
        )
    assert client.get("/properties/report").status_code == 200

    respx.reset()
    for index in [1, 3, 4]:
        respx.get(f"http://dev{index}.local/properties/report").mock(
            return_value=httpx.Response(
                200, json=sample_payload(f"sn{index}", soc=50 + index)
            )
        )
    respx.get("http://dev2.local/properties/report").mock(
        side_effect=httpx.TimeoutException("boom")
    )

    response = client.get("/properties/report")

    assert response.status_code == 200
    body = response.json()
    assert body["properties"]["electricLevel_2"] == 42
    assert body["properties"]["proxyDeviceStale_2"] == 1


@respx.mock
def test_get_report_returns_504_without_cache(client: TestClient) -> None:
    for index in [1, 3, 4]:
        respx.get(f"http://dev{index}.local/properties/report").mock(
            return_value=httpx.Response(
                200, json=sample_payload(f"sn{index}", soc=50 + index)
            )
        )
    respx.get("http://dev2.local/properties/report").mock(
        side_effect=httpx.TimeoutException("boom")
    )

    response = client.get("/properties/report")

    assert response.status_code == 504


@respx.mock
def test_post_write_splits_charge_across_four_devices(client: TestClient) -> None:
    for index in range(1, 5):
        respx.get(f"http://dev{index}.local/properties/report").mock(
            return_value=httpx.Response(
                200, json=sample_payload(f"sn{index}", soc=40 + index)
            )
        )
    client.get("/properties/report")

    routes = []
    for index in range(1, 5):
        routes.append(
            respx.post(f"http://dev{index}.local/properties/write").mock(
                return_value=httpx.Response(200, json={"success": True, "code": 0})
            )
        )

    response = client.post(
        "/properties/write",
        json={"sn": "virtual", "properties": {"acMode": 1, "inputLimit": 4000}},
    )

    assert response.status_code == 200
    sent = [
        json.loads(route.calls.last.request.content)["properties"]["inputLimit"]
        for route in routes
    ]
    assert sum(sent) == 4000
    assert len([value for value in sent if value > 0]) >= 1


@respx.mock
def test_post_write_does_not_hide_device_failure(client: TestClient) -> None:
    for index in range(1, 5):
        respx.post(f"http://dev{index}.local/properties/write").mock(
            return_value=httpx.Response(200, json={"success": True, "code": 0})
        )
    respx.post("http://dev2.local/properties/write").mock(
        return_value=httpx.Response(500, json={"error": "bad"})
    )

    response = client.post(
        "/properties/write",
        json={"sn": "virtual", "properties": {"acMode": 1, "inputLimit": 0}},
    )

    assert response.status_code == 502
