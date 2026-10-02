---
title: "Zendure proxy v0.1.31"
summary: "Restored-sensor backend markers accept AppDaemon boolean and text attributes."
---

**Important:** Keep AppDaemon `production_mode: true` and restart AppDaemon after
updating through HACS.

## Problem statement

AppDaemon converts boolean sensor attributes to strings. v0.1.30 recognizes a
boolean `proxy_restored_entity` marker, but a live `"true"` marker could move
subsequent publications to MQTT and create duplicate entity IDs.

## Solution

`_ha_attribute_is_true()` recognizes both boolean `True` and text `"true"`.
`_publish_proxy_ha_sensors()` and `_entity_is_proxy_managed()` use the helper for
restored and owned markers. Restored sensors keep the original IDs through
`set_state` during later publications and AppDaemon restarts.

## Verification

Release-gate tests simulate AppDaemon converting backend and ownership markers
to text, publish changed SoC and restart the proxy with MQTT available. Run the
complete pytest and unittest suites before deployment.
