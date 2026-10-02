from copy import deepcopy
from pathlib import Path
import sys
import unittest

import yaml
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "apps" / "Zendure-zenSDK-proxy"))

from prepare_gielz_temperature_group import (  # noqa: E402
    TEMPERATURE_IDS, prepare_package, split_temperature_group, temperature_availability,
)
from zendure_proxy_ha_sensors import build_proxy_ha_sensors  # noqa: E402
from zendure_proxy_metrics import MetricsRegistry  # noqa: E402
from zendure_proxy_publication import publication_policy  # noqa: E402


class HaSensorPolicyConfigTests(unittest.TestCase):
    def package(self, filename):
        return yaml.safe_load((ROOT / "examples" / filename).read_text())

    def test_package_filenames_are_valid_home_assistant_slugs(self):
        for filename in ["zendure_recorder_policy.yaml", "zendure_temperature_availability.yaml"]:
            self.assertTrue((ROOT / "examples" / filename).is_file())
            self.assertRegex(Path(filename).stem, r"^[a-z0-9_]+$")

    def test_recorder_excludes_exactly_approved_sources(self):
        config = self.package("zendure_recorder_policy.yaml")
        actual = config["recorder"]["exclude"]["entities"]
        expected = {
            "sensor.zendure_proxy_versie", "sensor.zendure_proxy_uptime",
            "sensor.anti_pingpong_p1_sensor", "sensor.anti_pingpong_p1_sensor_bron",
            "sensor.relay_saver_resterende_seconden",
            "sensor.zendure_proxy_queue_get_depth", "sensor.zendure_proxy_queue_post_depth",
        }
        for n in range(1, 4):
            expected.update(f"sensor.zendure_{n}_{suffix}" for suffix in ["serienummer", "ip_adres"])
            expected.add(f"sensor.zendure_proxy_device_{n}_queue_depth")
        for prefix in ["incoming", "device_1", "device_2", "device_3"]:
            for method in ["get", "post"]:
                expected.update(f"sensor.zendure_proxy_{prefix}_{method}_{suffix}"
                                for suffix in ["requests_per_second_5m", "latency_samples_5m"])
        expected.update(f"sensor.zendure_2400_ac_{suffix}" for suffix in [
            "resterende_oplaad_tijd", "resterende_ontlaad_tijd", "signaalsterkte",
            "aantal_batterijen", "batterij_serienummers", "totale_capaciteit",
            "serienummer", "productserie",
        ])
        self.assertEqual(len(actual), 40)
        self.assertEqual(set(actual), expected)
        self.assertEqual(set(config["recorder"]), {"exclude"})
        self.assertEqual(set(config["recorder"]["exclude"]), {"entities"})
        self.assertFalse(any(any(word in entity for word in [
            "energie", "vermogen", "efficientie", "rte", "temperatuur", "dsmr", "net_electricity"
        ]) for entity in actual))

    def test_p03_seven_day_purge_targets_only_22_source_states(self):
        automation = self.package("zendure_recorder_policy.yaml")["automation"][0]
        self.assertEqual(automation["triggers"], [{"trigger": "time", "at": "04:20:00"}])
        action = automation["actions"][0]
        self.assertEqual(action["action"], "recorder.purge_entities")
        self.assertEqual(action["data"]["keep_days"], 7)
        expected = {"sensor.zendure_actief_device"}
        for n in range(1, 4):
            expected.update(f"sensor.zendure_{n}_{suffix}" for suffix in [
                "modus", "relais_stand", "kalibratie_bezig", "opslagmodus",
                "deep_standby", "soc_limiet_status", "offgrid_modus",
            ])
        self.assertEqual(set(action["data"]["entity_id"]), expected)
        self.assertEqual(len(action["data"]["entity_id"]), 22)
        self.assertEqual(set(action["data"]), {"entity_id", "keep_days"})

    def test_every_generated_proxy_and_metric_sensor_has_approved_policy(self):
        sensors = build_proxy_ha_sensors({"properties": {}, "packData": []})
        self.assertEqual(len(sensors), 89)
        for entity_id in sensors:
            policy = publication_policy(entity_id)
            daily = entity_id.endswith(("_serienummer", "_ip_adres")) or entity_id == "sensor.zendure_proxy_versie"
            self.assertEqual(policy.heartbeat, 86400 if daily else 3600)
            if entity_id.endswith("_temperatuur"):
                interval = 600
            elif entity_id.endswith("_health") or entity_id == "sensor.proxy_zendure_pool_healthy" or entity_id.startswith("sensor.anti_pingpong_smart_"):
                interval = 60
            elif entity_id == "sensor.relay_saver_resterende_seconden":
                interval = 10
            else:
                interval = 0
            self.assertEqual(policy.change_interval, interval, entity_id)
        metrics = MetricsRegistry(3).flat_ha_sensors()
        self.assertEqual(len(metrics), 60)
        for entity_id in metrics:
            self.assertEqual(publication_policy(entity_id).change_interval,
                             10 if entity_id.endswith("_depth") else 60)
        for n in [4, 10]:
            self.assertEqual(publication_policy(f"sensor.zendure_{n}_serienummer").heartbeat, 86400)
            self.assertEqual(publication_policy(f"sensor.zendure_{n}_omvormer_temperatuur").change_interval, 600)

    def groups(self):
        return [
            {"resource_template": "http://{{ states('input_text.proxy') }}/properties/report",
             "scan_interval": 1, "timeout": 5, "sensor": [
                 {"name": "Power", "unique_id": "power", "value_template": "{{ value_json.properties.gridInputPower }}"},
                 *[{"name": entity, "unique_id": entity.upper(), "device_class": "temperature",
                    "value_template": "{{ value_json.properties.hyperTmp }}"} for entity in sorted(TEMPERATURE_IDS)],
             ]},
            {"resource": "http://p1/api/v1/data", "scan_interval": 1,
             "sensor": [{"name": "P1", "value_template": "{{ value_json.power }}"}]},
        ]

    def test_gielz_split_keeps_sensor_identity_templates_fast_fields_and_p1(self):
        original = self.groups()
        saved = deepcopy(original)
        output = split_temperature_group(original)
        self.assertEqual(original, saved)
        self.assertEqual(len(output), 3)
        self.assertEqual(output[0]["scan_interval"], 1)
        self.assertEqual(output[0]["sensor"], [original[0]["sensor"][0]])
        self.assertEqual(output[2], original[1])
        self.assertEqual(output[1]["scan_interval"], 600)
        self.assertEqual(output[1]["resource_template"], original[0]["resource_template"])
        for actual, expected in zip(output[1]["sensor"], original[0]["sensor"][1:]):
            self.assertEqual(actual, {**expected, "availability": temperature_availability(expected["unique_id"].lower())})

    def test_package_preparation_preserves_automation_and_other_yaml_bytes(self):
        before = "# User package\ninput_text: !include helpers.yaml\nautomation:\n  - alias: Gielz\n    id: '1776080123004'\n"
        after = "template:\n  - sensor: []\n# User suffix\n"
        source = before + yaml.safe_dump({"rest": self.groups()}, sort_keys=False) + after
        output = prepare_package(source)
        self.assertTrue(output.startswith(before))
        self.assertTrue(output.endswith(after))
        with self.assertRaisesRegex(ValueError, "1 second"):
            prepare_package(output)

    def test_preparation_refuses_missing_duplicate_or_custom_availability(self):
        for kind in ["missing", "duplicate", "availability"]:
            groups = self.groups()
            if kind == "missing":
                groups[0]["sensor"].pop()
            elif kind == "duplicate":
                groups[0]["sensor"].append(deepcopy(groups[0]["sensor"][-1]))
            else:
                groups[0]["sensor"][-1]["availability"] = "{{ custom_guard }}"
            with self.assertRaises(ValueError):
                split_temperature_group(groups)

    def test_health_refresh_uses_one_temperature_entity_and_ignores_attribute_updates(self):
        automation = self.package("zendure_temperature_availability.yaml")["automation"][0]
        self.assertEqual(automation["triggers"], [{"trigger": "state", "entity_id": [
            "sensor.proxy_zendure_pool_healthy", "sensor.zendure_1_health",
            "sensor.zendure_2_health", "sensor.zendure_3_health",
        ], "to": None}])
        self.assertEqual(automation["actions"], [{"action": "homeassistant.update_entity", "target": {
            "entity_id": "sensor.zendure_2400_ac_omvormer_temperatuur",
        }}])

    def test_temperature_availability_templates_use_sampled_device_and_battery_order(self):
        env = Environment(undefined=StrictUndefined)
        states = {"sensor.zendure_1_health": "Dead", "sensor.zendure_2_health": "Healthy",
                  "sensor.proxy_zendure_pool_healthy": "Degraded",
                  "input_text.zendure_2400_ac_batterij_volgorde": "2;1"}
        context = {"states": lambda entity: states.get(entity, "unknown"),
                   "is_state": lambda entity, value: states.get(entity) == value}
        template = env.from_string(temperature_availability("zendure_2400_ac_batterij_1_temperatuur"))
        response = {"packData": [{}, {}], "packDeviceSlots": [1, 2]}
        self.assertEqual(template.render(value_json=response, **context).strip(), "True")
        states["input_text.zendure_2400_ac_batterij_volgorde"] = "unknown"
        self.assertEqual(template.render(value_json=response, **context).strip(), "False")
        states["sensor.zendure_1_health"] = "Healthy"
        self.assertEqual(template.render(value_json=response, **context).strip(), "True")
        self.assertEqual(template.render(value_json={"packData": []}, **context).strip(), "False")
        states["sensor.proxy_zendure_pool_healthy"] = "Healthy"
        self.assertEqual(template.render(value_json={"packData": [{}]}, **context).strip(), "True")
        inverter = env.from_string(temperature_availability("zendure_2400_ac_omvormer_temperatuur"))
        states["sensor.zendure_1_health"] = "Degraded"
        self.assertEqual(inverter.render(value_json={"inverterTemperatureDeviceSlots": [2]}, **context).strip(), "True")
        self.assertEqual(inverter.render(value_json={"inverterTemperatureDeviceSlots": [1, 2]}, **context).strip(), "False")


if __name__ == "__main__":
    unittest.main()
