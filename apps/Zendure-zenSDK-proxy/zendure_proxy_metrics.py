"""In-memory metrics for ZendureProxy with dashboard and future Prometheus hooks."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import html
import math
import time
from typing import Any


def _now() -> float:
    return time.time()


def _pct(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(percentile / 100 * len(ordered)) - 1))
    return ordered[idx]


def _counter_attrs() -> dict[str, str]:
    return {"state_class": "total_increasing"}


@dataclass
class LatencySamples:
    samples: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=1000))

    def observe(self, latency_ms: float) -> None:
        self.samples.append((_now(), float(latency_ms)))

    def summary(self, window_s: float = 300.0) -> dict[str, float]:
        cutoff = _now() - window_s
        values = [value for ts, value in self.samples if ts >= cutoff]
        if not values:
            return {"count": 0, "avg": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}
        return {
            "count": len(values),
            "avg": sum(values) / len(values),
            "p50": _pct(values, 50),
            "p95": _pct(values, 95),
            "max": max(values),
        }


@dataclass
class EventWindow:
    events: deque[tuple[float, bool]] = field(default_factory=lambda: deque(maxlen=2000))

    def record(self, success: bool) -> None:
        self.events.append((_now(), bool(success)))

    def counts(self, window_s: float = 300.0) -> dict[str, float]:
        cutoff = _now() - window_s
        relevant = [(ts, success) for ts, success in self.events if ts >= cutoff]
        total = len(relevant)
        errors = sum(1 for _, success in relevant if not success)
        return {
            "total": total,
            "errors": errors,
            "error_rate": (errors / total * 100.0) if total else 0.0,
            "rate_per_min": (total / (window_s / 60.0)) if window_s else 0.0,
        }


@dataclass
class EndpointMetrics:
    total: int = 0
    errors: int = 0
    timeouts: int = 0
    active: int = 0
    latency: LatencySamples = field(default_factory=LatencySamples)
    window: EventWindow = field(default_factory=EventWindow)

    def start(self) -> None:
        self.active += 1

    def finish(self, latency_ms: float, success: bool, timeout: bool = False) -> None:
        self.total += 1
        if not success:
            self.errors += 1
        if timeout:
            self.timeouts += 1
        self.active = max(0, self.active - 1)
        self.latency.observe(latency_ms)
        self.window.record(success)


@dataclass
class DeviceMetrics:
    get: EndpointMetrics = field(default_factory=EndpointMetrics)
    post: EndpointMetrics = field(default_factory=EndpointMetrics)
    queue_depth: int = 0
    active_request: int = 0
    last_success_ts: float = 0.0
    last_error_ts: float = 0.0


class MetricsRegistry:
    """Small metrics registry with stable names for dashboards and Prometheus."""

    def __init__(self, device_count: int):
        self.started_ts = _now()
        self.incoming = {
            "GET": EndpointMetrics(),
            "POST": EndpointMetrics(),
        }
        self.devices = [DeviceMetrics() for _ in range(device_count)]
        self.incoming_queue_get_depth = 0
        self.incoming_queue_post_depth = 0
        self.queue_get_batches_total = 0
        self.queue_get_coalesced_requests_total = 0
        self.queue_post_batches_total = 0
        self.queue_post_deduplicated_requests_total = 0
        self.queue_post_deduplicated_groups_total = 0

    def start_incoming(self, method: str) -> None:
        self.incoming[method].start()

    def finish_incoming(
        self, method: str, latency_ms: float, status: int, timeout: bool = False
    ) -> None:
        self.incoming[method].finish(latency_ms, status < 400 and not timeout, timeout)

    def start_outgoing(self, device_idx: int, method: str) -> None:
        device = self.devices[device_idx]
        device.active_request = 1
        self._endpoint(device, method).start()

    def finish_outgoing(
        self,
        device_idx: int,
        method: str,
        latency_ms: float,
        success: bool,
        timeout: bool = False,
    ) -> None:
        device = self.devices[device_idx]
        device.active_request = 0
        self._endpoint(device, method).finish(latency_ms, success, timeout)
        if success:
            device.last_success_ts = _now()
        else:
            device.last_error_ts = _now()

    def set_outgoing_queue_depth(self, device_idx: int, depth: int) -> None:
        self.devices[device_idx].queue_depth = max(0, int(depth))

    def set_incoming_queue_depth(self, get_depth: int, post_depth: int) -> None:
        self.incoming_queue_get_depth = max(0, int(get_depth))
        self.incoming_queue_post_depth = max(0, int(post_depth))

    def record_queue_batch(
        self,
        get_count: int,
        post_group_count: int,
        coalesced_gets: int,
        deduplicated_posts: int,
        deduplicated_groups: int,
    ) -> None:
        if get_count:
            self.queue_get_batches_total += 1
        if post_group_count:
            self.queue_post_batches_total += post_group_count
        self.queue_get_coalesced_requests_total += max(0, int(coalesced_gets))
        self.queue_post_deduplicated_requests_total += max(0, int(deduplicated_posts))
        self.queue_post_deduplicated_groups_total += max(0, int(deduplicated_groups))

    def snapshot(self) -> dict[str, Any]:
        return {
            "uptime_s": max(0, int(_now() - self.started_ts)),
            "incoming": {
                method: self._endpoint_snapshot(metrics)
                for method, metrics in self.incoming.items()
            },
            "queue": {
                "incoming_get_depth": self.incoming_queue_get_depth,
                "incoming_post_depth": self.incoming_queue_post_depth,
                "get_batches_total": self.queue_get_batches_total,
                "get_coalesced_requests_total": self.queue_get_coalesced_requests_total,
                "post_batches_total": self.queue_post_batches_total,
                "post_deduplicated_requests_total": self.queue_post_deduplicated_requests_total,
                "post_deduplicated_groups_total": self.queue_post_deduplicated_groups_total,
            },
            "devices": [
                {
                    "idx": idx + 1,
                    "queue_depth": device.queue_depth,
                    "active_request": device.active_request,
                    "last_success_age_s": self._age(device.last_success_ts),
                    "last_error_age_s": self._age(device.last_error_ts),
                    "GET": self._endpoint_snapshot(device.get),
                    "POST": self._endpoint_snapshot(device.post),
                }
                for idx, device in enumerate(self.devices)
            ],
        }

    def flat_ha_sensors(self) -> dict[str, tuple[Any, dict[str, Any]]]:
        snap = self.snapshot()
        sensors: dict[str, tuple[Any, dict[str, Any]]] = {}

        sensors["sensor.zendure_proxy_uptime"] = (
            snap["uptime_s"],
            {"unit_of_measurement": "s"},
        )
        sensors["sensor.zendure_proxy_incoming_get_p95_ms"] = (
            round(snap["incoming"]["GET"]["latency"]["p95"], 1),
            {"unit_of_measurement": "ms"},
        )
        sensors["sensor.zendure_proxy_incoming_get_total"] = (
            snap["incoming"]["GET"]["total"],
            _counter_attrs(),
        )
        sensors["sensor.zendure_proxy_incoming_get_errors_total"] = (
            snap["incoming"]["GET"]["errors"],
            _counter_attrs(),
        )
        sensors["sensor.zendure_proxy_incoming_get_timeouts_total"] = (
            snap["incoming"]["GET"]["timeouts"],
            _counter_attrs(),
        )
        sensors["sensor.zendure_proxy_incoming_post_p95_ms"] = (
            round(snap["incoming"]["POST"]["latency"]["p95"], 1),
            {"unit_of_measurement": "ms"},
        )
        sensors["sensor.zendure_proxy_incoming_post_total"] = (
            snap["incoming"]["POST"]["total"],
            _counter_attrs(),
        )
        sensors["sensor.zendure_proxy_incoming_post_errors_total"] = (
            snap["incoming"]["POST"]["errors"],
            _counter_attrs(),
        )
        sensors["sensor.zendure_proxy_incoming_post_timeouts_total"] = (
            snap["incoming"]["POST"]["timeouts"],
            _counter_attrs(),
        )
        sensors["sensor.zendure_proxy_incoming_get_error_rate"] = (
            round(snap["incoming"]["GET"]["window"]["error_rate"], 2),
            {"unit_of_measurement": "%"},
        )
        sensors["sensor.zendure_proxy_incoming_post_error_rate"] = (
            round(snap["incoming"]["POST"]["window"]["error_rate"], 2),
            {"unit_of_measurement": "%"},
        )
        sensors["sensor.zendure_proxy_queue_get_depth"] = (
            snap["queue"]["incoming_get_depth"],
            {},
        )
        sensors["sensor.zendure_proxy_queue_post_depth"] = (
            snap["queue"]["incoming_post_depth"],
            {},
        )
        sensors["sensor.zendure_proxy_queue_cleanup_total"] = (
            snap["queue"]["get_coalesced_requests_total"]
            + snap["queue"]["post_deduplicated_requests_total"],
            _counter_attrs(),
        )
        sensors["sensor.zendure_proxy_queue_get_coalesced_total"] = (
            snap["queue"]["get_coalesced_requests_total"],
            _counter_attrs(),
        )
        sensors["sensor.zendure_proxy_queue_post_deduplicated_total"] = (
            snap["queue"]["post_deduplicated_requests_total"],
            _counter_attrs(),
        )

        for device in snap["devices"]:
            idx = device["idx"]
            sensors[f"sensor.zendure_proxy_device_{idx}_queue_depth"] = (
                device["queue_depth"],
                {},
            )
            sensors[f"sensor.zendure_proxy_device_{idx}_get_p95_ms"] = (
                round(device["GET"]["latency"]["p95"], 1),
                {"unit_of_measurement": "ms"},
            )
            sensors[f"sensor.zendure_proxy_device_{idx}_get_total"] = (
                device["GET"]["total"],
                _counter_attrs(),
            )
            sensors[f"sensor.zendure_proxy_device_{idx}_post_p95_ms"] = (
                round(device["POST"]["latency"]["p95"], 1),
                {"unit_of_measurement": "ms"},
            )
            sensors[f"sensor.zendure_proxy_device_{idx}_post_total"] = (
                device["POST"]["total"],
                _counter_attrs(),
            )
            sensors[f"sensor.zendure_proxy_device_{idx}_errors_total"] = (
                device["GET"]["errors"] + device["POST"]["errors"],
                _counter_attrs(),
            )
            sensors[f"sensor.zendure_proxy_device_{idx}_error_rate"] = (
                round(
                    (
                        device["GET"]["window"]["errors"]
                        + device["POST"]["window"]["errors"]
                    )
                    / max(
                        1,
                        device["GET"]["window"]["total"]
                        + device["POST"]["window"]["total"],
                    )
                    * 100.0,
                    2,
                ),
                {"unit_of_measurement": "%"},
            )

        return sensors

    def counter_sensor_entity_ids(self) -> list[str]:
        return [entity_id for entity_id, (_, attrs) in self.flat_ha_sensors().items()
                if attrs.get("state_class") == "total_increasing"]

    def restore_counters_from_sensors(self, states: dict[str, Any]) -> int:
        restored = 0

        def get_int(entity_id: str) -> int | None:
            raw = states.get(entity_id)
            if raw in (None, "", "unknown", "unavailable"):
                return None
            try:
                return max(0, int(float(raw)))
            except (TypeError, ValueError):
                return None

        def apply(entity_id: str, setter) -> None:
            nonlocal restored
            value = get_int(entity_id)
            if value is None:
                return
            setter(value)
            restored += 1

        apply("sensor.zendure_proxy_incoming_get_total",
              lambda value: setattr(self.incoming["GET"], "total", value))
        apply("sensor.zendure_proxy_incoming_get_errors_total",
              lambda value: setattr(self.incoming["GET"], "errors", value))
        apply("sensor.zendure_proxy_incoming_get_timeouts_total",
              lambda value: setattr(self.incoming["GET"], "timeouts", value))
        apply("sensor.zendure_proxy_incoming_post_total",
              lambda value: setattr(self.incoming["POST"], "total", value))
        apply("sensor.zendure_proxy_incoming_post_errors_total",
              lambda value: setattr(self.incoming["POST"], "errors", value))
        apply("sensor.zendure_proxy_incoming_post_timeouts_total",
              lambda value: setattr(self.incoming["POST"], "timeouts", value))
        apply("sensor.zendure_proxy_queue_get_coalesced_total",
              lambda value: setattr(self, "queue_get_coalesced_requests_total", value))
        apply("sensor.zendure_proxy_queue_post_deduplicated_total",
              lambda value: setattr(self, "queue_post_deduplicated_requests_total", value))

        cleanup_total = get_int("sensor.zendure_proxy_queue_cleanup_total")
        if (
            cleanup_total is not None
            and cleanup_total > self.queue_get_coalesced_requests_total
            + self.queue_post_deduplicated_requests_total
        ):
            self.queue_get_coalesced_requests_total = cleanup_total
            restored += 1

        for idx, device in enumerate(self.devices, start=1):
            apply(f"sensor.zendure_proxy_device_{idx}_get_total",
                  lambda value, metric=device.get: setattr(metric, "total", value))
            apply(f"sensor.zendure_proxy_device_{idx}_post_total",
                  lambda value, metric=device.post: setattr(metric, "total", value))
            device_errors = get_int(f"sensor.zendure_proxy_device_{idx}_errors_total")
            if device_errors is not None:
                device.get.errors = device_errors
                device.post.errors = 0
                restored += 1

        return restored

    def prometheus_lines(self) -> list[str]:
        """Return simple text metrics that can later power /metrics export."""
        snap = self.snapshot()
        lines = [
            f"zendure_proxy_uptime_seconds {snap['uptime_s']}",
            f"zendure_proxy_queue_get_depth {snap['queue']['incoming_get_depth']}",
            f"zendure_proxy_queue_post_depth {snap['queue']['incoming_post_depth']}",
            (
                "zendure_proxy_queue_get_coalesced_requests_total "
                f"{snap['queue']['get_coalesced_requests_total']}"
            ),
            (
                "zendure_proxy_queue_post_deduplicated_requests_total "
                f"{snap['queue']['post_deduplicated_requests_total']}"
            ),
        ]
        for method, metrics in snap["incoming"].items():
            label = f'method="{method.lower()}"'
            lines.append(f"zendure_proxy_incoming_requests_total{{{label}}} {metrics['total']}")
            lines.append(f"zendure_proxy_incoming_errors_total{{{label}}} {metrics['errors']}")
            lines.append(
                f"zendure_proxy_incoming_latency_p95_ms{{{label}}} "
                f"{metrics['latency']['p95']:.3f}"
            )
        for device in snap["devices"]:
            for method in ("GET", "POST"):
                label = f'device="{device["idx"]}",method="{method.lower()}"'
                metrics = device[method]
                lines.append(
                    f"zendure_proxy_outgoing_requests_total{{{label}}} {metrics['total']}"
                )
                lines.append(
                    f"zendure_proxy_outgoing_errors_total{{{label}}} {metrics['errors']}"
                )
                lines.append(
                    f"zendure_proxy_outgoing_latency_p95_ms{{{label}}} "
                    f"{metrics['latency']['p95']:.3f}"
                )
            lines.append(
                f"zendure_proxy_outgoing_queue_depth{{device=\"{device['idx']}\"}} "
                f"{device['queue_depth']}"
            )
        return lines

    @staticmethod
    def _endpoint(device: DeviceMetrics, method: str) -> EndpointMetrics:
        return device.get if method == "GET" else device.post

    @staticmethod
    def _endpoint_snapshot(metrics: EndpointMetrics) -> dict[str, Any]:
        return {
            "total": metrics.total,
            "errors": metrics.errors,
            "timeouts": metrics.timeouts,
            "active": metrics.active,
            "latency": metrics.latency.summary(),
            "window": metrics.window.counts(),
        }

    @staticmethod
    def _age(ts: float) -> int | None:
        if not ts:
            return None
        return max(0, int(_now() - ts))


def render_metrics_dashboard(title: str, snapshot: dict[str, Any], refresh_s: int) -> str:
    escaped_title = html.escape(title)
    sections = [
        _render_summary(snapshot),
        _render_incoming(snapshot),
        _render_queue(snapshot),
        _render_devices(snapshot),
    ]
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="{max(5, int(refresh_s))}">
  <title>{escaped_title}</title>
  <style>
    body {{ margin: 0; font-family: system-ui, sans-serif; background: #111827; color: #e5e7eb; }}
    header {{ padding: 16px 20px; border-bottom: 1px solid #374151; background: #0f172a; }}
    h1 {{ margin: 0; font-size: 18px; }}
    main {{ padding: 16px 20px; display: grid; gap: 16px; }}
    section {{ border: 1px solid #374151; border-radius: 8px; overflow: hidden; background: #020617; }}
    h2 {{ margin: 0; padding: 12px 14px; font-size: 15px; background: #1f2937; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ padding: 9px 12px; border-top: 1px solid #1f2937; text-align: right; }}
    th:first-child, td:first-child {{ text-align: left; }}
    th {{ color: #93c5fd; font-weight: 650; }}
  </style>
</head>
<body>
  <header><h1>{escaped_title}</h1></header>
  <main>
    {''.join(sections)}
  </main>
</body>
</html>"""


def _render_summary(snapshot: dict[str, Any]) -> str:
    return f"""<section>
<h2>Status</h2>
<table>
<tr><th>Metric</th><th>Value</th></tr>
<tr><td>Uptime</td><td>{snapshot['uptime_s']} s</td></tr>
</table>
</section>"""


def _render_incoming(snapshot: dict[str, Any]) -> str:
    rows = []
    for method, metrics in snapshot["incoming"].items():
        rows.append(
            "<tr>"
            f"<td>{method}</td>"
            f"<td>{metrics['total']}</td>"
            f"<td>{metrics['errors']}</td>"
            f"<td>{metrics['window']['error_rate']:.2f}%</td>"
            f"<td>{metrics['latency']['avg']:.1f}</td>"
            f"<td>{metrics['latency']['p95']:.1f}</td>"
            f"<td>{metrics['latency']['max']:.1f}</td>"
            f"<td>{metrics['active']}</td>"
            "</tr>"
        )
    return f"""<section>
<h2>Incoming Home Assistant HTTP</h2>
<table>
<tr><th>Method</th><th>Total</th><th>Errors</th><th>5m Error</th><th>Avg ms</th><th>P95 ms</th><th>Max ms</th><th>Active</th></tr>
{''.join(rows)}
</table>
</section>"""


def _render_queue(snapshot: dict[str, Any]) -> str:
    queue = snapshot["queue"]
    return f"""<section>
<h2>Queue Cleanup</h2>
<table>
<tr><th>Metric</th><th>Value</th></tr>
<tr><td>Incoming GET depth</td><td>{queue['incoming_get_depth']}</td></tr>
<tr><td>Incoming POST depth</td><td>{queue['incoming_post_depth']}</td></tr>
<tr><td>GET batches</td><td>{queue['get_batches_total']}</td></tr>
<tr><td>GET requests saved by coalescing</td><td>{queue['get_coalesced_requests_total']}</td></tr>
<tr><td>POST batches</td><td>{queue['post_batches_total']}</td></tr>
<tr><td>POST requests skipped by deduplication</td><td>{queue['post_deduplicated_requests_total']}</td></tr>
<tr><td>POST deduplicated groups</td><td>{queue['post_deduplicated_groups_total']}</td></tr>
</table>
</section>"""


def _render_devices(snapshot: dict[str, Any]) -> str:
    rows = []
    for device in snapshot["devices"]:
        for method in ("GET", "POST"):
            metrics = device[method]
            rows.append(
                "<tr>"
                f"<td>{device['idx']}</td>"
                f"<td>{method}</td>"
                f"<td>{metrics['total']}</td>"
                f"<td>{metrics['errors']}</td>"
                f"<td>{metrics['window']['error_rate']:.2f}%</td>"
                f"<td>{metrics['latency']['avg']:.1f}</td>"
                f"<td>{metrics['latency']['p95']:.1f}</td>"
                f"<td>{metrics['latency']['max']:.1f}</td>"
                f"<td>{device['queue_depth']}</td>"
                f"<td>{device['active_request']}</td>"
                "</tr>"
            )
    return f"""<section>
<h2>Outgoing Zendure Devices</h2>
<table>
<tr><th>Device</th><th>Method</th><th>Total</th><th>Errors</th><th>5m Error</th><th>Avg ms</th><th>P95 ms</th><th>Max ms</th><th>Queue</th><th>Active</th></tr>
{''.join(rows)}
</table>
</section>"""
