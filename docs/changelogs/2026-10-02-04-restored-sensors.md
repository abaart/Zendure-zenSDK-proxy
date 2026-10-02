---
title: "Zendure proxy v0.1.30"
summary: "Former REST sensor placeholders receive live proxy values after HA restarts."
---

**Important:** Keep AppDaemon `production_mode: true` in `appdaemon.yaml`.
After updating through HACS, restart AppDaemon manually.

## Problem statement

Live deployment found 60 restored registry placeholders for former REST sensors.
`proxy_ha_sensors_skip_existing` treated the placeholders as active sensors and
left SoC, command, operating-state and version entities `unavailable` after an HA
restart. Active Gielz REST power and aggregate SoC remained available.

## Solution

`_publish_proxy_ha_sensors()` recognizes `restored: true` and fills the placeholder
using `set_state`, preserving the original entity ID and registry entry. The
publisher stores `proxy_restored_entity: true` so AppDaemon restarts continue using
`set_state` instead of adding MQTT entities with `_2` IDs. Active REST sensors
keep publishing their own values. Sensor intervals and Recorder policy remain
unchanged.

## Verification

- Release-gate tests cover direct and awaitable AppDaemon APIs, changed SoC,
  identical-value suppression, active REST preservation and AppDaemon restarts
  with MQTT connected.
- Run the complete unittest and pytest suites, syntax and import checks before
  deployment.
