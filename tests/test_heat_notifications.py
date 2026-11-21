"""Surface heat detection and mobile alert behavior at the HA boundary."""

from datetime import timedelta
import importlib
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_coordinator as adapter
from custom_components.apartment_comfort.engine import Decision
from custom_components.apartment_comfort.notifications import ComfortNotifications


class HeatNotificationTests(unittest.TestCase):
    def setUp(self):
        fixture = adapter.AdapterTests()
        fixture.setUp()
        self.c, self.states, self.hass = fixture.c, fixture.states, fixture.hass
        self.c.config.update(radiator_temperature_entity="sensor.surface")
        self.states["sensor.surface"] = adapter.state("20", unit_of_measurement="°C")
        self.states["climate.rad"] = adapter.state("heat", hvac_modes=["heat"], min_temp=5,
                                                   temperature=22, hvac_action="heating")

    def read(self, surface):
        self.states["sensor.surface"].state = str(surface)
        self.c.inputs = self.c._read()
        return self.c.inputs.radiator_heating

    def test_surface_overrides_stuck_heating_with_hysteresis(self):
        self.assertIsNone(self.read(22))
        self.assertTrue(self.read(23))
        self.assertTrue(self.read(22))
        self.assertFalse(self.read(21))
        self.assertFalse(self.read(22))
        self.assertEqual(self.c._thermal_regime(), "idle")
        self.assertTrue(self.read(30))
        self.assertEqual(self.c._thermal_regime(), "radiator")
        self.states["climate.rad"].state = "off"
        self.assertEqual(self.c._thermal_regime(), "radiator")  # Residual heat.

    def test_invalid_sensor_resets_hysteresis_without_hvac_fallback(self):
        self.assertTrue(self.read(30))
        for invalid in ("unavailable", "nan", "invalid"):
            self.assertIsNone(self.read(invalid))
        self.assertIsNone(self.read(22))
        self.states["sensor.surface"].last_reported -= timedelta(days=2)
        self.assertIsNone(self.read(30))
        self.assertIsNone(self.c._thermal_regime())

    def test_surface_fahrenheit_and_missing_room(self):
        self.states["sensor.surface"].attributes["unit_of_measurement"] = "°F"
        self.assertTrue(self.read(86))
        self.states["sensor.room"].state = "unavailable"
        self.assertIsNone(self.read(86))

    def test_virtual_thermostat_uses_surface_not_raw_heating(self):
        modules = {}
        for name, attrs in (
            ("homeassistant.components.climate", {"ClimateEntity": type("Climate", (), {})}),
            ("homeassistant.components.climate.const", {
                "ClimateEntityFeature": SimpleNamespace(TARGET_TEMPERATURE=1, PRESET_MODE=2),
                "HVACAction": SimpleNamespace(OFF="off", IDLE="idle", HEATING="heating", COOLING="cooling"),
                "HVACMode": str}),
            ("homeassistant.const", {"ATTR_TEMPERATURE": "temperature", "UnitOfTemperature": SimpleNamespace(CELSIUS="°C")}),
            ("homeassistant.exceptions", {"ServiceValidationError": ValueError}),
            ("custom_components.apartment_comfort.entity", {"ComfortEntity": type("Entity", (), {})}),
        ):
            modules[name] = ModuleType(name)
            modules[name].__dict__.update(attrs)
        with patch.dict(sys.modules, modules):
            sys.modules.pop("custom_components.apartment_comfort.climate", None)
            climate = importlib.import_module("custom_components.apartment_comfort.climate")
            entity = SimpleNamespace(coordinator=self.c)
            self.c.config["control_enabled"] = True
            self.c.owned.add("climate.rad")
            self.read(20)
            self.assertEqual(climate.ComfortClimate.hvac_action.fget(entity), "idle")
            self.read(30)
            self.assertEqual(climate.ComfortClimate.hvac_action.fget(entity), "heating")
            self.c.mode = "off"
            self.assertEqual(climate.ComfortClimate.hvac_action.fget(entity), "off")

    @adapter.immediate_coroutine_test
    async def test_surface_controls_fan_confirmation(self):
        self.c.config.update(control_enabled=True, radiator_fan_entity="switch.fan")
        self.c.engine.config.update(self.c.config)
        self.states["switch.fan"] = adapter.state("off")
        self.states["sensor.surface"].state = "22.5"
        await self.c.async_tick()
        self.assertEqual(self.states["switch.fan"].state, "off")
        self.states["sensor.surface"].state = "25"
        await self.c.async_tick()
        self.assertEqual(self.states["switch.fan"].state, "on")

    @adapter.immediate_coroutine_test
    async def test_cooling_waits_for_surface_despite_stuck_action(self):
        self.c.config["control_enabled"] = True
        self.c.mode = "cool"
        self.c.ac_stopped = -100000
        self.states["sensor.room"].state = "27"
        self.states["sensor.surface"].state = "40"
        await self.c.async_tick()
        self.assertEqual(self.states["climate.ac"].state, "off")
        self.states["sensor.surface"].state = "27"
        await self.c.async_tick()
        self.assertEqual(self.states["climate.ac"].state, "cool")

    def alerts(self):
        self.c.config.update(notifications_enabled=True, notification_service="notify.mobile_app_phone",
                             notification_delay_minutes=1, notification_repeat_minutes=15,
                             control_enabled=True)
        self.read(20)
        self.c.decision = Decision(22, "heating", "radiator_priority", radiator=True)
        self.hass.services.async_call = AsyncMock()
        return self.c.notifications

    @adapter.immediate_coroutine_test
    async def test_delay_dedup_repeat_and_restart_cooldown(self):
        alerts = self.alerts()
        with patch("custom_components.apartment_comfort.notifications.time", return_value=10000):
            await alerts.async_update(0)
            await alerts.async_update(59)
            self.hass.services.async_call.assert_not_awaited()
            await alerts.async_update(60)
            self.hass.services.async_call.assert_awaited_once()
            payload = self.hass.services.async_call.call_args.args[2]
            self.assertIn("не подтверждена", payload["message"])
            self.assertEqual(payload["data"]["tag"], "test:response:radiator_entity")
            await alerts.async_update(120)
            restored = ComfortNotifications(self.c)
            restored.restore(alerts.snapshot())
            await restored.async_update(100)
            await restored.async_update(200)
            self.assertEqual(self.hass.services.async_call.await_count, 1)
        with patch("custom_components.apartment_comfort.notifications.time", return_value=10900):
            await restored.async_update(1000)
            self.assertEqual(self.hass.services.async_call.await_count, 2)

    @adapter.immediate_coroutine_test
    async def test_transient_problem_and_manual_pause_reset_delay(self):
        alerts = self.alerts()
        await alerts.async_update(0)
        self.c.manual_hold = True
        await alerts.async_update(61)
        self.c.manual_hold = False
        await alerts.async_update(62)
        self.read(30)
        await alerts.async_update(123)
        self.hass.services.async_call.assert_not_awaited()

    @adapter.immediate_coroutine_test
    async def test_disabled_alerts_and_delivery_failure_retry(self):
        alerts = self.alerts()
        self.c.config["notifications_enabled"] = False
        await alerts.async_update(0)
        await alerts.async_update(100)
        self.hass.services.async_call.assert_not_awaited()
        self.c.config["notifications_enabled"] = True
        self.hass.services.async_call.side_effect = adapter.FakeError("offline")
        await alerts.async_update(101)
        await alerts.async_update(161)
        self.assertFalse(alerts.sent)
        self.assertTrue(any("delivery failed" in issue for issue in self.c.issues))
        self.c.issues.clear()
        await alerts.async_update(162)
        self.assertEqual(self.hass.services.async_call.await_count, 1)
        self.hass.services.async_call.side_effect = None
        await alerts.async_update(221)
        self.assertIn("response:radiator_entity", alerts.sent)

    def test_fan_response_and_sensor_fault_conditions(self):
        alerts = self.alerts()
        self.c.config["radiator_fan_entity"] = ["fan.rad", "switch.rad"]
        self.states["fan.rad"] = adapter.state("on", percentage=20)
        self.states["switch.rad"] = adapter.state("off")
        self.c.owned.update(("fan.rad", "switch.rad"))
        self.c.decision.fan_percent = 60
        self.assertIn("fan:fan.rad", alerts.conditions())
        self.assertIn("fan:switch.rad", alerts.conditions())
        self.states["fan.rad"].attributes["percentage"] = 60
        self.states["switch.rad"].state = "on"
        self.assertNotIn("fan:fan.rad", alerts.conditions())
        self.assertNotIn("fan:switch.rad", alerts.conditions())
        self.c.inputs.temperature = None
        self.assertIn("room_sensor", alerts.conditions())

    def test_unconfirmed_setpoint_and_stop_reported_even_when_heating(self):
        alerts = self.alerts()
        self.read(30)
        self.c.owned.add("climate.rad")
        self.c.last_commands["climate.rad"] = (("heat", 25), 0)
        self.assertIn("response:radiator_entity", alerts.conditions())
        self.states["climate.rad"].attributes["temperature"] = 25
        self.assertNotIn("response:radiator_entity", alerts.conditions())
        self.c.decision.radiator = False
        self.c.last_commands["climate.rad"] = (("heat", 5), 0)
        self.assertIn("response:radiator_entity", alerts.conditions())

    def test_outage_requires_cold_weather_and_evidence(self):
        alerts = self.alerts()
        self.c.config.update(heating_available_entity="binary_sensor.heating", control_enabled=False)
        self.c.inputs.heating_available = False
        self.c.inputs.outdoor_temperature = 0
        self.assertIn("heating_outage", alerts.conditions())  # Works in observation.
        self.c.inputs.outdoor_temperature = 10
        self.assertNotIn("heating_outage", alerts.conditions())
        self.c.inputs.outdoor_temperature = None
        self.assertNotIn("heating_outage", alerts.conditions())
        self.c.config.update(control_enabled=True, pipe_temperature_entity="sensor.pipe")
        self.c.inputs.heating_available = True
        self.c.inputs.outdoor_temperature = 0
        self.c.inputs.pipe_temperature = 20
        self.assertIn("heating_outage_suspected", alerts.conditions())
        self.c.decision.radiator = False
        self.assertNotIn("heating_outage_suspected", alerts.conditions())
        self.c.config["notify_heating_outage"] = False
        self.c.inputs.heating_available = False
        self.assertNotIn("heating_outage", alerts.conditions())
