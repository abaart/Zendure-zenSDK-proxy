# Zendure zenSDK Proxy

AppDaemon proxy for the Gielz Zendure Home Assistant automation.

The proxy exposes the same Zendure zenSDK endpoints that the Gielz automation expects:

- `/properties/report`
- `/properties/write`
- `/endpoint/properties/report`
- `/endpoint/properties/write`

The proxy talks to one, two, or three Zendure devices and returns one combined response to Home Assistant.

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

The AppDaemon module must be `zendure_proxy.app`, because the `ZendureProxy` class is defined in `appdaemon/apps/Zendure-zenSDK-proxy/zendure_proxy/app.py`.

Point the Gielz `Zendure 2400 AC IP-adres` setting to the AppDaemon proxy address, for example:

```text
homeassistant.local:8120/endpoint
```

Use `localhost:8120/endpoint` when AppDaemon and Home Assistant run in the same network namespace.
