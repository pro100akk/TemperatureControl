"""Scenario tests for control and recommendations, using the production policy."""

import unittest

import bootstrap  # noqa: F401
from custom_components.apartment_comfort.engine import Engine, Inputs, absolute_humidity


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine({
            "radiator_entity": "climate.radiator", "radiator_fan_entity": "fan.radiator",
            "floor_entity": "climate.floor", "ac_entity": "climate.ac", "floor_enabled": True,
        })

    def evaluate(self, now=0, mode="heat", **kwargs):
        return self.engine.evaluate(Inputs(**({"temperature": 21, "period": "day"} | kwargs)), now, mode)

    def test_all_external_periods(self):
        for period, expected in [("morning", 22), ("day", 22), ("evening", 22), ("pre_sleep", 21), ("night", 20.5), ("pre_wake", 22)]:
            self.assertEqual(self.evaluate(period=period).target, expected)

    def test_idle_reason_reports_temperature_outside_band_without_actuation(self):
        for controller in ("hysteresis", "pi", "pid"):
            for mode, temperature, reason in (("heat", 24, "above_target"),
                                               ("cool", 20, "below_target"),
                                               ("heat", 22, "within_band")):
                engine = Engine({"controller": controller, "radiator_entity": "climate.rad",
                                 "ac_entity": "climate.ac"})
                d = engine.evaluate(Inputs(temperature=temperature, period="day"), 0, mode)
                self.assertEqual((d.status, d.reason), ("idle", reason))
                self.assertFalse(d.radiator or d.floor or d.cooling)

    def test_custom_period_values(self):
        engine = Engine({"night_state": "Сон"})
        self.assertEqual(engine.evaluate(Inputs(temperature=20, period="Сон"), 0).target, 20.5)

    def test_custom_pre_wake_target_and_quiet_fan(self):
        engine = Engine({"pre_wake_state": "Перед пробуждением", "pre_wake_temperature": 23,
                         "radiator_entity": "climate.radiator", "radiator_fan_entity": "fan.radiator"})
        decision = engine.evaluate(Inputs(temperature=18, period="Перед пробуждением", radiator_temperature=40), 0)
        self.assertEqual(decision.target, 23)
        self.assertGreater(decision.fan_percent, 0)
        self.assertLessEqual(decision.fan_percent, 30)

    def test_unknown_period_blocks(self):
        decision = self.evaluate(period="unknown")
        self.assertEqual(decision.reason, "unknown_day_period")
        self.assertFalse(decision.radiator)

    def test_manual_override(self):
        result = self.engine.evaluate(Inputs(temperature=20, period="other"), 0, override=23)
        self.assertEqual(result.target, 23)

    def test_missing_temperature_blocks(self):
        for value in (None, float("nan"), float("inf")):
            d = self.evaluate(temperature=value)
            self.assertEqual(d.status, "blocked")
            self.assertFalse(d.floor or d.radiator or d.cooling)

    def test_window_immediately_stops_heat(self):
        self.assertTrue(self.evaluate().radiator)
        d = self.evaluate(window=True)
        self.assertEqual(d.status, "ventilating")
        self.assertFalse(d.radiator or d.floor or d.cooling)

    def test_unknown_window_blocks(self):
        self.assertEqual(self.evaluate(window=None).status, "blocked")

    def test_window_heating_opt_in_keeps_heat_and_fan_but_not_cooling(self):
        self.engine.config["heat_with_window_open"] = True
        d = self.evaluate(window=True, radiator_temperature=40, co2=1500)
        self.assertTrue(d.radiator)
        self.assertGreater(d.fan_percent, 0)
        self.assertEqual(d.recommendation, "keep_window_open")
        self.assertFalse(self.evaluate(window=True, mode="cool", temperature=25).cooling)
        self.assertEqual(self.evaluate(window=None).status, "blocked")
        self.assertFalse(self.evaluate(window=True, mode="off").radiator)

    def test_window_opt_in_preserves_floor_delay_and_runtime_limit(self):
        self.engine.config["heat_with_window_open"] = True
        self.assertFalse(self.evaluate(window=True, heating_available=False).floor)
        self.assertTrue(self.evaluate(now=1801, window=True, heating_available=False).floor)
        self.assertFalse(self.evaluate(now=5402, window=True, heating_available=False).floor)

    def test_fan_reasons_and_exact_threshold(self):
        self.assertEqual(self.evaluate().fan_reason, "surface_temperature_unavailable")
        self.assertEqual(self.evaluate(radiator_temperature=23).fan_reason, "radiator_not_hot")
        self.assertEqual(self.evaluate(radiator_temperature=23.1).fan_reason, "requested")
        self.engine.config["max_fan_percent"] = 0
        d = self.evaluate(radiator_temperature=40)
        self.assertEqual(d.fan_reason, "speed_limit_zero")
        self.assertEqual(d.fan_percent, 0)

    def test_sustained_deficit_increases_fan_despite_valve_pid_resets(self):
        from custom_components.apartment_comfort.replay import checkpoint, replay_cycle
        from dataclasses import asdict
        import json

        for controller in ("pid", "pi", "hysteresis"):
            engine = Engine(self.engine.config | {"controller": controller})
            data = Inputs(temperature=21.5, period="day", radiator_temperature=40)
            first = engine.evaluate(data, 0).fan_percent
            for now in range(60, 12001, 60):
                engine.pid.reset()
                saved = checkpoint(engine, now, None)
                decision = engine.evaluate(data, now)
                self.assertEqual(decision.fan_percent, min(100, first + 5 * (now // 600)))
            record = dict(replay=saved, inputs=asdict(data), mode="heat", decision=asdict(decision))
            self.assertFalse(replay_cycle(json.loads(json.dumps(record)))["differences"])
            data.window = True
            self.assertEqual(engine.evaluate(data, now + 10).fan_percent, 0)
            data.window = False
            self.assertEqual(engine.evaluate(data, now + 20).fan_percent, first)

    def test_fan_ramp_respects_quiet_limit_and_restarts_after_gap_or_recovery(self):
        self.engine.config.update(controller="hysteresis", night_temperature=22, night_fan_percent=37)
        data = Inputs(temperature=21.5, period="night", radiator_temperature=40)
        for now in range(0, 1801, 300):
            decision = self.engine.evaluate(data, now)
        self.assertEqual(decision.fan_percent, 37)
        self.assertEqual(self.engine.evaluate(data, 2400).fan_percent, 30)
        self.engine.evaluate(data, 2700)
        data.temperature = 21.9
        self.assertEqual(self.engine.evaluate(data, 3000).fan_percent, 20)
        data.temperature = 21.5
        self.assertEqual(self.engine.evaluate(data, 3010).fan_percent, 30)

    def test_pipe_fan_mode_requires_confirmed_heating_and_hot_pipe(self):
        self.engine.config["fan_temperature_source"] = "pipe"
        for heating in (False, None):
            d = self.evaluate(pipe_temperature=40, radiator_heating=heating)
            self.assertEqual(d.fan_percent, 0)
            self.assertEqual(d.fan_reason, "radiator_not_heating")
        self.assertEqual(self.evaluate(radiator_heating=True).fan_reason, "pipe_temperature_unavailable")
        self.assertEqual(self.evaluate(pipe_temperature=23, radiator_heating=True).fan_percent, 0)
        d = self.evaluate(pipe_temperature=40, radiator_heating=True)
        self.assertGreater(d.fan_percent, 0)
        self.assertEqual(self.evaluate(pipe_temperature=40, radiator_heating=True, window=True).fan_percent, 0)
        self.engine.config["heat_with_window_open"] = True
        self.assertGreater(self.evaluate(pipe_temperature=40, radiator_heating=True, window=True).fan_percent, 0)
        self.assertEqual(self.evaluate(pipe_temperature=40, radiator_heating=True, temperature=22).fan_percent, 0)

    def test_surface_mode_does_not_silently_fall_back_to_pipe(self):
        self.assertEqual(self.evaluate(pipe_temperature=40, radiator_heating=True).fan_percent, 0)

    def test_radiator_has_priority_and_floor_not_used_for_small_error(self):
        d = self.evaluate(temperature=20)
        self.assertTrue(d.radiator)
        self.assertFalse(self.evaluate(now=10000, temperature=20).floor)

    def test_confirmed_outage_waits_then_uses_floor(self):
        self.assertFalse(self.evaluate(heating_available=False).floor)
        d = self.evaluate(now=1801, heating_available=False)
        self.assertTrue(d.floor)
        self.assertFalse(d.radiator)

    def test_unknown_heating_does_not_trigger_reserve(self):
        self.evaluate(heating_available=None)
        self.assertFalse(self.evaluate(now=9000, heating_available=None).floor)

    def test_floor_disabled(self):
        self.engine.config["floor_enabled"] = False
        self.evaluate(heating_available=False)
        self.assertFalse(self.evaluate(now=9000, heating_available=False).floor)

    def test_floor_max_runtime_latches(self):
        self.evaluate(heating_available=False)
        self.assertTrue(self.evaluate(now=1801, heating_available=False).floor)
        self.assertFalse(self.evaluate(now=5402, heating_available=False).floor)
        self.assertTrue(self.engine.floor_locked)
        self.assertFalse(self.evaluate(now=10000, heating_available=False).floor)
        self.evaluate(now=10001, temperature=22)
        self.assertFalse(self.engine.floor_locked)

    def test_window_cancels_deficit_timer(self):
        self.evaluate(heating_available=False)
        self.evaluate(now=1500, window=True, heating_available=False)
        self.assertFalse(self.evaluate(now=1801, heating_available=False).floor)

    def test_hysteresis(self):
        self.assertFalse(self.evaluate(temperature=21.9).radiator)
        self.assertTrue(self.evaluate(temperature=21.5).radiator)
        self.assertTrue(self.evaluate(temperature=21.9).radiator)
        self.assertFalse(self.evaluate(temperature=22).radiator)

    def test_fan_requires_hot_radiator(self):
        self.assertEqual(self.evaluate(radiator_temperature=22).fan_percent, 0)
        self.assertGreater(self.evaluate(radiator_temperature=40).fan_percent, 0)
        self.assertEqual(self.evaluate(radiator_temperature=None).fan_percent, 0)

    def test_night_fan_limit(self):
        self.assertLessEqual(self.evaluate(period="night", temperature=18, radiator_temperature=40).fan_percent, 30)

    def test_cooling_exclusive(self):
        d = self.evaluate(mode="cool", temperature=24)
        self.assertTrue(d.cooling)
        self.assertFalse(d.radiator or d.floor)

    def test_off_retains_air_advice(self):
        d = self.evaluate(mode="off", co2=1500)
        self.assertFalse(d.radiator or d.floor or d.cooling)
        self.assertEqual(d.recommendation, "open_window")

    def test_co2_latch_and_clear(self):
        self.assertEqual(self.evaluate(co2=1200).recommendation, "open_window")
        self.assertEqual(self.evaluate(co2=900).recommendation, "open_window")
        self.assertEqual(self.evaluate(co2=700).recommendation, "none")

    def test_missing_co2_does_not_clear_previous_alarm(self):
        self.evaluate(co2=1500)
        self.assertIn("co2", self.evaluate(co2=None, window=True).ventilation_reasons)

    def test_close_only_after_air_clears(self):
        d = self.evaluate(window=True, co2=600, cooking=False)
        self.assertEqual(d.recommendation, "close_window")

    def test_unknown_air_requires_check(self):
        self.assertEqual(self.evaluate(window=True).recommendation, "check_air_quality_before_closing")

    def test_cold_room_with_high_co2_reports_both(self):
        d = self.evaluate(window=True, temperature=17, co2=1500)
        self.assertEqual(d.recommendation, "close_window_cold_air_quality_unresolved")
        self.assertIn("co2", d.ventilation_reasons)

    def test_cooking_not_inferred_from_high_co2(self):
        d = self.evaluate(cooking=True, co2=500)
        self.assertIn("cooking", d.ventilation_reasons)
        self.assertIn("kitchen_exhaust", d.exhaust_recommendations)

    def test_shower_recommends_exhaust(self):
        self.assertIn("shower_exhaust", self.evaluate(shower_humidity=85).exhaust_recommendations)

    def test_humidity_without_outdoor_data_is_uncertain(self):
        d = self.evaluate(room_humidity=75)
        self.assertNotIn("humidity", d.ventilation_reasons)
        self.assertIn("humidity_check_outdoor_air", d.exhaust_recommendations)

    def test_absolute_humidity_handles_cold_humid_outside(self):
        d = self.evaluate(room_humidity=70, outdoor_temperature=0, outdoor_humidity=95)
        self.assertIn("humidity", d.ventilation_reasons)

    def test_hot_humid_outside_not_recommended_for_drying(self):
        d = self.evaluate(room_humidity=70, outdoor_temperature=30, outdoor_humidity=90)
        self.assertNotIn("humidity", d.ventilation_reasons)

    def test_absolute_humidity_known_value(self):
        self.assertAlmostEqual(absolute_humidity(20, 50), 8.64, delta=0.1)

    def test_free_cooling(self):
        self.assertIn("free_cooling", self.evaluate(temperature=25, outdoor_temperature=15).ventilation_reasons)
        self.assertNotIn("free_cooling", self.evaluate(temperature=25, outdoor_temperature=28).ventilation_reasons)

    def test_presence_never_misclassifies_sleep_as_away(self):
        self.assertEqual(self.evaluate(period="night", presence=False).target, 20.5)


if __name__ == "__main__":
    unittest.main()
