# AGENTS.md

## Working Style

Be pragmatic and concise, but not cryptic.

Hard rules:

- Never write slogan sentences. If you use abstract terms like canonical, path, chain, flow, refactor, simplify, clean up, improve, better, or align, immediately restate the same point with exact identifiers and an explicit action.
- Avoid vague referents. Do not use this, that, it, they, these, those, or included unless the noun is explicitly named in the same sentence.
- When saying "make X the default/canonical", specify:
  - the single intended entry point by exact name;
  - which other entry points must call into that entry point or be removed by exact name;
  - the observable behavior before and after the change.

Language:

- Write explanations in simple English that is suitable for a non-native speaker.
- Keep code identifiers, file paths, URLs, YAML keys, and technical terms unchanged.

Pull request text:

- Before writing a PR title or body, check for `CONTRIBUTING.md`, `.github/PULL_REQUEST_TEMPLATE.md`, or `.github/PULL_REQUEST_TEMPLATE/*`.
- Start every PR body with `## Problem statement`.
- Add `## Solution`.
- Add `## Implementation notes` when the PR changes control flow, data flow, configuration, external behavior, or important defaults.
- Add `## Verification` with exact commands or checks.
- Avoid vague PR body bullets. Name exact files, functions, commands, settings, or error messages.

## AppDaemon HACS Deployment Notes

This repository publishes the AppDaemon implementation through HACS as category `appdaemon`.

Current HACS install shape:

- HACS category: `AppDaemon`.
- HACS metadata file: `hacs.json`.
- HACS release asset name: `zendure-zensdk-proxy-appdaemon.zip`.
- HACS app folder after install: `/config/appdaemon/apps/Zendure-zenSDK-proxy/`.
- AppDaemon module: `zendure_proxy`.
- AppDaemon class: `ZendureProxy`.
- AppDaemon app file: `apps/Zendure-zenSDK-proxy/zendure_proxy.py`.
- Example config: `examples/apps.yaml`.

The installed AppDaemon folder must contain the Python modules directly:

```text
/config/appdaemon/apps/Zendure-zenSDK-proxy/zendure_proxy.py
/config/appdaemon/apps/Zendure-zenSDK-proxy/zendure_proxy_config.py
/config/appdaemon/apps/Zendure-zenSDK-proxy/zendure_proxy_device_client.py
/config/appdaemon/apps/Zendure-zenSDK-proxy/zendure_proxy_get_handler.py
/config/appdaemon/apps/Zendure-zenSDK-proxy/zendure_proxy_post_handler.py
/config/appdaemon/apps/Zendure-zenSDK-proxy/zendure_proxy_power.py
/config/appdaemon/apps/Zendure-zenSDK-proxy/zendure_proxy_queue.py
/config/appdaemon/apps/Zendure-zenSDK-proxy/zendure_proxy_standby.py
/config/appdaemon/apps/Zendure-zenSDK-proxy/zendure_proxy_state.py
```

The AppDaemon `apps.yaml` block must be top-level:

```yaml
zendure_proxy:
  module: zendure_proxy
  class: ZendureProxy
```

Do not nest `zendure_proxy:` below another AppDaemon app such as `dynamisch_handelen:`.

## HACS Release Procedure

HACS AppDaemon directory discovery was unreliable for this repository during testing. HACS previously tried old paths such as `apps/apps.yaml` and produced errors such as:

```text
Downloading abaart/Zendure-zenSDK-proxy with version v0.1.0 failed with (No content to download)
GitHub returned 404 for https://api.github.com/repos/abaart/Zendure-zenSDK-proxy/contents/apps/apps.yaml
```

Use a zip release for HACS downloads.

`hacs.json` must contain:

```json
{
  "name": "Zendure zenSDK Proxy",
  "filename": "zendure-zensdk-proxy-appdaemon.zip",
  "zip_release": true
}
```

Before release, run:

```bash
python3 -m compileall -q apps/Zendure-zenSDK-proxy
PYTHONPATH=apps/Zendure-zenSDK-proxy python3 -c 'import zendure_proxy; print(zendure_proxy.ZendureProxy)'
```

Create the HACS release zip with Python modules at the zip root:

```bash
mkdir -p /private/tmp/zendure-hacs-release
zip -j /private/tmp/zendure-hacs-release/zendure-zensdk-proxy-appdaemon.zip apps/Zendure-zenSDK-proxy/*.py
unzip -l /private/tmp/zendure-hacs-release/zendure-zensdk-proxy-appdaemon.zip
```

The zip must not contain `apps/`, `Zendure-zenSDK-proxy/`, `examples/`, `apps.yaml`, or `__pycache__/`.

Commit, push `main`, tag, push the tag, and create the GitHub release:

Release notes must start with a clear reminder that users must restart AppDaemon manually after updating through HACS. Put the reminder at the very top of the release notes before `## Problem statement`, for example:

```markdown
**Important:** After updating through HACS, restart the AppDaemon add-on manually. HACS updates the files, but HACS does not restart AppDaemon.
```

```bash
git add <changed-files>
git commit -m "<message>"
git push fork HEAD:main
git tag vX.Y.Z
git push fork vX.Y.Z
gh release create vX.Y.Z /private/tmp/zendure-hacs-release/zendure-zensdk-proxy-appdaemon.zip --repo abaart/Zendure-zenSDK-proxy --title vX.Y.Z --notes "<release notes>"
```

After creating the release, verify that GitHub exposes the asset:

```bash
gh api repos/abaart/Zendure-zenSDK-proxy/releases/tags/vX.Y.Z --jq '.tag_name, .assets[].name'
```

Expected output includes:

```text
vX.Y.Z
zendure-zensdk-proxy-appdaemon.zip
```

Also verify the HACS GitHub Action:

```bash
gh api repos/abaart/Zendure-zenSDK-proxy/actions/runs --jq '.workflow_runs[0:3][] | [.name, .head_sha, .status, .conclusion, .html_url] | @tsv'
```

## HACS Cache Recovery

If HACS still tries `apps/apps.yaml`, the user's HACS repository state is stale.

Recommended user-facing recovery:

1. Remove `Zendure zenSDK Proxy` from HACS.
2. Add `https://github.com/abaart/Zendure-zenSDK-proxy` again as category `AppDaemon`.
3. Install the latest release.
4. Restart AppDaemon.

Avoid reintroducing `apps/apps.yaml` as the main fix. That file caused HACS to install only `apps.yaml` in earlier layouts.

## Home Assistant and AppDaemon Networking

Home Assistant Terminal, Home Assistant Core, and AppDaemon can run in different containers. From HA Terminal, `127.0.0.1` means the Terminal container, not the AppDaemon container.

For the Home Assistant Community Add-ons AppDaemon add-on, the internal add-on host is usually:

```text
a0d7b954-appdaemon
```

For Gielz, use:

```text
a0d7b954-appdaemon:8120/endpoint
```

Do not recommend these for Gielz when AppDaemon is an add-on:

```text
127.0.0.1:8120/endpoint
192.168.x.x:8120/endpoint
```

The legacy aiohttp routes are:

```text
GET  http://a0d7b954-appdaemon:8120/properties/report
POST http://a0d7b954-appdaemon:8120/properties/write
GET  http://a0d7b954-appdaemon:8120/endpoint/properties/report
POST http://a0d7b954-appdaemon:8120/endpoint/properties/write
```

The AppDaemon API endpoints are:

```text
GET  http://a0d7b954-appdaemon:5050/api/appdaemon/zendure_proxy_report
POST http://a0d7b954-appdaemon:5050/api/appdaemon/zendure_proxy_write
```

The AppDaemon UI log dashboard is:

```text
http://a0d7b954-appdaemon:5050/app/zendure_proxy_logs
```

The AppDaemon UI metrics dashboard is:

```text
http://a0d7b954-appdaemon:5050/app/zendure_proxy_metrics
```

`register_route(...)` app routes do not appear automatically in the HADashboard dashboard list. HADashboard lists `.dash` files from the AppDaemon dashboard directory. `examples/zendure_proxy.dash` is the example dashboard file that embeds `/app/zendure_proxy_metrics` and `/app/zendure_proxy_logs` with iframe widgets.

The report endpoints call the same internal function:

- `_handle_get()` handles `/properties/report` and `/endpoint/properties/report`.
- `_api_report()` handles `/api/appdaemon/zendure_proxy_report`.
- Both call `_execute_report_request()`.
- `build_combined_response()` must preserve REST compatibility fields used by `HA_REST_proxy_sensors_NL` and `HA_REST_proxy_sensors_EN`: `properties.socLimit_1`, `properties.socLimit_2`, `properties.socLimit_3`, top-level `sn_1`, top-level `sn_2`, top-level `sn_3`, top-level `product_1`, top-level `product_2`, top-level `product_3`, `properties.dualModeDamper`, `properties.equalMode`, `properties.alwaysDualMode`, `properties.outputPackPower_1`, `properties.outputPackPower_2`, `properties.outputPackPower_3`, `properties.packInputPower_1`, `properties.packInputPower_2`, `properties.packInputPower_3`, `properties.batCalTime_1`, `properties.batCalTime_2`, and `properties.batCalTime_3`.
- `build_combined_response()` must publish `properties.activeDevice` as a bitmask where bit 0 is Zendure 1, bit 1 is Zendure 2, and bit 2 is Zendure 3. `0` means no active Zendure.

The write endpoints call the same internal function:

- `_handle_post()` handles `/properties/write` and `/endpoint/properties/write`.
- `_api_write()` handles `/api/appdaemon/zendure_proxy_write`.
- Both call `_execute_write_request(payload)`.

## Useful HA Terminal Tests

Run these commands from the Home Assistant Terminal add-on:

```bash
curl -i http://a0d7b954-appdaemon:8120/properties/report
curl -i http://a0d7b954-appdaemon:8120/endpoint/properties/report
curl -i http://a0d7b954-appdaemon:5050/api/appdaemon/zendure_proxy_report
```

POST smoke tests:

```bash
curl -i \
  -X POST \
  -H "Content-Type: application/json" \
  -d '{"ping":"pong"}' \
  http://a0d7b954-appdaemon:8120/properties/write
```

```bash
curl -i \
  -X POST \
  -H "Content-Type: application/json" \
  -d '{"ping":"pong"}' \
  http://a0d7b954-appdaemon:8120/endpoint/properties/write
```

## AppDaemon Runtime Notes

After a HACS update, tell users to restart AppDaemon. HACS updates files, but HACS does not restart the AppDaemon add-on.

Logging:

- `ZendureProxy._proxy_log(...)` writes to the standard AppDaemon log and to `ProxyFileLogger` when `log_file_enabled` is true.
- Do not add a method named `ZendureProxy._log(...)`. AppDaemon `self.log(...)` calls the internal AppDaemon `_log(logger, msg, level, *args, **kwargs)` method.
- The default rotating file is `<appdaemon-config-dir>/logs/zendure_proxy.log`.
- `ProxyFileLogger` lives in `zendure_proxy_logging.py`.
- The AppDaemon UI route is registered with `register_route(self._logs_dashboard, self._cfg.log_dashboard_route)`.
- The default route is `/app/zendure_proxy_logs`.

Metrics:

- `MetricsRegistry` lives in `zendure_proxy_metrics.py`.
- `MetricsRegistry` tracks incoming GET/POST counts, latency samples, active request counts, errors, timeouts, queue cleanup counters, and per-device outgoing GET/POST metrics.
- `MetricsRegistry.prometheus_lines()` returns Prometheus-style text lines for a future `/metrics` endpoint.
- `metrics_ha_sensors_enabled` defaults to true.
- `ZendureProxy._publish_metrics_sensors()` publishes metrics through AppDaemon `set_state(...)`.
- `ZendureProxy._publish_metrics_sensors()` passes `replace=True` to `set_state(...)`.
- `MetricsRegistry.flat_ha_sensors()` sets `state_class: total_increasing` only on counter sensors such as `sensor.zendure_proxy_incoming_get_total` and `sensor.zendure_proxy_queue_get_coalesced_total`.
- `ZendureProxy._publish_metrics_sensors()` passes state values through `ZendureProxy._ha_sensor_state(...)`, because AppDaemon can omit falsy numeric states such as `0` from the Home Assistant state payload.
- `ZendureProxy._restore_metrics_counters_from_ha()` reads existing Home Assistant counter sensor states at startup and passes them to `MetricsRegistry.restore_counters_from_sensors(...)`.
- `ZendureProxy._publish_proxy_ha_sensors(...)` publishes automatic proxy response sensors from `build_proxy_ha_sensors(...)`.
- `proxy_ha_sensors_enabled` defaults to `true`.
- `proxy_ha_sensors_skip_existing` defaults to `true`; with that setting, `ZendureProxy._publish_proxy_ha_sensors(...)` skips an entity_id when Home Assistant already has a REST sensor with the same entity_id.
- `proxy_ha_sensors_mqtt_discovery_enabled` defaults to `true`; with that setting, `ZendureProxy._publish_proxy_mqtt_sensor(...)` publishes MQTT discovery payloads with `unique_id` only when the AppDaemon MQTT plugin is available.
- MQTT discovery requires a working MQTT broker, a configured Home Assistant MQTT integration, and a configured AppDaemon MQTT plugin. MQTT discovery is not guaranteed on every Home Assistant installation.
- AppDaemon `set_state(...)` fallback sensors do not get a Home Assistant `unique_id`. Keep REST sensor YAML when entity registry `unique_id` behavior is required and MQTT is not available.
- `metrics_ha_sensors_interval` defaults to 30 seconds, so Home Assistant is not updated on every request.
- `ZendureProxy._metrics_dashboard(...)` renders `/app/zendure_proxy_metrics`.

Queue behavior:

- `RequestQueue` in `zendure_proxy_queue.py` handles incoming Home Assistant GET/POST batching.
- `RequestQueue.drain()` coalesces multiple queued GET requests into one upstream GET.
- `RequestQueue.drain()` deduplicates queued POST requests by property key-set and keeps the latest POST request per key-set.
- `ZendureProxy._processor()` logs warning lines when GET coalescing or POST deduplication happens.
- `DeviceClient` in `zendure_proxy_device_client.py` owns one outgoing `asyncio.Queue` per physical Zendure device.
- `DeviceClient._worker()` serializes outgoing requests so one Zendure IP receives at most one in-flight request.

`ZendureProxy.terminate()` must close runtime resources:

- deregister `_report_endpoint_handle` with `deregister_endpoint(...)`;
- deregister `_write_endpoint_handle` with `deregister_endpoint(...)`;
- call `_runner.cleanup()` for the aiohttp server on `server_port`;
- cancel `_processor_task`;
- await `_processor_task` under `contextlib.suppress(asyncio.CancelledError)`;
- close every `DeviceClient` with `client.close()`.

If AppDaemon stops cleanly, AppDaemon calls `terminate()`. If the container receives `SIGKILL`, Python cannot run cleanup code.
