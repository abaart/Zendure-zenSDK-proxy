---
title: "Home Assistant package filenames"
summary: "Recorder and temperature availability packages use valid package IDs."
---

## Problem statement

Home Assistant rejected the two new packages when deployed with hyphens in
their filenames. With `!include_dir_named`, each filename becomes a package ID,
which must use lowercase letters, numbers and underscores. The configuration
check succeeded, but startup logs reported `invalid slug` and skipped Recorder
exclusions, P03 retention and the temperature health-refresh automation.

## Solution

Copy `zendure_recorder_policy.yaml` and
`zendure_temperature_availability.yaml` from `examples/` into the packages
folder using their underscore filenames. Restart Home Assistant and confirm
that both automations are loaded. Proxy Python modules remain at v0.1.29.

## Verification

- Package filename regression test in `tests/test_ha_sensor_policy_config.py`.
- `python3 -m unittest discover -s tests`: 73 tests passed.
- `python3 -m pytest -q`: 191 tests passed.
- `ha core check` succeeded for the deployed configuration.
