"""Forecast under ventilation, calibrated speeds, weather and shared ownership."""
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import bootstrap  # noqa: F401
import test_coordinator as adapter
from test_coordinator import state, immediate_coroutine_test, dt_module
from custom_components.apartment_comfort.adaptive import ThermalModel
from custom_components.apartment_comfort.engine import Engine, Inputs
from custom_components.apartment_comfort.const import DEFAULTS
from custom_components.apartment_comfort.ventilation import speed_curve, interpolate


class ForecastTests(unittest.TestCase):
    def test_gentle_stage_and_immediate_forecast_escalation(self):
        engine = Engine({"shower_control_enabled": True, "kitchen_control_enabled": True})
        data = Inputs(temperature=22.3, period="day", window=True, outdoor_temperature=5)
        d = engine.evaluate(data, 0, predicted_temperature=22.1)
        self.assertTrue(d.shower_requested)
        self.assertEqual(d.recommendation, "keep_window_open")
        self.assertEqual(d.kitchen_airflow_percent, 0)
        d = engine.evaluate(data, 0, predicted_temperature=22.4)
        self.assertTrue(d.shower_requested)
        self.assertEqual(d.kitchen_airflow_percent, 50)
        self.assertIn("free_cooling", d.ventilation_reasons)
        data.temperature = 23.1
        self.assertEqual(engine.evaluate(data, 0, predicted_temperature=22).kitchen_airflow_percent, 50)

    def test_gentle_stage_preserves_guards_and_air_quality(self):
        engine = Engine({"shower_control_enabled": True, "kitchen_control_enabled": True})
        for changes in ({"window": False}, {"window": None}, {"outdoor_temperature": None},
                        {"outdoor_temperature": 25}, {"temperature": None}, {"period": "unknown"}):
            data = Inputs(**(dict(temperature=22.3, period="day", window=True, outdoor_temperature=5) | changes))
            d = engine.evaluate(data, 0, predicted_temperature=24)
            self.assertFalse(d.shower_requested)
            self.assertEqual(d.kitchen_airflow_percent, 0)
        data = Inputs(temperature=22.3, period="day", window=True, outdoor_temperature=5, co2=1400)
        self.assertEqual(engine.evaluate(data, 0).kitchen_airflow_percent, 50)
        self.assertFalse(engine.evaluate(data, 0, "off").shower_requested)
        data.period, data.co2 = "night", 500
        self.assertEqual(engine.evaluate(data, 0, predicted_temperature=24).kitchen_airflow_percent, 0)

    def test_open_window_and_exhaust_forecast(self):
        model = ThermalModel()
        data = Inputs(temperature=22, outdoor_temperature=5, window=True, kitchen_airflow=80)
        value, status = model.estimate(data, 30, DEFAULTS)
        self.assertLess(value, 22)
        self.assertEqual(status, "estimated")
        data.kitchen_airflow = 0
        self.assertGreater(model.estimate(data, 30, DEFAULTS)[0], value)

    def test_shower_has_no_outdoor_effect_when_closed(self):
        model = ThermalModel()
        data = Inputs(temperature=22, outdoor_temperature=5, shower_exhaust=False)
        before = model.estimate(data, 30, DEFAULTS)
        data.shower_exhaust = True
        self.assertEqual(before, model.estimate(data, 30, DEFAULTS))

    def test_forecast_without_outdoor_or_learning(self):
        model = ThermalModel()
        self.assertEqual(model.estimate(Inputs(temperature=22), 30, DEFAULTS)[0], 22)
        self.assertIsNotNone(model.estimate(Inputs(temperature=22, window=True), 30,
                                          DEFAULTS | {"learning_enabled": False})[0])
        self.assertIsNone(model.estimate(Inputs(), 30, DEFAULTS)[0])

    def test_nonlinear_curve_and_validation(self):
        points = speed_curve(DEFAULTS["kitchen_speed_curve"])
        self.assertEqual(interpolate(50, points), 35)
        self.assertEqual(interpolate(35, points, inverse=True), 50)
        for invalid in ("0:0,50:80,100:70", "0:0,50:20,50:30,100:100", "0:0,nan:30,100:100"):
            with self.assertRaises(ValueError):
                speed_curve(invalid)

    def test_ventilating_regime_learns_and_restores(self):
        model = ThermalModel()
        regime = "radiator|w=True|k=5|s=True|cook=False|weather=cloudy"
        for i in range(14):
            model.observe(22 - i / 12, 5, regime, i * 300)
        value, status = model.estimate(
            Inputs(temperature=21, outdoor_temperature=5, window=True, kitchen_airflow=50),
            30, DEFAULTS)
        self.assertEqual(status, "ready")
        self.assertAlmostEqual(value, 20.5)
        restored = ThermalModel()
        restored.restore(model.rates)
        self.assertEqual(restored.rates, model.rates)

    def test_window_night_and_master_mode(self):
        engine = Engine({"kitchen_control_enabled": True, "shower_control_enabled": True})
        data = Inputs(temperature=23, period="day", co2=1400, shower_humidity=80, window=True)
        self.assertEqual(engine.evaluate(data, 0).kitchen_airflow_percent, 50)
        self.assertTrue(engine.evaluate(data, 0).shower_requested)
        for window in (False, None):
            data.window = window
            self.assertEqual(engine.evaluate(data, 0).kitchen_airflow_percent, 0)
        data.window, data.period = True, "night"
        self.assertEqual(engine.evaluate(data, 0).kitchen_airflow_percent, 0)
        engine.config["kitchen_night_allowed"] = True
        self.assertEqual(engine.evaluate(data, 0).kitchen_airflow_percent, 25)
        self.assertEqual(engine.evaluate(data, 0, "off").kitchen_airflow_percent, 0)

    def test_separate_shower_exchange_and_closed_window_factor(self):
        model = ThermalModel()
        data = Inputs(temperature=22, outdoor_temperature=12, shower_exhaust=True)
        config = DEFAULTS | {"background_exchange_per_hour": 0,
                             "shower_exchange_per_hour": 0.4,
                             "closed_window_exhaust_factor": 0.5}
        self.assertEqual(model.estimate(data, 60, config)[0], 20)
        data.window = True
        config["window_exchange_per_hour"] = 0
        self.assertEqual(model.estimate(data, 60, config)[0], 18)

    def test_configurable_heat_sources_and_fan_requires_heat(self):
        model = ThermalModel()
        data = Inputs(temperature=22, radiator_fan=True, computer_power=500)
        config = DEFAULTS | {"radiator_degrees_per_hour": 2,
                             "radiator_fan_degrees_per_hour": 0.4,
                             "computer_degrees_per_kwh": 0.6}
        self.assertEqual(model.estimate(data, 60, config)[0], 22.3)
        data.radiator_heating = True
        self.assertEqual(model.estimate(data, 60, config)[0], 24.7)

    def test_learned_rate_does_not_double_count_manual_coefficients(self):
        model = ThermalModel()
        for i in range(14):
            model.observe(20 + i / 12, 10, "radiator_fan", i * 300)
        data = Inputs(temperature=22, outdoor_temperature=10, radiator_heating=True,
                      radiator_fan=True, computer_power=1000)
        config = DEFAULTS | {"radiator_degrees_per_hour": 6,
                             "radiator_fan_degrees_per_hour": 6,
                             "computer_degrees_per_kwh": 6}
        self.assertEqual(model.estimate(data, 60, config), (23, "ready"))

    def test_floor_and_cooling_estimates_with_learning_disabled(self):
        model = ThermalModel()
        config = DEFAULTS | {"learning_enabled": False, "floor_degrees_per_hour": 2,
                             "cooling_degrees_per_hour": 3}
        data = Inputs(temperature=22, floor_heating=True)
        self.assertEqual(model.estimate(data, 60, config), (24, "estimated"))
        data.floor_heating, data.ac_cooling = False, True
        self.assertEqual(model.estimate(data, 60, config), (19, "estimated"))


class VentilationAdapterTests(unittest.TestCase):
    setUp = adapter.AdapterTests.setUp
    apply_service = adapter.AdapterTests.apply_service

    async def test_tick_uses_forecast_and_starts_shower_before_kitchen(self):
        self.c.config.update(control_enabled=True, shower_control_enabled=True, kitchen_control_enabled=True,
                             shower_exhaust_entity="switch.shower", kitchen_exhaust_entity="switch.hood",
                             window_entity="binary_sensor.window", outdoor_temperature_entity="sensor.outside")
        self.c.engine.config.update(self.c.config)
        self.states.update({"switch.shower": state("off"), "switch.hood": state("off"),
                            "binary_sensor.window": state("on"), "sensor.outside": state("5", unit_of_measurement="°C")})
        self.states["sensor.room"].state = "22.3"
        from unittest.mock import patch
        with patch.object(self.c.engine.model, "estimate", return_value=(22.6, "estimated")):
            await self.c.async_tick()
        calls = [call.args[2]["entity_id"] for call in self.hass.services.async_call.call_args_list]
        self.assertEqual(calls, ["switch.shower", "switch.hood"])
        self.assertEqual(self.c.decision.predicted_temperature, 22.6)

    async def test_weather_priority_delta_and_fallback(self):
        self.c.config.update(weather_entity="weather.home", outdoor_temperature_entity="sensor.outside")
        self.states["weather.home"] = state("rainy", temperature=10, temperature_unit="°C", humidity=75)
        self.states["sensor.outside"] = state("3", unit_of_measurement="°C")
        self.c.config["forecast_minutes"] = 60
        self.c.weather_rows = [{"datetime": (dt_module.utcnow() + timedelta(hours=1, milliseconds=1)).isoformat(), "temperature": 12}]
        data = self.c._read()
        self.assertEqual(data.outdoor_temperature, 3)
        self.assertAlmostEqual(data.weather_temperature_delta, 2, places=3)
        self.assertEqual(data.weather_condition, "rainy")
        self.assertEqual(data.outdoor_humidity, 75)
        self.states["sensor.outside"].state = "unavailable"
        self.assertEqual(self.c._read().outdoor_temperature, 10)

    async def test_dimmer_uses_inverse_curve(self):
        self.c.config["kitchen_exhaust_entity"] = "light.hood"
        self.states["light.hood"] = state("off", supported_color_modes=["brightness"])
        self.c.inputs.period = "day"
        self.c.inputs.window = True
        self.hass.services.async_call = AsyncMock()
        await self.c._exhaust("kitchen_exhaust_entity", 35)
        args = self.hass.services.async_call.call_args
        self.assertEqual(args.args[:2], ("light", "turn_on"))
        self.assertEqual(args.args[2]["brightness_pct"], 50)

    async def test_external_change_releases_only_exhaust(self):
        self.c.config["kitchen_exhaust_entity"] = "fan.hood"
        self.states["fan.hood"] = state("on", percentage=70)
        self.c.owned.add("fan.hood")
        self.c.exhaust_expected["fan.hood"] = ("on", 50, None)
        self.c._check_exhaust_overrides()
        self.assertIn("fan.hood", self.c.exhaust_holds)
        self.assertNotIn("fan.hood", self.c.owned)
        self.assertFalse(self.c.manual_hold)
        await self.c._exhaust("kitchen_exhaust_entity", 0)
        self.hass.services.async_call.assert_not_awaited()

    async def test_external_running_device_is_not_seized(self):
        self.c.config["shower_exhaust_entity"] = "switch.shower"
        self.states["switch.shower"] = state("on")
        await self.c._exhaust("shower_exhaust_entity", 100)
        await self.c._exhaust("shower_exhaust_entity", 0)
        self.hass.services.async_call.assert_not_awaited()

    async def test_weather_service_failure_keeps_forecast(self):
        self.c.config["weather_entity"] = "weather.home"
        from test_coordinator import FakeError
        self.hass.services.async_call = AsyncMock(side_effect=FakeError("unsupported hourly"))
        await self.c.async_tick()
        self.assertIsNotNone(self.c.decision.predicted_temperature)

    async def test_night_relay_cannot_meet_speed_limit(self):
        self.c.config["kitchen_exhaust_entity"] = "switch.hood"
        self.states["switch.hood"] = state("off")
        self.c.inputs.period = "night"
        self.c.inputs.window = True
        await self.c._exhaust("kitchen_exhaust_entity", 25)
        self.hass.services.async_call.assert_not_awaited()

    async def test_owned_hood_stops_when_window_closes(self):
        self.c.config["kitchen_exhaust_entity"] = "fan.hood"
        self.states["fan.hood"] = state("off", supported_features=1)
        self.c.inputs.window = True
        self.c.inputs.period = "day"
        await self.c._exhaust("kitchen_exhaust_entity", 35)
        self.assertEqual(self.states["fan.hood"].attributes["percentage"], 50)
        self.c.inputs.window = False
        await self.c._exhaust("kitchen_exhaust_entity", 35)
        self.assertEqual(self.states["fan.hood"].state, "off")
        self.c._check_exhaust_overrides()
        self.assertNotIn("fan.hood", self.c.exhaust_holds)

    async def test_automation_off_allows_restart_without_global_pause(self):
        self.c.config["shower_exhaust_entity"] = "switch.shower"
        self.c.owned.add("switch.shower")
        old, new = state("on"), state("off")
        old.context = SimpleNamespace(id="own", parent_id=None, user_id=None)
        new.context = SimpleNamespace(id="external", parent_id="other-automation", user_id=None)
        self.hass.async_create_task = lambda coro: coro.close()
        self.c._changed(SimpleNamespace(data={"entity_id": "switch.shower", "old_state": old, "new_state": new}))
        self.assertNotIn("switch.shower", self.c.exhaust_holds)
        self.assertFalse(self.c.manual_hold)
        self.assertNotIn("switch.shower", self.c.owned)

    async def test_own_delayed_event_does_not_hold(self):
        self.c.config["shower_exhaust_entity"] = "switch.shower"
        self.c.owned.add("switch.shower")
        self.c.exhaust_contexts.append("own")
        old, new = state("off"), state("on")
        old.context = SimpleNamespace(id="before", parent_id=None, user_id=None)
        new.context = SimpleNamespace(id="own", parent_id=None, user_id=None)
        self.hass.async_create_task = lambda coro: coro.close()
        self.c._changed(SimpleNamespace(data={"entity_id": "switch.shower", "old_state": old, "new_state": new}))
        self.assertNotIn("switch.shower", self.c.exhaust_holds)
        self.assertEqual(self.c.exhaust_expected["switch.shower"], ("on", None, None))

    async def test_exhaust_hold_survives_restart_and_schedule_resumes(self):
        from custom_components.apartment_comfort.coordinator import ComfortCoordinator
        self.entry.options = self.entry.data | {"shower_exhaust_entity": "switch.shower"}
        self.states["switch.shower"] = state("off")
        restored = ComfortCoordinator(self.hass, self.entry)
        restored.store.data = {"exhaust_holds": ["switch.shower"]}
        await restored.async_initialize()
        self.assertIn("switch.shower", restored.exhaust_holds)
        await restored.async_resume_schedule()
        self.assertFalse(restored.exhaust_holds)

    async def test_external_off_is_restarted_for_both_roles(self):
        for key, entity in (("shower_exhaust_entity", "switch.shower"),
                            ("kitchen_exhaust_entity", "switch.hood")):
            self.c.config[key] = entity
            self.states[entity] = state("off")
            self.c.inputs.window, self.c.inputs.period = True, "day"
            await self.c._exhaust(key, 100)
            self.assertIn(entity, self.c.owned)
            self.states[entity].state = "off"
            self.c._check_exhaust_overrides()
            await self.c._exhaust(key, 100)
            self.assertEqual(self.states[entity].state, "on")
            await self.c._exhaust(key, 0)
            self.assertEqual(self.states[entity].state, "off")

    async def test_external_on_context_releases_unchanged_output(self):
        entity = "switch.shower"
        self.c.config["shower_exhaust_entity"] = entity
        self.c.owned.add(entity)
        old, new = state("on"), state("on")
        old.context = SimpleNamespace(id="own", parent_id=None, user_id=None)
        new.context = SimpleNamespace(id="external", parent_id=None, user_id=None)
        self.states[entity] = new
        self.hass.async_create_task = lambda coro: coro.close()
        self.c._changed(SimpleNamespace(data={"entity_id": entity, "old_state": old, "new_state": new}))
        await self.c._exhaust("shower_exhaust_entity", 0)
        await self.c.async_stop()
        self.hass.services.async_call.assert_not_awaited()
        self.assertNotIn(entity, self.c.owned)

    async def test_legacy_hold_clears_when_off_and_requested(self):
        self.c.config["shower_exhaust_entity"] = "switch.shower"
        self.states["switch.shower"] = state("off")
        self.c.exhaust_holds.add("switch.shower")
        await self.c._exhaust("shower_exhaust_entity", 100)
        self.assertEqual(self.states["switch.shower"].state, "on")
        self.assertNotIn("switch.shower", self.c.exhaust_holds)

    async def test_restart_does_not_stop_exhaust_with_unknown_offline_owner(self):
        from custom_components.apartment_comfort.coordinator import ComfortCoordinator
        self.entry.options = self.entry.data | {"shower_exhaust_entity": "switch.shower"}
        self.states["switch.shower"] = state("on")
        restored = ComfortCoordinator(self.hass, self.entry)
        restored.store.data = {"owned": ["switch.shower"],
                               "exhaust_expected": {"switch.shower": ["on", None, None]}}
        await restored.async_initialize()
        await restored.async_stop()
        self.hass.services.async_call.assert_not_awaited()
        self.assertNotIn("switch.shower", restored.owned)


for name, test in list(vars(VentilationAdapterTests).items()):
    if name.startswith("test_"):
        setattr(VentilationAdapterTests, name, immediate_coroutine_test(test))
