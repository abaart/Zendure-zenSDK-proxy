# Sensor publication and Recorder

For operators using the proxy with Gielz, the Python changes reduce repeated HA
writes while keeping power control and the complete REST response current. The
HA packages below apply the approved history and temperature settings separately.
Installing the Python modules alone does not change Recorder or Gielz YAML.

## Publication rules

`_publish_proxy_ha_sensors()` and `_publish_metrics_sensors()` use
`SensorPublications.due()` and record successful writes with
`SensorPublications.record()`. A lock serializes report and metrics publications.
Identical reports, cache replies and the 300-second device refresh use the same
rules. Startup publishes eligible sensors once. Existing REST sensors remain
owned by REST; health transitions can temporarily update the existing per-device
status entities without claiming ownership or replacing their attributes.

Intervals describe the minimum spacing between changed-value publications.
The next report or timer callback publishes the latest value once the interval
expires. The metrics decision timer runs every `min(metrics_ha_sensors_interval,
10)` seconds. The configured metrics interval remains accepted.

| Decisions | Sources | Changed values |
| --- | --- | --- |
| P01-P03, P07-P10, P12-P13, P15 | Per-device power, commands, SoC, operating states; reserve and relay settings | Immediate |
| P04 | Device and pool health | State changes immediate; diagnostic attributes at most once per 60 seconds |
| P05 | Inverter and battery temperatures | 600 seconds; unknown/unavailable and recovery immediate |
| P06 | Device serial/IP, proxy version | Immediate; unchanged heartbeat 86400 seconds |
| P11 | Smart gain, loss and net euros | 60 seconds |
| P14 | Relay-saver countdown | 10 seconds; zero immediate |
| P16, P18-P21 | Uptime, latency, errors, traffic and counters | 60 seconds |
| P17 | Queue depths | 10 seconds |
| P22-P27, P31-P37 | Gielz control values, energy, tariffs, P1/DSMR, efficiencies, strategy and history sources | Existing fast REST/template behavior retained |
| P28-P30 | REST time estimates, RSSI and metadata | Existing REST behavior retained |
| P38 | All other unchanged proxy sensors, including metrics | 3600-second heartbeat |

`_publish_sensor_heartbeats()` runs every 60 seconds, using cached responses
without an upstream GET. The timer allows a 60-second lead on hour/day deadlines
so unchanged values are republished before their deadline even without REST
clients. A blocked event loop, failed writes or unavailable broker can delay
publication. Disabled sensors have no heartbeat.

`proxy_updated_at` is the Unix time of publication. The
`proxy_last_successful_get_at` attribute is the Unix time of the last successful
device GET, recorded only when that device responds. The pool sensor also exposes
`proxy_last_successful_get_at_by_slot`. Combined sensors conservatively use the
oldest last-success timestamp across configured devices, or null when a device
has never responded. Cache replies and publication heartbeats do not advance
measurement timestamps. New successful timestamps alone do not trigger writes.

MQTT discovery retains `unique_id` and `default_entity_id`. Discovery is sent at
startup, when its payload changes, and after a Connected event or observed broker
reconnection. `force_update` is false. State and attribute messages are sent only
when the sensor is due. AppDaemon `set_state` remains the fallback.

## Gielz temperatures

Prepare the current Gielz package locally, using Python with PyYAML installed:

```bash
python3 -m pip install -r tools/requirements.txt
python3 tools/prepare_gielz_temperature_group.py \
  /path/to/current/zendure_gielz1986_nl.yaml \
  /path/to/new/zendure_gielz1986_nl.yaml
```

The output must be a new file. The command performs no deployment and preserves
every section outside `rest:` byte-for-byte. Within `rest:`, all seven temperatures
move to one 600-second group with their original names, unique IDs, value
templates and metadata. The original fast group and HomeWizard group retain
their intervals and other sensors. Missing temperature IDs, duplicates, custom
availability and previously split groups require review rather than overwriting.

Install the prepared Gielz package together with
`examples/zendure_temperature_availability.yaml` only during an approved rollout.
Keep the filename with underscores when copying into a `!include_dir_named`
packages folder: Home Assistant uses the filename as the package ID.
The package calls `homeassistant.update_entity` for one temperature entity when
pool or individual device health changes; the shared REST coordinator refreshes
all seven sensors. The report adds `packDeviceSlots`, parallel to `packData`, and
`inverterTemperatureDeviceSlots`, listing the aggregate temperature contributors.
The original REST fields and pack ordering remain unchanged. Temperature
availability checks the sampled source device's fast health entity, so a failed
device does not hide temperatures from healthy devices. Battery 7-18 sensors
also use the source mapping. Responses from an older proxy without source
mapping use pool health as a conservative fallback. For more than three devices,
add the extra health entities to the availability automation's trigger list.

Temperature history remains enabled, with lower sampling detail. Thermal advice
receives samples less often; power, SoC, operating modes, minimum/maximum SoC,
energy integration sources and the five-second Gielz control loop remain fast.
The shared proxy API never freezes temperature fields to enforce HA intervals.
The REST `availability` option is supported by HA 2026.9.1, the inspected version.
Check compatibility before deploying to an older HA installation.

## Restored sensor IDs

Home Assistant may restore old REST registry entries as `unavailable` placeholders
after their YAML definitions are removed. `_publish_proxy_ha_sensors()` publishes
proxy values into placeholders with `set_state`, retains the original entity IDs,
and marks `proxy_restored_entity: true` so AppDaemon restarts keep using
`set_state`. Active REST sensors remain responsible for their existing IDs.
Registry entries are preserved, avoiding duplicate MQTT entities with `_2` IDs.

## Recorder configuration

`examples/zendure_recorder_policy.yaml` has 40 explicit exclusions for the
approved three-device installation. The exclusions cover P06, P10, P14, P16,
P17, P19 and P28-P30. Static identifiers and selected transient diagnostics stay
available to dashboards and automations but gain no new Recorder history or
Recorder statistics. Previously recorded history remains until normal purging.
Counters, latency, error rates, power, energy, efficiency and temperatures remain
recorded. `sensor.net_electricity_power_watt` and all three DSMR phase voltages
remain recorded. No broad sensor glob or include list is introduced.

The same package schedules `recorder.purge_entities` at 04:20 with
`keep_days: 7`, targeting exactly the 22 P03 source entities. The purge removes
source history older than seven days. Derived daily relay totals, the aggregate
Gielz mode and `binary_sensor.p1_nul_import_actief` are outside the purge list.
Global retention can still remove data sooner if configured below seven days.

Merge the exclusions with existing Recorder settings, preserving global
`purge_keep_days`, `commit_interval`, database URL and repack settings. For more
than three devices, add the corresponding explicit identifier, queue and traffic
exclusions after reviewing the device inventory. Keep a backup before installing
the purge automation: enabling the package authorizes deletion at the next 04:20.
No immediate purge or database repack is part of the implementation.

## Verification and rollout

Run the complete test suite with PyYAML installed. Release-gate tests exercise
direct and awaitable AppDaemon calls, concurrent reports, failures, availability,
timestamps, MQTT discovery and publication deadlines. Configuration tests assert
all 40 exclusions and all 22 purge targets, and verify the Gielz preparation.

During an approved rollout, validate HA configuration, retain the previous
packages, restart HA for the REST grouping and Recorder configuration, and
restart AppDaemon with `production_mode: true`. Compare Recorder states/hour and
HA CPU before and after under similar battery activity. Verify power/SoC control,
energy totals, daily relay totals and temperature availability. A reduction in
proxy writes does not establish a reduction in total CPU without a live comparison.

Sources:
- [Recorder](https://www.home-assistant.io/integrations/recorder/)
- [Entity-specific purging](https://www.home-assistant.io/actions/recorder.purge_entities/)
- [MQTT sensor behavior](https://www.home-assistant.io/integrations/sensor.mqtt/)
- [HA 2026.9.1 REST schema](https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/rest/schema.py)
- [AppDaemon MQTT API](https://appdaemon.readthedocs.io/en/latest/MQTT_API_REFERENCE.html)
- [Recorder filtering issue #155498](https://github.com/home-assistant/core/issues/155498)


### Network incident monitoring

`sensor.zendure_N_wifi_rssi` publishes signal strength in dBm every 60 seconds,
including unchanged readings. The source timestamp remains the last successful
GET time, so the HA watchdog can reject cached readings older than five minutes.
Invalid and excluded-slot RSSI readings are unavailable. The combined report
also exposes `properties.rssi_N` while preserving the aggregate `properties.rssi`.

The optional `examples/zendure_network_watchdog.yaml` package keeps a rolling
hour of request-error deltas and a 24-hour time-weighted RSSI reference. Minute
sample attributes provide time coverage when source values stay constant. Keep
sample sensors recorded to restore Statistics buffers on HA startup. The package
uses stored incident timestamps and a shared 24-hour notification limit per
device; observation gaps longer than five minutes reset the incident timers.
