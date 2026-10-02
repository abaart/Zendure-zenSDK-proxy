---
title: "Zendure proxy v0.1.29"
summary: "MQTT discovery starts correctly when AppDaemon returns an awaitable plugin API."
---

**Important:** Keep AppDaemon `production_mode: true` in `appdaemon.yaml`.
After updating through HACS, restart the AppDaemon add-on manually. HACS updates
the files but does not restart AppDaemon.

## Problem statement

Live verification of v0.1.28 found that AppDaemon returns its MQTT plugin API
as an `asyncio.Task`. The proxy logged `'_asyncio.Task' object has no attribute
'listen_event'` and used the `set_state` fallback instead of MQTT discovery.
Power control and REST responses remained available.

## Solution

`initialize()` resolves `_get_mqtt_api()` before using the plugin.
`_get_mqtt_api()` resolves `get_plugin_api("MQTT")` and catches failures after
awaiting the plugin result. MQTT discovery and reconnect callbacks can start.
The sensor publication and Recorder settings from v0.1.28 remain present.

## Verification

- `python3 -m unittest discover -s tests`
- `python3 -m pytest -q`
- `python3 -m compileall -q apps/Zendure-zenSDK-proxy tools`
- `PYTHONPATH=apps/Zendure-zenSDK-proxy python3 -c 'import zendure_proxy; print(zendure_proxy.ZendureProxy)'`
- Release-gate tests initialize MQTT with direct and Task plugin results and verify disabled or failed plugin loading.
