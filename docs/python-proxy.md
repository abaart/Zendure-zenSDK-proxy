# Python Zendure Proxy

The Python proxy is a Docker based replacement for the Node-RED flow. Home
Assistant can keep using the same Zendure address field because the Python
server exposes the same main endpoints:

- `GET /properties/report`
- `POST /properties/write`

The Python implementation adds:

- JSON logs on stdout.
- Prometheus metrics at `GET /metrics`.
- Health checks at `GET /healthz`.
- A 60 second per-device cache for `GET /properties/report`.
- A device list instead of fixed variables such as `ipZendure1`, `ipZendure2`,
  and `ipZendure3`.

## Docker Compose

Create `config.yaml` from `config.example.yaml` and set the Zendure IP
addresses.

```bash
cp config.example.yaml config.yaml
docker compose up --build
```

Point Home Assistant or Gielz to:

```text
<docker-host-ip>:1880
```

## Environment Overrides

`config.yaml` is the main configuration source. These environment variables can
override the file:

- `PORT`
- `LOG_LEVEL`
- `ZENDURE_TIMEOUT_SECONDS`
- `ZENDURE_CACHE_TTL_SECONDS`
- `ZENDURE_DEVICES`

`ZENDURE_DEVICES` accepts comma separated `name=host` pairs:

```text
zendure1=192.168.1.10,zendure2=192.168.1.11,zendure3=192.168.1.12
```

## Cache Behavior

The proxy caches only valid `GET /properties/report` responses. When a Zendure
device times out, returns non-2xx, returns invalid JSON, or returns JSON without
a `properties` object, the proxy uses the last valid response for that device
when the cached response is not older than `zendure.cache_ttl_seconds`.

`POST /properties/write` never uses cached data to hide a failed write.

