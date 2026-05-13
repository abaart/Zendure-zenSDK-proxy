# Python Zendure zenSDK Proxy

This README explains how to install the Python proxy, connect Home Assistant to
the proxy, and monitor the proxy.

The Python proxy replaces the Node-RED proxy at the HTTP API level. Home
Assistant or the Gielz Zendure integration still calls these endpoints:

- `GET /properties/report`
- `POST /properties/write`

The Python proxy then calls each configured Zendure device through the same
ZenSDK endpoints:

- `GET http://<zendure-ip>/properties/report`
- `POST http://<zendure-ip>/properties/write`

## What The Python Proxy Adds

- Runs as a Docker container.
- Can run as a Home Assistant add-on.
- Supports more than 3 Zendure devices by reading a device list from config.
- Uses one shared Zendure HTTP client in `zendure_proxy/client.py`.
- Caches valid `GET /properties/report` responses per Zendure device.
- Uses a cached read response for up to 60 seconds when one Zendure device has a
  timeout, bad HTTP status, invalid JSON, or malformed JSON.
- Returns `POST /properties/write` errors immediately. Cached read data is never
  used to hide failed write commands.
- Writes JSON logs to stdout.
- Exposes Prometheus metrics at `GET /metrics`.
- Exposes a health endpoint at `GET /healthz`.

## Option 1: Install With Docker Compose

Use Docker Compose when the proxy runs on a normal Docker host, a NAS, a small
Linux server, or a machine next to Home Assistant.

### 1. Create `config.yaml`

Copy the example config:

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml` and set the Zendure device IP addresses:

```yaml
zendure:
  timeout_seconds: 5
  cache_ttl_seconds: 60
  devices:
    - name: "zendure1"
      host: "192.168.1.101"
    - name: "zendure2"
      host: "192.168.1.102"
    - name: "zendure3"
      host: "192.168.1.103"
    - name: "zendure4"
      host: ""
```

Leave `host` empty for a disabled Zendure device.

### 2. Start The Proxy

Run:

```bash
docker compose up --build
```

The proxy listens on port `1880` by default.

### 3. Check The Proxy

Run these commands from the Docker host:

```bash
curl http://localhost:1880/healthz
curl http://localhost:1880/properties/report
curl http://localhost:1880/metrics
```

Expected result:

- `GET /healthz` returns JSON with `"ok": true`.
- `GET /properties/report` returns one virtual Zendure response.
- `GET /metrics` returns Prometheus text metrics.

### 4. Wire Home Assistant Or Gielz To The Proxy

In the Gielz dashboard field `Zendure 2400 AC IP-adres` or
`Zendure 2400 AC IP Address`, enter:

```text
<docker-host-ip>:1880
```

Example:

```text
192.168.1.50:1880
```

If the Home Assistant setup expects an `/endpoint` prefix, enter:

```text
<docker-host-ip>:1880/endpoint
```

The Python proxy defines the same paths behind the host and port:

- Home Assistant calls `http://<docker-host-ip>:1880/properties/report`.
- Home Assistant calls `http://<docker-host-ip>:1880/properties/write`.

## Option 2: Install As A Home Assistant Add-on

Use the Home Assistant add-on option when the proxy should run inside Home
Assistant, like the current Node-RED setup.

The add-on files are in:

```text
home-assistant-addon/
```

### 1. Put The Add-on In A Home Assistant Add-on Repository

Create or use a Home Assistant add-on repository and add this folder:

```text
home-assistant-addon/
```

The add-on folder contains:

- `home-assistant-addon/config.yaml`
- `home-assistant-addon/Dockerfile`
- `home-assistant-addon/run.sh`
- `home-assistant-addon/README.md`

### 2. Install The Add-on

In Home Assistant:

1. Open **Settings**.
2. Open **Add-ons**.
3. Open **Add-on Store**.
4. Add your local or GitHub add-on repository.
5. Install **Zendure zenSDK Proxy**.

### 3. Configure The Add-on

Set the device list in the add-on options:

```yaml
log_level: INFO
timeout_seconds: 5
cache_ttl_seconds: 60
devices:
  - name: zendure1
    host: 192.168.1.101
  - name: zendure2
    host: 192.168.1.102
  - name: zendure3
    host: 192.168.1.103
```

### 4. Start The Add-on

Start **Zendure zenSDK Proxy** from the Home Assistant add-on page.

The add-on uses host networking and exposes port `1880`.

### 5. Wire Gielz To The Add-on

If Gielz runs in the same Home Assistant instance, enter this value in the Gielz
Zendure IP field:

```text
localhost:1880
```

If the Gielz setup expects an `/endpoint` prefix, enter:

```text
localhost:1880/endpoint
```

If another Home Assistant instance or another machine calls the add-on, enter:

```text
<home-assistant-ip>:1880
```

## Configuration Reference

The normal Docker container reads `config.yaml`.

```yaml
server:
  host: "0.0.0.0"
  port: 1880
  log_level: "INFO"

zendure:
  timeout_seconds: 5
  cache_ttl_seconds: 60
  devices:
    - name: "zendure1"
      host: "192.168.1.101"
    - name: "zendure2"
      host: "192.168.1.102"

proxy:
  language: "EN"
  solar_power_info: false
  single_mode_upperlimit_percent: 100
  single_mode_lowerlimit_percent: 40
  single_mode_change_device_diff: 5
  single_mode_delayed_standby_timer: 300
  dualmode_damper_enable: false
  dualmode_damper_timer: 120
  dualmode_damper_amount: 200
  balancing_factor: 5
  always_dual_mode: false
  equal_mode: false
```

### Environment Variable Overrides

The Docker container reads these environment variables after `config.yaml`:

- `PORT`
- `LOG_LEVEL`
- `ZENDURE_TIMEOUT_SECONDS`
- `ZENDURE_CACHE_TTL_SECONDS`
- `ZENDURE_DEVICES`

Example `ZENDURE_DEVICES` value:

```text
zendure1=192.168.1.101,zendure2=192.168.1.102,zendure3=192.168.1.103
```

`ZENDURE_DEVICES` replaces the `zendure.devices` list from `config.yaml`.

## Home Assistant Sensors For More Than 3 Zendures

The checked-in `Global_(EN)_Proxy/HA_REST_proxy_sensors_EN` and
`Dutch_(NL)_Proxy/HA_REST_proxy_sensors_NL` files define per-device proxy
sensors for Zendure 1, Zendure 2, and Zendure 3.

The Python proxy publishes per-device fields for every configured Zendure:

- `properties.electricLevel_N`
- `properties.latestPowerCmd_N`
- `properties.gridInputPower_N`
- `properties.outputHomePower_N`
- `properties.proxyDeviceStale_N`
- `sn_N`

Generate Home Assistant YAML for any number of devices:

```bash
uv run python tools/generate_ha_sensors.py --devices 4 --language EN
```

For Dutch labels, run:

```bash
uv run python tools/generate_ha_sensors.py --devices 4 --language NL
```

Paste the generated YAML into the same Home Assistant REST sensor section where
the current proxy sensors are configured.

## Metrics

Open:

```text
http://<proxy-host>:1880/metrics
```

Important metrics:

- `zendure_proxy_http_requests_total`
- `zendure_proxy_http_request_duration_seconds`
- `zendure_proxy_device_requests_total`
- `zendure_proxy_device_request_duration_seconds`
- `zendure_proxy_device_timeouts_total`
- `zendure_proxy_device_errors_total`
- `zendure_proxy_device_cache_hits_total`
- `zendure_proxy_device_cache_age_seconds`
- `zendure_proxy_latest_power_command_watts`
- `zendure_proxy_state_of_charge_percent`

Prometheus can scrape this endpoint directly.

## Logs

The proxy writes JSON logs to stdout.

Docker Compose logs:

```bash
docker compose logs -f zendure-proxy
```

Home Assistant add-on logs:

1. Open **Settings**.
2. Open **Add-ons**.
3. Open **Zendure zenSDK Proxy**.
4. Open **Logs**.

Useful log events:

- `proxy_request`: one Home Assistant request handled by the proxy.
- `zendure_request`: one successful HTTP request to one Zendure device.
- `zendure_timeout`: one Zendure device did not answer before
  `zendure.timeout_seconds`.
- `zendure_get_cache_used`: the proxy used a cached `GET /properties/report`
  response for one Zendure device.

## Cache Behavior

`zendure.cache_ttl_seconds` controls how long a cached Zendure read response can
hide a read problem.

Default:

```yaml
zendure:
  cache_ttl_seconds: 60
```

Behavior:

- A valid live `GET /properties/report` response updates the cache for that
  Zendure device.
- A timeout, non-2xx response, invalid JSON response, or malformed JSON response
  uses the cached response when the cached response is not older than 60 seconds.
- The proxy returns `504` for a timeout when no fresh cached response exists.
- The proxy returns `502` for a non-timeout device read error when no fresh
  cached response exists.
- `POST /properties/write` returns write errors immediately.

The combined virtual response includes cache status fields:

- `properties.proxyDeviceStale_N`: `1` means the proxy used cached data for
  Zendure device `N`.
- `properties.proxyDeviceLastSuccessAgeSeconds_N`: cache age for Zendure device
  `N`.

## Local Development

Install dependencies and run tests:

```bash
uv sync --extra test
uv run pytest -q
```

Run the server without Docker:

```bash
CONFIG_PATH=config.yaml uv run python -m uvicorn zendure_proxy.server:app --host 0.0.0.0 --port 1880
```

## Troubleshooting

### `GET /healthz` Fails

Check that the container or add-on is running:

```bash
docker compose ps
docker compose logs zendure-proxy
```

### `GET /properties/report` Returns `504`

At least one Zendure device timed out and no fresh cached response exists for
that Zendure device.

Check:

- The Zendure IP address in `config.yaml`.
- The network route from the proxy host to the Zendure device.
- The value of `zendure.timeout_seconds`.

### `GET /properties/report` Returns `502`

At least one Zendure device returned a bad response and no fresh cached response
exists for that Zendure device.

Check the logs for `zendure_network_error`, `http_status`, `invalid_json`, or
`malformed_json`.

### Home Assistant Sensors For Device 4 Are Missing

Run the sensor generator:

```bash
uv run python tools/generate_ha_sensors.py --devices 4 --language EN
```

Paste the generated YAML into the Home Assistant REST sensor configuration and
restart Home Assistant.

