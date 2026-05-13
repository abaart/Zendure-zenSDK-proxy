from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, REGISTRY, generate_latest


HTTP_REQUESTS = Counter(
    "zendure_proxy_http_requests_total",
    "Incoming proxy HTTP requests.",
    ["method", "path", "status"],
)
HTTP_DURATION = Histogram(
    "zendure_proxy_http_request_duration_seconds",
    "Incoming proxy HTTP request duration.",
    ["method", "path"],
)
DEVICE_REQUESTS = Counter(
    "zendure_proxy_device_requests_total",
    "Zendure device HTTP requests.",
    ["device", "method", "path", "status"],
)
DEVICE_DURATION = Histogram(
    "zendure_proxy_device_request_duration_seconds",
    "Zendure device HTTP request duration.",
    ["device", "method", "path"],
)
DEVICE_TIMEOUTS = Counter(
    "zendure_proxy_device_timeouts_total",
    "Zendure device timeout count.",
    ["device", "method", "path"],
)
DEVICE_ERRORS = Counter(
    "zendure_proxy_device_errors_total",
    "Zendure device error count.",
    ["device", "method", "path", "error_type"],
)
DEVICE_CACHE_HITS = Counter(
    "zendure_proxy_device_cache_hits_total",
    "Fresh cached Zendure read responses used.",
    ["device"],
)
DEVICE_CACHE_AGE = Gauge(
    "zendure_proxy_device_cache_age_seconds",
    "Age of cached Zendure read response used.",
    ["device"],
)
LATEST_POWER = Gauge(
    "zendure_proxy_latest_power_command_watts",
    "Latest power command per device. Positive means charging; negative means discharging.",
    ["device"],
)
STATE_OF_CHARGE = Gauge(
    "zendure_proxy_state_of_charge_percent",
    "Latest state of charge per device.",
    ["device"],
)


def prometheus_response() -> bytes:
    return generate_latest(REGISTRY)

