"""Confirmation timing and unbiased quality statistics with irregular events."""

from types import SimpleNamespace
import unittest

import bootstrap  # noqa: F401
from custom_components.apartment_comfort.monitoring import CommandMonitor, QualityMetrics


def state(value, **attrs):
    return SimpleNamespace(state=value, attributes=attrs)


class CommandTests(unittest.TestCase):
    def test_successful_service_is_not_confirmation_and_retry_keeps_deadline(self):
        monitor = CommandMonitor(timeout=60, failure_threshold=2)
        command = monitor.begin("fan.one", "set_percentage", {"percentage": 40}, 0)
        self.assertEqual(monitor.check({"fan.one": state("on", percentage=10)}, 30), [])
        self.assertEqual(monitor.begin("fan.one", "set_percentage", {"percentage": 40}, 55), command)
        result = monitor.check({"fan.one": state("on", percentage=10)}, 60)
        self.assertEqual(result[0]["outcome"], "timeouts")
        monitor.begin("fan.one", "set_percentage", {"percentage": 40}, 61)
        monitor.check({}, 121)
        self.assertEqual(len(monitor.issues()), 1)
        monitor.begin("fan.one", "set_percentage", {"percentage": 40}, 122)
        result = monitor.check({"fan.one": state("on", percentage=40)}, 124)
        self.assertEqual(result[0]["latency_seconds"], 2)
        self.assertEqual(monitor.issues(), [])
        self.assertEqual(monitor.snapshot()["devices"]["fan.one"]["mean_latency_seconds"], 2)

    def test_errors_and_superseded_or_cancelled_requests(self):
        monitor = CommandMonitor()
        monitor.begin("switch.one", "turn_on", {}, 0)
        monitor.begin("switch.one", "turn_off", {}, 1)
        result = monitor.check({"switch.one": state("off")}, 2)
        self.assertEqual(len(result), 1)
        self.assertEqual(monitor.stats["switch.one"]["timeouts"], 0)
        monitor.begin("switch.one", "turn_on", {}, 3)
        monitor.cancel("switch.one")
        self.assertEqual(monitor.check({}, 100), [])
        monitor.begin("switch.one", "turn_on", {}, 101)
        self.assertEqual(monitor.failed("switch.one", "turn_on", 102)["outcome"], "service_errors")
        self.assertEqual(monitor.check({}, 300), [])

    def test_confirmation_checks_units_setpoints_and_brightness(self):
        match = CommandMonitor.matches
        self.assertFalse(match("set_temperature", {"temperature": 72}, state("heat", temperature=22)))
        self.assertTrue(match("set_temperature", {"temperature": 72}, state("heat", temperature=72)))
        self.assertFalse(match("set_hvac_mode", {"hvac_mode": "heat"}, state("unavailable")))
        self.assertFalse(match("turn_on", {"brightness_pct": 50}, state("on", brightness=255)))
        self.assertTrue(match("turn_on", {"brightness_pct": 50}, state("on", brightness=128)))


class QualityTests(unittest.TestCase):
    def test_time_weighting_gaps_and_inactive_control(self):
        quality = QualityMetrics()
        for now, temperature, active in ((0, 24, True), (1, 24, True), (60, 22, True),
                                          (61, 22, False), (1000, 24, True)):
            quality.observe(now, temperature, 22, .3, active, None, 5, "stable")
        result = quality.snapshot()
        self.assertEqual(result["observed_minutes"], 1)
        self.assertEqual(result["mae_c"], 2)
        self.assertEqual(result["rmse_c"], 2)
        self.assertEqual(result["overheat_minutes"], 1)
        self.assertEqual(result["max_overshoot_c"], 2)

    def test_forecasts_are_scored_at_horizon_and_discarded_when_context_changes(self):
        quality = QualityMetrics()
        quality.observe(0, 22, 22, .3, True, 23, 5, "stable")
        quality.observe(299, 22.5, 22, .3, True, None, 5, "stable")
        self.assertEqual(quality.snapshot()["forecast_samples"], 0)
        quality.observe(300, 22.5, 22, .3, True, 23, 5, "stable")
        self.assertEqual(quality.snapshot()["forecast_mae_c"], .5)
        quality.observe(600, 20, 22, .3, True, None, 5, "window_open")
        self.assertEqual(quality.snapshot()["forecast_samples"], 1)
        self.assertEqual(quality.snapshot()["forecast_discarded"], 1)

    def test_missing_readings_and_late_forecasts_are_not_scored(self):
        quality = QualityMetrics()
        quality.observe(0, 22, 22, .3, True, 23, 5, "stable")
        quality.observe(500, 23, 22, .3, True, None, 5, "stable")
        self.assertEqual(quality.snapshot()["forecast_discarded"], 1)
        self.assertIsNone(quality.snapshot()["forecast_mae_c"])
        quality.observe(501, None, 22, .3, True, None, 5, "stable")
        self.assertIsNone(quality.previous)

    def test_switches_use_real_mode_changes_not_attribute_updates_or_reconnects(self):
        quality = QualityMetrics()
        for value in ("off", "off", "heat", "heat", "unavailable", "heat", "off"):
            quality.device("climate.one", state(value))
        self.assertEqual(quality.snapshot()["switches"], {"climate.one": 2})
        quality.observe(0, 22, 22, .3, True, None, 5, "stable")
        quality.observe(1800, 22, 22, .3, True, None, 5, "stable")
        self.assertEqual(quality.snapshot()["switches_per_hour"], {"climate.one": 4})
