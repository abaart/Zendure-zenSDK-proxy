---
title: "Zendure proxy v0.1.28"
summary: "Changed power and SoC remain fast; unchanged sensors generate fewer Home Assistant writes."
---

**Important:** Keep AppDaemon `production_mode: true` in `appdaemon.yaml`.
After updating through HACS, restart the AppDaemon add-on manually. HACS updates
the files but does not restart AppDaemon.

## Problem statement

For operators using Gielz and multiple Zendures, repeated proxy reports changed
sensor timestamps even when sensor values stayed the same. The repeated writes
added Home Assistant history rows. Some existing sensors also stopped updating
after restart because Home Assistant returned the ownership marker as text.

## Solution

Power, power commands, SoC and operating-state changes remain immediately
available. Temperature changes publish every ten minutes, with immediate
unavailability and recovery. Unchanged values are republished at least hourly;
serial numbers, IP addresses and the proxy version use a daily heartbeat.
Publication time and the last successful device measurement are shown separately.

## Implementation notes

`SensorPublications.due()` applies the intervals to both reports and metrics.
`_entity_is_proxy_managed()` recognizes boolean and text markers. MQTT discovery
is repeated only for configuration changes and reconnection, with `force_update`
disabled. The Gielz compatibility endpoint from `origin/main` is retained.

Home Assistant configuration is a separate rollout: the preparation tool splits
the seven Gielz temperatures into a 600-second REST group without changing the
fast control sensors. The example Recorder package excludes 40 approved
diagnostics and metadata entities and schedules a seven-day purge for 22 P03
source states at 04:20. Derived daily totals and global retention remain unchanged.
HACS installs Python modules; HACS does not install the example HA packages.

## Verification

- `python3 -m compileall -q apps/Zendure-zenSDK-proxy tools`
- `PYTHONPATH=apps/Zendure-zenSDK-proxy python3 -c 'import zendure_proxy; print(zendure_proxy.ZendureProxy)'`
- `python3 -m unittest discover -s tests`
- `python3 -m pytest -q`
- `git diff --check`
- Configuration tests check all Recorder targets and render temperature availability templates.
