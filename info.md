# Zendure zenSDK Proxy

AppDaemon proxy for the Gielz Zendure Home Assistant automation.

The proxy exposes the same Zendure zenSDK endpoints that the Gielz automation expects:

- `/properties/report`
- `/properties/write`
- `/endpoint/properties/report`
- `/endpoint/properties/write`

For Home Assistant automations, the proxy also registers AppDaemon API endpoints:

- `GET /api/appdaemon/zendure_proxy_report`
- `POST /api/appdaemon/zendure_proxy_write`

The proxy talks to one, two, or three Zendure devices and returns one combined response to Home Assistant.

The proxy also writes its own rotating logfile and exposes a small AppDaemon UI log page:

```text
http://a0d7b954-appdaemon:5050/app/zendure_proxy_logs
```

## Installation

1. Install and start AppDaemon.
2. Open the HACS configuration options.
3. Enable `AppDaemon apps discovery & tracking`.
4. Add this repository to HACS as an AppDaemon repository.
5. Install `Zendure zenSDK Proxy` from HACS.
6. Copy the `zendure_proxy` block from `examples/apps.yaml` into your AppDaemon `apps.yaml`.
7. Change `ip_zendure_1`, `ip_zendure_2`, and `ip_zendure_3` in AppDaemon `apps.yaml`.
8. Restart AppDaemon.

HACS downloads the app code to the Home Assistant configuration directory under `appdaemon/apps/Zendure-zenSDK-proxy/`.

The AppDaemon module must be `zendure_proxy`, because the `ZendureProxy` class is defined in `appdaemon/apps/Zendure-zenSDK-proxy/zendure_proxy.py`.

Point the Gielz `Zendure 2400 AC IP-adres` setting to the internal AppDaemon add-on address, for example:

```text
a0d7b954-appdaemon:8120/endpoint
```

Custom Home Assistant automations can also call the AppDaemon API endpoints directly:

```text
http://a0d7b954-appdaemon:5050/api/appdaemon/zendure_proxy_report
http://a0d7b954-appdaemon:5050/api/appdaemon/zendure_proxy_write
```

The HTTP server is also available through a normal host name when the caller can reach the AppDaemon container port directly:

```text
homeassistant.local:8120/endpoint
```
