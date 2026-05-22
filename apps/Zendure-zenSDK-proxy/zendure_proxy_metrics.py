"""In-memory metrics for ZendureProxy with dashboard and future Prometheus hooks."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
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


METRICS_WINDOW_S = 300.0


@dataclass
class LatencySamples:
    samples: deque[tuple[float, float]] = field(default_factory=deque)

    def observe(self, latency_ms: float) -> None:
        now_ts = _now()
        self.samples.append((now_ts, float(latency_ms)))
        self._trim(now_ts - METRICS_WINDOW_S)

    def summary(self, window_s: float = 300.0) -> dict[str, float]:
        cutoff = _now() - window_s
        self._trim(cutoff)
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

    def _trim(self, cutoff: float) -> None:
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()


@dataclass
class EventWindow:
    events: deque[tuple[float, bool]] = field(default_factory=deque)

    def record(self, success: bool) -> None:
        now_ts = _now()
        self.events.append((now_ts, bool(success)))
        self._trim(now_ts - METRICS_WINDOW_S)

    def counts(self, window_s: float = 300.0) -> dict[str, float]:
        cutoff = _now() - window_s
        self._trim(cutoff)
        relevant = [(ts, success) for ts, success in self.events if ts >= cutoff]
        total = len(relevant)
        errors = sum(1 for _, success in relevant if not success)
        return {
            "total": total,
            "errors": errors,
            "error_rate": (errors / total * 100.0) if total else 0.0,
            "rate_per_min": (total / (window_s / 60.0)) if window_s else 0.0,
            "rate_per_s": (total / window_s) if window_s else 0.0,
        }

    def _trim(self, cutoff: float) -> None:
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()


@dataclass
class CounterActivity:
    events: deque[tuple[float, int]] = field(default_factory=deque)
    last_hit_ts: float = 0.0

    def record(self, amount: int = 1) -> None:
        amount = max(0, int(amount))
        if amount == 0:
            return
        now_ts = _now()
        self.events.append((now_ts, amount))
        self.last_hit_ts = now_ts
        self._trim(now_ts - METRICS_WINDOW_S)

    def snapshot(self, window_s: float = 300.0) -> dict[str, Any]:
        cutoff = _now() - window_s
        self._trim(cutoff)
        return {
            "delta": sum(amount for _, amount in self.events),
            "last_hit_ts": self.last_hit_ts,
        }

    def _trim(self, cutoff: float) -> None:
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()


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
    get_last_known_fallback_total: int = 0
    get_last_known_fallback_activity: CounterActivity = field(
        default_factory=CounterActivity
    )
    relay_switches_total: int = 0
    relay_power_active: bool | None = None


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
        self.incoming_get_rate_limited_cache_total = 0
        self.incoming_get_rate_limited_cache_activity = CounterActivity()
        self.queue_get_batches_total = 0
        self.queue_get_batches_activity = CounterActivity()
        self.queue_get_coalesced_requests_total = 0
        self.queue_get_coalesced_requests_activity = CounterActivity()
        self.queue_post_batches_total = 0
        self.queue_post_batches_activity = CounterActivity()
        self.queue_post_deduplicated_requests_total = 0
        self.queue_post_deduplicated_requests_activity = CounterActivity()
        self.queue_post_deduplicated_groups_total = 0
        self.queue_post_deduplicated_groups_activity = CounterActivity()

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

    def record_incoming_get_rate_limited_cache(self) -> None:
        self.incoming_get_rate_limited_cache_total += 1
        self.incoming_get_rate_limited_cache_activity.record()

    def record_device_get_last_known_fallback(self, device_idx: int) -> None:
        if not 0 <= device_idx < len(self.devices):
            return
        self.devices[device_idx].get_last_known_fallback_total += 1
        self.devices[device_idx].get_last_known_fallback_activity.record()

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
            self.queue_get_batches_activity.record()
        if post_group_count:
            post_group_count = max(0, int(post_group_count))
            self.queue_post_batches_total += post_group_count
            self.queue_post_batches_activity.record(post_group_count)
        coalesced_gets = max(0, int(coalesced_gets))
        deduplicated_posts = max(0, int(deduplicated_posts))
        deduplicated_groups = max(0, int(deduplicated_groups))
        self.queue_get_coalesced_requests_total += coalesced_gets
        self.queue_get_coalesced_requests_activity.record(coalesced_gets)
        self.queue_post_deduplicated_requests_total += deduplicated_posts
        self.queue_post_deduplicated_requests_activity.record(deduplicated_posts)
        self.queue_post_deduplicated_groups_total += deduplicated_groups
        self.queue_post_deduplicated_groups_activity.record(deduplicated_groups)

    def record_device_relay_measurement(self, device_idx: int, active: bool) -> None:
        if not 0 <= device_idx < len(self.devices):
            return
        device = self.devices[device_idx]
        active = bool(active)
        if device.relay_power_active is None:
            device.relay_power_active = active
            return
        if device.relay_power_active != active:
            device.relay_switches_total += 1
            device.relay_power_active = active

    def snapshot(self) -> dict[str, Any]:
        now_ts = _now()
        return {
            "generated_ts": now_ts,
            "window_s": int(METRICS_WINDOW_S),
            "uptime_s": max(0, int(now_ts - self.started_ts)),
            "incoming": {
                method: self._endpoint_snapshot(metrics)
                for method, metrics in self.incoming.items()
            },
            "queue": {
                "incoming_get_depth": self.incoming_queue_get_depth,
                "incoming_post_depth": self.incoming_queue_post_depth,
                "incoming_get_rate_limited_cache_total": (
                    self.incoming_get_rate_limited_cache_total
                ),
                "incoming_get_rate_limited_cache_activity": (
                    self.incoming_get_rate_limited_cache_activity.snapshot(
                        METRICS_WINDOW_S
                    )
                ),
                "get_batches_total": self.queue_get_batches_total,
                "get_batches_activity": self.queue_get_batches_activity.snapshot(
                    METRICS_WINDOW_S
                ),
                "get_coalesced_requests_total": self.queue_get_coalesced_requests_total,
                "get_coalesced_requests_activity": (
                    self.queue_get_coalesced_requests_activity.snapshot(
                        METRICS_WINDOW_S
                    )
                ),
                "post_batches_total": self.queue_post_batches_total,
                "post_batches_activity": self.queue_post_batches_activity.snapshot(
                    METRICS_WINDOW_S
                ),
                "post_deduplicated_requests_total": self.queue_post_deduplicated_requests_total,
                "post_deduplicated_requests_activity": (
                    self.queue_post_deduplicated_requests_activity.snapshot(
                        METRICS_WINDOW_S
                    )
                ),
                "post_deduplicated_groups_total": self.queue_post_deduplicated_groups_total,
                "post_deduplicated_groups_activity": (
                    self.queue_post_deduplicated_groups_activity.snapshot(
                        METRICS_WINDOW_S
                    )
                ),
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
                    "get_last_known_fallback_total": (
                        device.get_last_known_fallback_total
                    ),
                    "get_last_known_fallback_activity": (
                        device.get_last_known_fallback_activity.snapshot(
                            METRICS_WINDOW_S
                        )
                    ),
                    "relay_switches_total": device.relay_switches_total,
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
        sensors["sensor.zendure_proxy_incoming_get_requests_per_second_5m"] = (
            round(snap["incoming"]["GET"]["window"]["rate_per_s"], 4),
            {"unit_of_measurement": "req/s"},
        )
        sensors["sensor.zendure_proxy_incoming_get_latency_samples_5m"] = (
            snap["incoming"]["GET"]["latency"]["count"],
            {},
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
        sensors["sensor.zendure_proxy_incoming_get_rate_limited_cache_total"] = (
            snap["queue"]["incoming_get_rate_limited_cache_total"],
            _counter_attrs(),
        )
        sensors["sensor.zendure_proxy_incoming_post_p95_ms"] = (
            round(snap["incoming"]["POST"]["latency"]["p95"], 1),
            {"unit_of_measurement": "ms"},
        )
        sensors["sensor.zendure_proxy_incoming_post_requests_per_second_5m"] = (
            round(snap["incoming"]["POST"]["window"]["rate_per_s"], 4),
            {"unit_of_measurement": "req/s"},
        )
        sensors["sensor.zendure_proxy_incoming_post_latency_samples_5m"] = (
            snap["incoming"]["POST"]["latency"]["count"],
            {},
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
            sensors[f"sensor.zendure_proxy_device_{idx}_get_requests_per_second_5m"] = (
                round(device["GET"]["window"]["rate_per_s"], 4),
                {"unit_of_measurement": "req/s"},
            )
            sensors[f"sensor.zendure_proxy_device_{idx}_get_latency_samples_5m"] = (
                device["GET"]["latency"]["count"],
                {},
            )
            sensors[f"sensor.zendure_proxy_device_{idx}_get_total"] = (
                device["GET"]["total"],
                _counter_attrs(),
            )
            sensors[f"sensor.zendure_proxy_device_{idx}_post_p95_ms"] = (
                round(device["POST"]["latency"]["p95"], 1),
                {"unit_of_measurement": "ms"},
            )
            sensors[f"sensor.zendure_proxy_device_{idx}_post_requests_per_second_5m"] = (
                round(device["POST"]["window"]["rate_per_s"], 4),
                {"unit_of_measurement": "req/s"},
            )
            sensors[f"sensor.zendure_proxy_device_{idx}_post_latency_samples_5m"] = (
                device["POST"]["latency"]["count"],
                {},
            )
            sensors[f"sensor.zendure_proxy_device_{idx}_post_total"] = (
                device["POST"]["total"],
                _counter_attrs(),
            )
            sensors[f"sensor.zendure_proxy_device_{idx}_errors_total"] = (
                device["GET"]["errors"] + device["POST"]["errors"],
                _counter_attrs(),
            )
            sensors[f"sensor.zendure_proxy_device_{idx}_get_last_known_fallback_total"] = (
                device["get_last_known_fallback_total"],
                _counter_attrs(),
            )
            sensors[f"sensor.zendure_proxy_device_{idx}_relay_switches_total"] = (
                device["relay_switches_total"],
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
            if isinstance(raw, str):
                raw = raw.strip()
            if raw in (None, "", "unknown", "unavailable"):
                return None
            try:
                value = Decimal(str(raw))
            except (InvalidOperation, TypeError, ValueError):
                return None
            if not value.is_finite():
                return None
            return max(0, int(value))

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
        apply("sensor.zendure_proxy_incoming_get_rate_limited_cache_total",
              lambda value: setattr(self, "incoming_get_rate_limited_cache_total", value))
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
            apply(f"sensor.zendure_proxy_device_{idx}_relay_switches_total",
                  lambda value, metric=device: setattr(metric, "relay_switches_total", value))
            device_errors = get_int(f"sensor.zendure_proxy_device_{idx}_errors_total")
            if device_errors is not None:
                device.get.errors = device_errors
                device.post.errors = 0
                restored += 1
            apply(
                f"sensor.zendure_proxy_device_{idx}_get_last_known_fallback_total",
                lambda value, metric=device: setattr(
                    metric, "get_last_known_fallback_total", value
                ),
            )

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
                "zendure_proxy_incoming_get_rate_limited_cache_total "
                f"{snap['queue']['incoming_get_rate_limited_cache_total']}"
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
            lines.append(
                f"zendure_proxy_incoming_requests_per_second_5m{{{label}}} "
                f"{metrics['window']['rate_per_s']:.6f}"
            )
            lines.append(
                f"zendure_proxy_incoming_latency_samples_5m{{{label}}} "
                f"{metrics['latency']['count']}"
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
                    f"zendure_proxy_outgoing_requests_per_second_5m{{{label}}} "
                    f"{metrics['window']['rate_per_s']:.6f}"
                )
                lines.append(
                    f"zendure_proxy_outgoing_latency_samples_5m{{{label}}} "
                    f"{metrics['latency']['count']}"
                )
            lines.append(
                f"zendure_proxy_outgoing_queue_depth{{device=\"{device['idx']}\"}} "
                f"{device['queue_depth']}"
            )
            lines.append(
                f"zendure_proxy_device_relay_switches_total{{device=\"{device['idx']}\"}} "
                f"{device['relay_switches_total']}"
            )
            lines.append(
                f"zendure_proxy_device_get_last_known_fallback_total{{device=\"{device['idx']}\"}} "
                f"{device['get_last_known_fallback_total']}"
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
            "latency": metrics.latency.summary(METRICS_WINDOW_S),
            "window": metrics.window.counts(METRICS_WINDOW_S),
        }

    @staticmethod
    def _age(ts: float) -> int | None:
        if not ts:
            return None
        return max(0, int(_now() - ts))


def render_metrics_dashboard(title: str, snapshot: dict[str, Any], refresh_s: int) -> str:
    escaped_title = html.escape(title)
    refresh_s = max(5, int(refresh_s))
    sections = [
        _render_summary(snapshot, refresh_s),
        _render_incoming(snapshot),
        _render_cache(snapshot),
        _render_queue(snapshot),
        _render_devices(snapshot),
    ]
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_title}</title>
  <style>
    body {{ margin: 0; font-family: system-ui, sans-serif; background: #111827; color: #e5e7eb; }}
    header {{ padding: 16px 20px; border-bottom: 1px solid #374151; background: #0f172a; }}
    h1 {{ margin: 0; font-size: 18px; }}
    .meta {{ margin-top: 8px; display: flex; flex-wrap: wrap; gap: 8px 16px; color: #cbd5e1; font-size: 13px; }}
    main {{ padding: 16px 20px; display: grid; gap: 16px; }}
    section {{ border: 1px solid #374151; border-radius: 8px; background: #020617; }}
    h2 {{ margin: 0; padding: 12px 14px; font-size: 15px; background: #1f2937; }}
    .table-wrap {{ overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ padding: 9px 12px; border-top: 1px solid #1f2937; text-align: right; }}
    th:first-child, td:first-child {{ text-align: left; }}
    .group-start {{ border-left: 1px solid #334155; }}
    th {{ color: #93c5fd; font-weight: 650; }}
    .help {{ position: relative; display: inline-flex; align-items: center; justify-content: center; width: 15px; height: 15px; margin-left: 5px; border: 1px solid #64748b; border-radius: 50%; color: #cbd5e1; font-size: 10px; font-weight: 700; cursor: help; vertical-align: middle; }}
    .help:focus {{ outline: 2px solid #93c5fd; outline-offset: 2px; }}
    .help::after {{ content: attr(data-help); position: fixed; z-index: 1000; left: 50%; bottom: 18px; transform: translateX(-50%); width: min(560px, calc(100vw - 40px)); padding: 10px 12px 28px; border: 1px solid #475569; border-radius: 6px; background: #0f172a; color: #f8fafc; font-size: 13px; font-weight: 400; line-height: 1.35; text-align: left; white-space: normal; box-shadow: 0 14px 34px rgba(0, 0, 0, 0.42); opacity: 0; pointer-events: none; }}
    .help::before {{ content: "Auto-refresh paused while this help is open."; position: fixed; z-index: 1001; left: 50%; bottom: 28px; transform: translateX(-50%); width: min(536px, calc(100vw - 64px)); color: #94a3b8; font-size: 11px; font-weight: 400; line-height: 1.2; text-align: left; opacity: 0; pointer-events: none; }}
    .help:hover::after, .help:focus::after {{ opacity: 1; }}
    .help:hover::before, .help:focus::before {{ opacity: 1; }}
  </style>
</head>
<body>
  <header>
    <h1>{escaped_title}</h1>
    {_render_dashboard_meta(snapshot, refresh_s)}
  </header>
  <main>
    {''.join(sections)}
  </main>
  {_render_refresh_script(refresh_s)}
</body>
</html>"""


def _render_dashboard_meta(snapshot: dict[str, Any], refresh_s: int) -> str:
    window_label = _format_duration(snapshot.get("window_s", int(METRICS_WINDOW_S)))
    updated = time.strftime(
        "%Y-%m-%d %H:%M:%S",
        time.localtime(float(snapshot.get("generated_ts", _now()))),
    )
    return (
        '<div class="meta">'
        f"<span>Last updated: {html.escape(updated)}</span>"
        f"<span>Auto refresh: {refresh_s} s</span>"
        f"<span>Window metrics: last {html.escape(window_label)}</span>"
        "</div>"
    )


def _render_refresh_script(refresh_s: int) -> str:
    refresh_ms = max(5, int(refresh_s)) * 1000
    return f"""<script>
(function () {{
  var refreshMs = {refresh_ms};
  var refreshTimer = null;
  function scheduleRefresh() {{
    window.clearTimeout(refreshTimer);
    refreshTimer = window.setTimeout(function () {{
      window.location.reload();
    }}, refreshMs);
  }}
  function pauseRefresh() {{
    window.clearTimeout(refreshTimer);
  }}
  document.querySelectorAll(".help").forEach(function (help) {{
    help.addEventListener("mouseenter", pauseRefresh);
    help.addEventListener("focus", pauseRefresh);
    help.addEventListener("mouseleave", scheduleRefresh);
    help.addEventListener("blur", scheduleRefresh);
  }});
  scheduleRefresh();
}}());
</script>"""


def _render_summary(snapshot: dict[str, Any], refresh_s: int) -> str:
    window_label = _format_duration(snapshot.get("window_s", int(METRICS_WINDOW_S)))
    return f"""<section>
<h2>Status</h2>
<div class="table-wrap">
<table>
<tr><th>{_help_label("Metric", _HELP["metric"])}</th><th>{_help_label("Value", _HELP["value"])}</th></tr>
<tr><td>{_help_label("Uptime", _HELP["uptime"])}</td><td>{_format_duration(snapshot['uptime_s'])}</td></tr>
<tr><td>{_help_label("Auto refresh", _HELP["auto_refresh"])}</td><td>{refresh_s} s</td></tr>
<tr><td>{_help_label("Window metrics", _HELP["window_metrics"])}</td><td>last {window_label}</td></tr>
</table>
</div>
</section>"""


def _render_incoming(snapshot: dict[str, Any]) -> str:
    rows = []
    for method, metrics in snapshot["incoming"].items():
        rows.append(
            "<tr>"
            f"<td>{method}</td>"
            f"<td>{metrics['total']}</td>"
            f"<td>{metrics['errors']}</td>"
            f"<td>{metrics['timeouts']}</td>"
            f"<td class=\"group-start\">{metrics['window']['rate_per_s']:.3f}</td>"
            f"<td>{metrics['latency']['count']}</td>"
            f"<td>{metrics['window']['error_rate']:.2f}%</td>"
            f"<td>{metrics['latency']['avg']:.1f}</td>"
            f"<td>{metrics['latency']['p95']:.1f}</td>"
            f"<td>{metrics['latency']['max']:.1f}</td>"
            f"<td class=\"group-start\">{metrics['active']}</td>"
            "</tr>"
        )
    return f"""<section>
<h2>Incoming Home Assistant HTTP</h2>
<div class="table-wrap">
<table>
{_header_row(["Method", "Total", "Errors", "Timeouts", "Req/s 5m", "Samples 5m", "Error 5m", "Avg 5m ms", "P95 5m ms", "Max 5m ms", "Active"], {"Req/s 5m", "Active"})}
{''.join(rows)}
</table>
</div>
</section>"""


def _render_queue(snapshot: dict[str, Any]) -> str:
    queue = snapshot["queue"]
    return f"""<section>
<h2>Queue Cleanup</h2>
<div class="table-wrap">
<table>
{_activity_header_row()}
{_metric_activity_row("Incoming GET depth", queue['incoming_get_depth'])}
{_metric_activity_row("Incoming POST depth", queue['incoming_post_depth'])}
{_metric_activity_row("GET batches", queue['get_batches_total'], queue['get_batches_activity'])}
{_metric_activity_row("GET requests saved by coalescing", queue['get_coalesced_requests_total'], queue['get_coalesced_requests_activity'])}
{_metric_activity_row("POST batches", queue['post_batches_total'], queue['post_batches_activity'])}
{_metric_activity_row("POST older requests skipped", queue['post_deduplicated_requests_total'], queue['post_deduplicated_requests_activity'])}
{_metric_activity_row("POST key groups deduplicated", queue['post_deduplicated_groups_total'], queue['post_deduplicated_groups_activity'])}
</table>
</div>
</section>"""


def _render_cache(snapshot: dict[str, Any]) -> str:
    queue = snapshot["queue"]
    device_rows = "".join(
        _metric_activity_row(
            f"Zendure {device['idx']} GET last-known fallbacks",
            device["get_last_known_fallback_total"],
            device["get_last_known_fallback_activity"],
        )
        for device in snapshot["devices"]
    )
    return f"""<section>
<h2>Cache And Fallback</h2>
<div class="table-wrap">
<table>
{_activity_header_row()}
{_metric_activity_row("GET responses served from rate-limit cache", queue['incoming_get_rate_limited_cache_total'], queue['incoming_get_rate_limited_cache_activity'])}
{device_rows}
</table>
</div>
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
                f"<td>{metrics['timeouts']}</td>"
                f"<td class=\"group-start\">{metrics['window']['rate_per_s']:.3f}</td>"
                f"<td>{metrics['latency']['count']}</td>"
                f"<td>{metrics['window']['error_rate']:.2f}%</td>"
                f"<td>{metrics['latency']['avg']:.1f}</td>"
                f"<td>{metrics['latency']['p95']:.1f}</td>"
                f"<td>{metrics['latency']['max']:.1f}</td>"
                f"<td class=\"group-start\">{device['queue_depth']}</td>"
                f"<td>{device['active_request']}</td>"
                f"<td>{_format_age(device['last_success_age_s'])}</td>"
                f"<td>{_format_age(device['last_error_age_s'])}</td>"
                f"<td>{device['relay_switches_total']}</td>"
                "</tr>"
            )
    return f"""<section>
<h2>Outgoing Zendure Devices</h2>
<div class="table-wrap">
<table>
{_header_row(["Device", "Method", "Total", "Errors", "Timeouts", "Req/s 5m", "Samples 5m", "Error 5m", "Avg 5m ms", "P95 5m ms", "Max 5m ms", "Queue", "Active", "Last success", "Last error", "Relay switches"], {"Req/s 5m", "Queue"})}
{''.join(rows)}
</table>
</div>
</section>"""


_HELP = {
    "metric": "Name of the metric shown in this row.",
    "value": "Current value for the metric shown in this row.",
    "total_value": "Lifetime total for counters. For queue-depth rows this column shows the current queue depth instead.",
    "delta_5m": "How much this counter increased during the last 5 minutes. Queue-depth rows are gauges, so this column shows n/a for those rows.",
    "last_hit": "Local date and time when this counter last increased during the current AppDaemon runtime. Restored totals from Home Assistant do not restore this timestamp.",
    "uptime": "How long the ZendureProxy AppDaemon app has been running since the current start.",
    "auto_refresh": "How often the browser reloads this dashboard page automatically.",
    "window_metrics": "The time range used for rate, error-rate, latency, and sample-count columns. Lifetime counters such as Total and Errors do not use this window.",
    "Method": "HTTP method for the measured requests. GET reads Zendure state. POST sends write commands to Zendure devices.",
    "Device": "Zendure device number from the configured device list.",
    "Total": "Lifetime count since AppDaemon started or since the counter was restored from Home Assistant sensor state.",
    "Errors": "Lifetime count of requests that did not finish successfully. HTTP status codes 400 and higher count as errors.",
    "Timeouts": "Lifetime count of requests that finished because the proxy hit a timeout.",
    "Req/s 5m": "Average requests per second during the last 5 minutes. The proxy counts events in EventWindow and divides the 5-minute count by 300 seconds.",
    "Samples 5m": "Number of latency measurements in the last 5 minutes. If this value is 0, Avg 5m ms, P95 5m ms, and Max 5m ms are shown as 0.0.",
    "Error 5m": "Percentage of requests in the last 5 minutes that did not finish successfully.",
    "Avg 5m ms": "Average request latency in milliseconds from latency samples recorded during the last 5 minutes.",
    "P95 5m ms": "95th percentile request latency in milliseconds from latency samples recorded during the last 5 minutes. Roughly 95 percent of sampled requests were this fast or faster.",
    "Max 5m ms": "Slowest request latency in milliseconds from latency samples recorded during the last 5 minutes.",
    "Active": "Requests currently in progress for this method or device.",
    "Queue": "Outgoing request queue depth for this Zendure device. DeviceClient serializes requests so the same physical Zendure device receives at most one in-flight request.",
    "Last success": "Age of the latest successful outgoing request for this Zendure device. The value is never until the first success is recorded.",
    "Last error": "Age of the latest failed outgoing request for this Zendure device. The value is never until the first error is recorded.",
    "Relay switches": "Lifetime count of measured relay state changes for this Zendure device. The proxy compares outputPackPower and packInputPower from fresh GET responses; a transition between 0 W and more than 0 W counts as one switch.",
    "Incoming GET depth": "Number of incoming Home Assistant GET requests currently waiting in RequestQueue.",
    "Incoming POST depth": "Number of incoming Home Assistant POST requests currently waiting in RequestQueue.",
    "GET batches": "Lifetime count of worker cycles where RequestQueue.drain() processed at least one queued GET request.",
    "GET requests saved by coalescing": "Lifetime count of queued GET requests that did not trigger their own upstream Zendure GET. RequestQueue.drain() coalesces multiple waiting GET requests into one upstream GET round, then gives the same combined response to all waiting GET callers.",
    "POST batches": "Lifetime count of POST key-set groups processed by RequestQueue.drain(). One drain can process multiple groups when queued POST payloads use different property key sets.",
    "POST older requests skipped": "Lifetime count of old queued POST requests that RequestQueue.drain() did not send to Zendure devices. Example: three queued POST requests with the same properties key set produce two skipped old requests and one sent newest request.",
    "POST key groups deduplicated": "Lifetime count of properties key-set groups where RequestQueue.drain() found more than one queued POST request. Example: three queued POST requests with only inputLimit count as one deduplicated key group.",
    "GET responses served from rate-limit cache": "Lifetime count of Home Assistant GET requests answered directly from state.last_get_response because request_ts - state.last_upstream_get_ts was inside get_rate_limit_window. No upstream Zendure GET round is started for that response.",
}


def _header_row(labels: list[str], group_start: set[str] | None = None) -> str:
    group_start = group_start or set()
    cells = []
    for label in labels:
        class_attr = ' class="group-start"' if label in group_start else ""
        cells.append(f"<th{class_attr}>{_help_label(label, _HELP[label])}</th>")
    return f"<tr>{''.join(cells)}</tr>"


def _metric_row(label: str, value: Any) -> str:
    return f"<tr><td>{_help_label(label, _help_text(label))}</td><td>{value}</td></tr>"


def _activity_header_row() -> str:
    return (
        "<tr>"
        f"<th>{_help_label('Metric', _HELP['metric'])}</th>"
        f"<th>{_help_label('Total', _HELP['total_value'])}</th>"
        f"<th>{_help_label('+5m', _HELP['delta_5m'])}</th>"
        f"<th>{_help_label('Last hit', _HELP['last_hit'])}</th>"
        "</tr>"
    )


def _metric_activity_row(
    label: str, value: Any, activity: dict[str, Any] | None = None
) -> str:
    if activity is None:
        delta = "n/a"
        last_hit = "n/a"
    else:
        delta = activity.get("delta", 0)
        last_hit = _format_datetime(activity.get("last_hit_ts", 0.0))
    return (
        "<tr>"
        f"<td>{_help_label(label, _help_text(label))}</td>"
        f"<td>{value}</td>"
        f"<td>{delta}</td>"
        f"<td>{last_hit}</td>"
        "</tr>"
    )


def _help_text(label: str) -> str:
    if label in _HELP:
        return _HELP[label]
    if label.startswith("Zendure ") and label.endswith(" GET last-known fallbacks"):
        return (
            "Lifetime count of GET responses for this Zendure slot where the physical "
            "device returned no fresh response, and build_combined_response(...) used "
            "state.devices[idx].last_response for that slot instead."
        )
    return f"Current value for {label}."


def _help_label(label: str, help_text: str) -> str:
    safe_label = html.escape(label)
    safe_help = html.escape(help_text, quote=True)
    return (
        f'{safe_label}<span class="help" tabindex="0" role="img" title="{safe_help}" '
        f'aria-label="{safe_help}" data-help="{safe_help}">i</span>'
    )


def _format_age(age_s: int | None) -> str:
    if age_s is None:
        return "never"
    return f"{_format_duration(age_s)} ago"


def _format_datetime(ts: float | int | None) -> str:
    if not ts:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))


def _format_duration(seconds: float | int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        if sec == 0:
            return f"{minutes} min"
        return f"{minutes} min {sec} s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        if minutes == 0:
            return f"{hours} h"
        return f"{hours} h {minutes} min"
    days, hours = divmod(hours, 24)
    if hours == 0:
        return f"{days} d"
    return f"{days} d {hours} h"
