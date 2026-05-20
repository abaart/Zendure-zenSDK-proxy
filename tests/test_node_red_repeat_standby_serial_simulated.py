from __future__ import annotations

import asyncio

from zendure_proxy import ZendureProxy, should_repeat_last_power
from zendure_proxy_config import Config, is_placeholder_device_ip, load_config
from zendure_proxy_device_client import build_device_url
from zendure_proxy_get_handler import GatewayTimeoutError, execute_get
from zendure_proxy_power import now
from zendure_proxy_standby import _delayed_standby, manage_standby
from zendure_proxy_state import DeviceState, ProxyState

from conftest import FakeDeviceClient, device_response
from node_red_expected import STANDBY_POST_PROPERTIES


def _repeat_state(device_count: int = 2) -> ProxyState:
    state = ProxyState(
        device_count=device_count,
        devices=[
            DeviceState(ip=f"ip{idx + 1}", sn=f"SN{idx + 1}", electric_level=50)
            for idx in range(device_count)
        ],
    )
    state.latest_get_ts = 1000
    state.latest_power_message_ts = 960
    state.last_post_payload = {"properties": {"acMode": 1, "inputLimit": 500}}
    state.latest_power_cmd = 500
    return state


def test_manual_repeat_uses_node_red_guard_conditions() -> None:
    cfg = Config(device_ips=["ip1", "ip2"], manual_mode_repeat=True)
    state = _repeat_state()

    assert should_repeat_last_power(state, cfg, current_ts=1000) is True

    state.latest_power_message_ts = 990
    assert should_repeat_last_power(state, cfg, current_ts=1000) is False

    state = _repeat_state(device_count=1)
    assert should_repeat_last_power(state, Config(device_ips=["ip1"]), current_ts=1000) is False

    state = _repeat_state()
    state.latest_power_cmd = 0
    assert should_repeat_last_power(state, cfg, current_ts=1000) is False

    state = _repeat_state()
    state.latest_get_ts = 980
    assert should_repeat_last_power(state, cfg, current_ts=1000) is False

    state = _repeat_state()
    for device in state.devices:
        device.soc_limit = 1
    assert should_repeat_last_power(state, cfg, current_ts=1000) is False

    state = _repeat_state()
    state.last_post_payload = {"properties": {"acMode": 1, "inputLimit": 0}}
    assert should_repeat_last_power(state, cfg, current_ts=1000) is False

    state = _repeat_state()
    state.last_post_payload = {"properties": {"inputLimit": 500, "outputLimit": 0}}
    assert should_repeat_last_power(state, cfg, current_ts=1000) is False

    state = _repeat_state()
    state.latest_power_cmd = -500
    state.last_post_payload = {"properties": {"acMode": 2, "outputLimit": 500}}
    for device in state.devices:
        device.soc_limit = 2
    assert should_repeat_last_power(state, cfg, current_ts=1000) is False


def test_delayed_standby_posts_smartmode_and_zero_power_properties() -> None:
    state = ProxyState(
        device_count=2,
        devices=[
            DeviceState(ip="ip1", sn="SN1", smart_mode=1),
            DeviceState(ip="ip2", sn="SN2", smart_mode=1),
        ],
    )
    state.devices_active_idx = [0]
    clients = [FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(
        _delayed_standby(
            1,
            state,
            clients,
            0,
            lambda *args, **kwargs: None,
        )
    )

    assert clients[1].post_payloads == [
        {"sn": "SN2", "properties": STANDBY_POST_PROPERTIES}
    ]
    assert state.devices[1].standby_device is True


def test_delayed_standby_skips_duplicate_zero_timestamp() -> None:
    state = ProxyState(
        device_count=2,
        devices=[
            DeviceState(ip="ip1", sn="SN1", smart_mode=1),
            DeviceState(ip="ip2", sn="SN2", smart_mode=1, latest_power_cmd_zero_ts=123),
        ],
    )
    state.devices_active_idx = [0]
    clients = [FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(_delayed_standby(1, state, clients, 0, lambda *args, **kwargs: None))
    state.devices[1].standby_task = None
    asyncio.run(_delayed_standby(1, state, clients, 0, lambda *args, **kwargs: None))

    assert clients[1].post_payloads == [
        {"sn": "SN2", "properties": STANDBY_POST_PROPERTIES}
    ]


def test_delayed_standby_rechecks_recent_successful_get_before_post() -> None:
    state = ProxyState(
        device_count=2,
        devices=[
            DeviceState(ip="ip1", sn="SN1", smart_mode=1),
            DeviceState(
                ip="ip2",
                sn="SN2",
                smart_mode=1,
                latest_power_cmd_zero_ts=123,
            ),
        ],
        device_active_count=1,
        devices_active_idx=[0],
        latest_power_cmd=500,
        latest_get_ts=now() - 11,
    )
    clients = [FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(
        _delayed_standby(
            1,
            state,
            clients,
            0,
            lambda *args, **kwargs: None,
            cfg=Config(device_ips=["ip1", "ip2"]),
        )
    )

    assert clients[1].post_payloads == []


def test_manage_standby_blocks_node_red_guard_conditions() -> None:
    cfg = Config(device_ips=["ip1", "ip2"], standby_timer=300)
    state = ProxyState(
        device_count=2,
        devices=[
            DeviceState(ip="ip1", sn="SN1", electric_level=50),
            DeviceState(ip="ip2", sn="SN2", electric_level=52, latest_power_cmd_zero_ts=10),
        ],
        device_active_count=1,
        devices_active_idx=[0],
        latest_power_cmd=500,
        latest_get_ts=now(),
    )
    clients = [FakeDeviceClient(), FakeDeviceClient()]

    asyncio.run(
        manage_standby(
            state,
            clients,
            1,
            [500, 0],
            cfg,
            lambda *args, **kwargs: None,
        )
    )
    assert state.devices[1].standby_task is None

    state.devices[1].electric_level = 80
    state.transition_start_ts = now()
    asyncio.run(
        manage_standby(
            state,
            clients,
            1,
            [500, 0],
            cfg,
            lambda *args, **kwargs: None,
        )
    )
    assert state.devices[1].standby_task is None

    state.transition_start_ts = 0
    state.latest_get_ts = now() - 11
    asyncio.run(
        manage_standby(
            state,
            clients,
            1,
            [500, 0],
            cfg,
            lambda *args, **kwargs: None,
        )
    )
    assert state.devices[1].standby_task is None

    state.latest_get_ts = now()
    state.latest_power_cmd = 0
    asyncio.run(
        manage_standby(
            state,
            clients,
            1,
            [0, 0],
            cfg,
            lambda *args, **kwargs: None,
        )
    )
    assert state.devices[1].standby_task is None


def test_serial_retry_populates_missing_serial_numbers() -> None:
    proxy = ZendureProxy.__new__(ZendureProxy)
    proxy._clients = [
        FakeDeviceClient(device_response(1, "SN1")),
        FakeDeviceClient(device_response(2, "SN2")),
    ]
    proxy._state = ProxyState(
        device_count=2,
        devices=[DeviceState(ip="ip1"), DeviceState(ip="ip2")],
    )
    proxy._proxy_log = lambda *args, **kwargs: None

    asyncio.run(proxy._ensure_serial_numbers())

    assert [device.sn for device in proxy._state.devices] == ["SN1", "SN2"]


def test_report_request_retries_missing_serials_before_returning_503() -> None:
    proxy = ZendureProxy.__new__(ZendureProxy)
    client = FakeDeviceClient(None)
    proxy._cfg = Config(device_ips=["ip1"])
    proxy._clients = [client]
    proxy._state = ProxyState(device_count=1, devices=[DeviceState(ip="ip1")])
    proxy._metrics = _FakeMetrics()
    proxy._proxy_log = lambda *args, **kwargs: None
    proxy._publish_proxy_ha_sensors = _noop_publish

    data, status = asyncio.run(proxy._execute_report_request())

    assert status == 503
    assert data == {"error": "Initializing - try again shortly"}
    assert client.get_calls == 1


def test_execute_get_raises_gateway_timeout_when_strict_compat_is_enabled() -> None:
    state = ProxyState(
        device_count=2,
        devices=[
            DeviceState(ip="ip1", sn="SN1"),
            DeviceState(ip="ip2", sn="SN2"),
        ],
        last_get_response={"cached": True},
    )
    clients = [FakeDeviceClient(device_response(1, "SN1")), FakeDeviceClient(None)]

    try:
        asyncio.run(
            execute_get(
                clients,
                state,
                Config(
                    device_ips=["ip1", "ip2"],
                    node_red_compat_strict_get_errors=True,
                ),
                lambda *args, **kwargs: None,
            )
        )
    except GatewayTimeoutError as exc:
        assert str(exc) == "Gateway Timeout"
    else:
        raise AssertionError("GatewayTimeoutError was not raised")


def test_successful_get_updates_latest_get_timestamp_only_after_device_replies() -> None:
    state = ProxyState(
        device_count=1,
        devices=[DeviceState(ip="ip1", sn="SN1")],
    )
    state.latest_get_ts = 0
    clients = [FakeDeviceClient(device_response(1, "SN1"))]

    asyncio.run(
        execute_get(
            clients,
            state,
            Config(device_ips=["ip1"]),
            lambda *args, **kwargs: None,
        )
    )

    assert state.latest_get_ts > 0

    previous_ts = state.latest_get_ts
    clients = [FakeDeviceClient(None)]
    try:
        asyncio.run(
            execute_get(
                clients,
                state,
                Config(device_ips=["ip1"], node_red_compat_strict_get_errors=True),
                lambda *args, **kwargs: None,
            )
        )
    except GatewayTimeoutError:
        pass
    else:
        raise AssertionError("GatewayTimeoutError was not raised")

    assert state.latest_get_ts == previous_ts


def test_report_request_returns_gateway_timeout_for_strict_get_error() -> None:
    proxy = ZendureProxy.__new__(ZendureProxy)
    proxy._cfg = Config(
        device_ips=["ip1"],
        node_red_compat_strict_get_errors=True,
    )
    proxy._clients = [FakeDeviceClient(device_response(1, "SN1"))]
    proxy._state = ProxyState(
        device_count=1,
        devices=[DeviceState(ip="ip1", sn="SN1")],
    )
    proxy._metrics = _FakeMetrics()
    proxy._queue = _GatewayTimeoutQueue()
    proxy._proxy_log = lambda *args, **kwargs: None
    proxy._publish_proxy_ha_sensors = _noop_publish
    proxy._record_incoming_depths = _noop_record_depths
    proxy._debug_capture_payload = lambda *args, **kwargs: None

    data, status = asyncio.run(proxy._execute_report_request())

    assert status == 504
    assert data == {"error": "Gateway Timeout"}


def test_simulated_device_urls_and_placeholder_ips_are_compatible_with_node_red() -> None:
    assert build_device_url(
        "testdevice1",
        "properties/report",
        "proxy.local:8120/endpoint/properties/report",
    ) == "http://proxy.local:8120/endpoint/testdevice1/properties/report"
    assert build_device_url(
        "testdevice2",
        "properties/write",
        "proxy.local:8120/properties/write",
    ) == "http://proxy.local:8120/testdevice2/properties/write"

    assert is_placeholder_device_ip("192.168.x.x") is True
    assert is_placeholder_device_ip("192.168.x.y") is True
    assert load_config(
        {"ip_zendure_1": "192.168.x.x", "ip_zendure_2": "192.168.x.y"}
    ).device_ips == []


async def _noop_publish(_response: dict) -> None:
    return None


async def _noop_record_depths() -> None:
    return None


class _GatewayTimeoutQueue:
    async def enqueue_get(self):
        future = asyncio.get_event_loop().create_future()
        future.set_exception(GatewayTimeoutError("Gateway Timeout"))
        return future


class _FakeMetrics:
    def start_incoming(self, _method: str) -> None:
        return None

    def finish_incoming(
        self,
        _method: str,
        _duration_ms: float,
        _status: int,
        _timeout: bool,
    ) -> None:
        return None
