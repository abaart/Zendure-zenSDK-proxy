"""Exercise the HA package's actual templates with time and source failures."""
import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest

from jinja2 import Environment, StrictUndefined
import yaml

PACKAGE = Path(__file__).resolve().parents[1] / 'examples/zendure_network_watchdog.yaml'


class WatchdogTemplatesTests(unittest.TestCase):
    def setUp(self):
        self.package = yaml.safe_load(PACKAGE.read_text())
        self.ts = 1000000
        self.previous = {}
        self.values = {
            'sensor.zendure_1_errors_last_hour': '200',
            'sensor.zendure_1_health': 'Degraded',
            'sensor.zendure_1_wifi_rssi': '-60',
            'sensor.zendure_1_rssi_average_24h': '-60',
        }
        self.attributes = {
            'sensor.zendure_1_errors_last_hour': {'age_coverage_ratio': 0.98, 'buffer_usage_ratio': 0.98},
            'sensor.zendure_1_rssi_average_24h': {'age_coverage_ratio': 0.8, 'buffer_usage_ratio': 0.8},
        }
        self.running = True
        self.fresh = True
        self.env = Environment(undefined=StrictUndefined)
        self.templates = {}
        self.env.globals.update(is_number=self.is_number, as_timestamp=self.timestamp,
                                now=lambda: datetime.fromtimestamp(self.ts, timezone.utc),
                                state_attr=self.state_attr)

    @staticmethod
    def is_number(value):
        try:
            return float('-inf') < float(value) < float('inf')
        except (ValueError, TypeError):
            return False

    @staticmethod
    def timestamp(value, default=None):
        if isinstance(value, datetime):
            return value.timestamp()
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def state_attr(self, entity, attr):
        if entity == 'sensor.zendure_1_network_monitor':
            return self.previous if attr == 'monitor' else None
        if entity == 'sensor.zendure_1_wifi_rssi' and attr == 'proxy_last_successful_get_at':
            return self.ts - (30 if self.fresh else 600)
        return self.attributes.get(entity, {}).get(attr)

    def render(self, template, **variables):
        outer = self
        class States:
            sensor = SimpleNamespace(zendure_proxy_uptime=SimpleNamespace(
                state='100', last_updated=outer.ts - (30 if outer.running else 600)))
            def __call__(self, entity):
                return outer.values.get(entity, 'unknown')
        if template not in self.templates:
            self.templates[template] = self.env.from_string(template)
        return self.templates[template].render(states=States(), **variables).strip()

    def tick(self, seconds=60):
        self.ts += seconds
        template = self.package['template'][1]['variables']['monitor']
        self.previous = ast.literal_eval(self.render(template, n=1, limits=self.package['template'][1]['variables']['limits']))
        return self.previous

    def test_connection_requires_24_hours_and_resets_after_hour_of_recovery(self):
        m = self.tick()
        start = m['connection_since']
        for _ in range(1439):
            self.assertFalse(self.tick()['connection_alert'])
        self.assertTrue(self.tick()['connection_alert'])
        self.assertEqual(self.previous['connection_since'], start)
        self.values['sensor.zendure_1_errors_last_hour'] = '0'
        self.assertFalse(self.tick()['connection_alert'])
        for _ in range(59):
            self.tick()
        self.assertEqual(self.previous['connection_since'], start)
        self.assertEqual(self.tick()['connection_since'], 0)

    def test_short_restart_preserves_timer_and_long_gap_restarts_observation(self):
        start = self.tick()['connection_since']
        self.assertEqual(self.tick(120)['connection_since'], start)
        self.assertNotEqual(self.tick(600)['connection_since'], start)

    def test_missing_or_stale_proxy_cannot_finish_connection_timer(self):
        self.tick()
        self.running = False
        self.assertEqual(self.tick()['connection_since'], 0)
        self.running = True
        self.values['sensor.zendure_1_errors_last_hour'] = 'unavailable'
        self.assertEqual(self.tick()['connection_since'], 0)

    def test_wifi_reference_freezes_and_requires_ten_minutes(self):
        self.values['sensor.zendure_1_wifi_rssi'] = '-75'
        self.assertFalse(self.tick()['wifi_alert'])
        for _ in range(9):
            self.values['sensor.zendure_1_rssi_average_24h'] = '-73'
            self.assertFalse(self.tick()['wifi_alert'])
        m = self.tick()
        self.assertTrue(m['wifi_alert'])
        self.assertEqual(m['wifi_reference'], -60)
        self.values['sensor.zendure_1_wifi_rssi'] = '-62'
        self.assertFalse(self.tick()['wifi_alert'])
        self.assertIsNone(self.previous['wifi_reference'])

    def test_wifi_accepts_stronger_signal_but_ignores_warmup_and_stale_data(self):
        self.values['sensor.zendure_1_wifi_rssi'] = '-45'
        self.attributes['sensor.zendure_1_rssi_average_24h']['age_coverage_ratio'] = 0.1
        self.assertEqual(self.tick()['wifi_since'], 0)
        self.attributes['sensor.zendure_1_rssi_average_24h']['age_coverage_ratio'] = 0.8
        self.assertGreater(self.tick()['wifi_since'], 0)
        self.fresh = False
        self.assertEqual(self.tick()['wifi_since'], 0)
        self.assertFalse(self.previous['wifi_alert'])

    def test_sparse_history_cannot_supply_reference(self):
        self.values['sensor.zendure_1_wifi_rssi'] = '-80'
        self.attributes['sensor.zendure_1_rssi_average_24h']['buffer_usage_ratio'] = 0.01
        self.assertEqual(self.tick()['wifi_since'], 0)
        self.attributes['sensor.zendure_1_errors_last_hour']['buffer_usage_ratio'] = 0.01
        self.assertEqual(self.tick()['connection_since'], 0)

    def test_notification_combines_causes_and_enforces_daily_limit(self):
        repeat = self.package['automation'][0]['actions'][0]['repeat']
        self.assertEqual(repeat['for_each'], [1, 2, 3])
        condition = repeat['sequence'][1]['if'][0]['value_template']
        for cause in ['connection_alert', 'wifi_alert']:
            for elapsed, expected in [(0, False), (86399, False), (86400, True)]:
                rendered = self.render(condition, m={cause: True}, last_notification=self.ts-elapsed)
                self.assertEqual(ast.literal_eval(rendered), expected)
        self.assertFalse(ast.literal_eval(self.render(condition, m={}, last_notification=0)))
        actions = repeat['sequence'][1]['then']
        self.assertEqual(actions[1]['action'], 'input_number.set_value')
        self.assertEqual(actions[2]['action'], 'persistent_notification.create')
        self.assertTrue(actions[3]['continue_on_error'])
        helper = next(iter(self.package['input_number'].values()))
        self.assertNotIn('initial', helper)
        self.assertEqual(helper['min'], 0)
        self.assertGreater(helper['max'], self.ts)


if __name__ == '__main__':
    unittest.main()
