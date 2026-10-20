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


class VentilationAdapterTests(unittest.TestCase):
    setUp = adapter.AdapterTests.setUp
    apply_service = adapter.AdapterTests.apply_service

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

    async def test_automation_event_holds_exhaust_without_global_pause(self):
        self.c.config["shower_exhaust_entity"] = "switch.shower"
        self.c.owned.add("switch.shower")
        old, new = state("on"), state("off")
        new.context = SimpleNamespace(id="external", parent_id="other-automation", user_id=None)
        self.hass.async_create_task = lambda coro: coro.close()
        self.c._changed(SimpleNamespace(data={"entity_id": "switch.shower", "old_state": old, "new_state": new}))
        self.assertIn("switch.shower", self.c.exhaust_holds)
        self.assertFalse(self.c.manual_hold)
        self.assertNotIn("switch.shower", self.c.owned)

    async def test_own_delayed_event_does_not_hold(self):
        self.c.config["shower_exhaust_entity"] = "switch.shower"
        self.c.owned.add("switch.shower")
        self.c.exhaust_contexts.append("own")
        old, new = state("off"), state("on")
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


for name, test in list(vars(VentilationAdapterTests).items()):
    if name.startswith("test_"):
        setattr(VentilationAdapterTests, name, immediate_coroutine_test(test))
