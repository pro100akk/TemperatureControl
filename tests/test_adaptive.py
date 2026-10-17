"""Time, learning, persistence and safety scenarios for adaptive control."""

import unittest

import bootstrap  # noqa: F401
from custom_components.apartment_comfort.adaptive import PID, ThermalModel
from custom_components.apartment_comfort.const import DEFAULTS
from custom_components.apartment_comfort.engine import Engine, Inputs


class AdaptiveTests(unittest.TestCase):
    def train(self, model):
        for i in range(14):
            model.observe(20 + i / 12, 5, "radiator", i * 300)

    def test_learns_known_rate_and_forecast(self):
        model = ThermalModel()
        self.train(model)
        self.assertAlmostEqual(model.rates["radiator"]["rate"], 1)
        self.assertAlmostEqual(model.forecast(21, 5, 30), 21.5)

    def test_waits_for_enough_evidence(self):
        model = ThermalModel()
        model.observe(20, 5, "idle", 0)
        model.observe(20.1, 5, "idle", 300)
        self.assertIsNone(model.forecast(20.1, 5, 30))

    def test_gap_and_regime_changes_are_not_training(self):
        model = ThermalModel()
        for now, regime in ((0, "idle"), (300, "radiator"), (2000, "radiator")):
            model.observe(20, 5, regime, now)
        self.assertEqual(model.rates, {})

    def test_disabled_or_missing_data_clears_forecast(self):
        for kwargs in ({"enabled": False}, {"regime": None}, {"temperature": None}, {"outdoor": None}):
            model = ThermalModel()
            self.train(model)
            model.observe(**(dict(temperature=21, outdoor=5, regime="radiator", now=4200) | kwargs))
            self.assertIsNone(model.forecast(21, 5, 30))

    def test_jump_rejected_and_weather_extrapolation_refused(self):
        model = ThermalModel()
        self.train(model)
        count = model.rates["radiator"]["count"]
        model.observe(28, 5, "radiator", 4200)
        self.assertEqual(model.rates["radiator"]["count"], count)
        self.assertIsNone(model.forecast(21, -10, 30))

    def test_restore_validates_and_never_restores_sample_clock(self):
        source = ThermalModel()
        self.train(source)
        model = ThermalModel()
        model.restore(source.rates | {"idle": {"rate": float("nan")}})
        self.assertEqual(model.rates, source.rates)
        self.assertIsNone(model.sample)
        self.assertIsNone(model.forecast(21, 5, 30))

    def test_pid_integrates_elapsed_time_not_number_of_events(self):
        config = DEFAULTS | {"controller": "pi"}
        outputs = []
        for interval in (10, 30, 60):
            pid = PID()
            for now in range(0, 601, interval):
                pid.update(21, 22, "heat", now, config)
            outputs.append(pid.output)
        for output in outputs:
            self.assertAlmostEqual(output, 0.6)

    def test_saturation_does_not_wind_up(self):
        pid = PID()
        config = DEFAULTS | {"controller": "pi"}
        for now in range(0, 3600, 30):
            self.assertEqual(pid.update(10, 22, "heat", now, config), 1)
        self.assertEqual(pid.integral, 0)
        self.assertEqual(pid.update(23, 22, "heat", 3600, config), 0)

    def test_derivative_brakes_rising_temperature(self):
        pi, pid = PID(), PID()
        for regulator, kind in ((pi, "pi"), (pid, "pid")):
            config = DEFAULTS | {"controller": kind, "pid_ki": 0}
            regulator.update(20, 22, "heat", 0, config)
            regulator.update(21, 22, "heat", 60, config)
        self.assertLess(pid.output, pi.output)

    def test_target_mode_and_gap_reset_integral(self):
        for target, mode, now in ((21, "heat", 120), (22, "cool", 120), (22, "heat", 1000)):
            pid = PID()
            config = DEFAULTS | {"controller": "pi"}
            pid.update(21, 22, "heat", 0, config)
            pid.update(21, 22, "heat", 60, config)
            self.assertGreater(pid.integral, 0)
            pid.update(21, target, mode, now, config)
            self.assertEqual(pid.integral, 0)

    def test_pi_pid_preserve_safety_and_night_limits(self):
        for kind in ("pi", "pid"):
            engine = Engine({"controller": kind, "radiator_entity": "climate.rad",
                             "radiator_fan_entity": "fan.rad", "ac_entity": "climate.ac"})
            inputs = Inputs(temperature=19, period="night", radiator_temperature=40)
            d = engine.evaluate(inputs, 0)
            self.assertTrue(d.radiator)
            self.assertLessEqual(d.fan_percent, 30)
            self.assertLessEqual(abs(d.actuator_target - d.target), 3)
            for kwargs in ({"window": True}, {"window": None}, {"temperature": None}, {"period": "unknown"}):
                d = engine.evaluate(Inputs(**(dict(temperature=19, period="night") | kwargs)), 60)
                self.assertFalse(d.radiator or d.cooling or d.floor)
                self.assertEqual(engine.pid.integral, 0)
            d = engine.evaluate(Inputs(temperature=24, period="day"), 120, "cool")
            self.assertTrue(d.cooling)
            self.assertFalse(d.radiator or d.floor)
            self.assertLess(d.actuator_target, 24)

    def test_pi_floor_runtime_limit(self):
        engine = Engine({"controller": "pi", "floor_enabled": True, "floor_entity": "climate.floor"})
        inputs = Inputs(temperature=19, period="day", heating_available=False)
        engine.evaluate(inputs, 0)
        self.assertTrue(engine.evaluate(inputs, 1800).floor)
        self.assertFalse(engine.evaluate(inputs, 5401).floor)
        self.assertTrue(engine.floor_locked)
