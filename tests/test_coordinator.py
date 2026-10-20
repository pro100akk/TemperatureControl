"""Adapter tests with a small HA boundary double, not an HA integration test."""

import asyncio
from functools import wraps
from datetime import datetime, timedelta, timezone
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from unittest.mock import patch

import bootstrap  # noqa: F401


def module(name, **attrs):
    obj = ModuleType(name)
    obj.__dict__.update(attrs)
    sys.modules[name] = obj
    return obj


class FakeError(Exception):
    pass


class FakeCoordinator:
    def __init__(self, hass, *args, **kwargs):
        self.hass = hass

    def async_set_updated_data(self, data):
        self.data = data


class FakeStore:
    def __init__(self, *args):
        self.data = None

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.data = data

    def async_delay_save(self, callback, delay):
        self.data = callback()


class Converter:
    @staticmethod
    def convert(value, source, target):
        if source not in ("°C", "°F") or target not in ("°C", "°F"):
            raise ValueError("unknown unit")
        if source == target:
            return value
        return value * 9 / 5 + 32 if source == "°C" else (value - 32) * 5 / 9


module("homeassistant")
module("homeassistant.core", callback=lambda f: f, Context=lambda: SimpleNamespace(id="integration-context"))
module("homeassistant.exceptions", HomeAssistantError=FakeError)
module("homeassistant.helpers")
module("homeassistant.helpers.event", async_track_state_change_event=lambda *a: lambda: None,
       async_track_time_interval=lambda *a: lambda: None)
module("homeassistant.helpers.storage", Store=FakeStore)
module("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=FakeCoordinator)
dt_module = module("homeassistant.util.dt", utcnow=lambda: datetime.now(timezone.utc))
module("homeassistant.util", dt=dt_module)
module("homeassistant.util.unit_conversion", TemperatureConverter=Converter)

from custom_components.apartment_comfort.coordinator import ComfortCoordinator


def state(value, **attrs):
    return SimpleNamespace(state=value, attributes=attrs, last_updated=datetime.now(timezone.utc),
                           last_reported=datetime.now(timezone.utc))


class AdapterTests(unittest.TestCase):
    async def test_pipe_mode_starts_and_stops_relay_from_actual_heating(self):
        self.c.config.update(control_enabled=True, fan_temperature_source="pipe",
                             pipe_temperature_entity="sensor.pipe", radiator_fan_entity="light.fan")
        self.c.engine.config.update(self.c.config)
        self.states["sensor.pipe"] = state("40", unit_of_measurement="°C")
        self.states["light.fan"] = state("off")
        self.states["climate.rad"].attributes["hvac_action"] = "idle"
        await self.c.async_tick()
        self.assertEqual(self.states["light.fan"].state, "off")
        self.states["climate.rad"].attributes["hvac_action"] = "heating"
        await self.c.async_tick()
        self.assertEqual(self.states["light.fan"].state, "on")
        self.assertEqual(self.c.fan_status, "on")
        self.states["climate.rad"].attributes["hvac_action"] = "idle"
        await self.c.async_tick()
        self.assertEqual(self.states["light.fan"].state, "off")
        self.assertEqual(self.c.fan_status, "radiator_not_heating")

    async def test_optional_stale_sensor_does_not_reset_pi_integral(self):
        self.c.config.update(control_enabled=True, controller="pi",
                             outdoor_humidity_entity="sensor.outdoor_humidity")
        self.c.engine.config.update(self.c.config)
        self.states["sensor.room"].state = "21"
        self.states["sensor.outdoor_humidity"] = state("50", unit_of_measurement="%")
        self.states["sensor.outdoor_humidity"].last_reported -= timedelta(days=1)
        with patch("custom_components.apartment_comfort.coordinator.monotonic", return_value=1000):
            await self.c.async_tick()
        with patch("custom_components.apartment_comfort.coordinator.monotonic", return_value=1060):
            await self.c.async_tick()
        self.assertGreater(self.c.engine.pid.integral, 0)
        self.assertIn("outdoor_humidity_entity: stale", self.c.issues)

    async def test_failed_command_still_resets_pi_integral(self):
        self.c.config.update(control_enabled=True, controller="pi")
        self.c.engine.config.update(self.c.config)
        self.states["sensor.room"].state = "21"
        self.hass.services.async_call.side_effect = FakeError("failed")
        await self.c.async_tick()
        self.assertEqual(self.c.engine.pid.integral, 0)
        self.assertIsNone(self.c.engine.pid.previous)

    async def test_compensation_does_not_seize_unowned_or_use_stale_local_sensor(self):
        self.c.inputs.temperature = 25.5
        self.states["climate.rad"].state = "heat"
        self.states["climate.rad"].attributes.update(current_temperature=26, temperature=26)
        self.assertFalse(await self.c._climate("radiator_entity", True, "heat", 26))
        self.hass.services.async_call.assert_not_awaited()
        self.c.owned.add("climate.rad")
        self.states["climate.rad"].last_reported -= timedelta(days=1)
        await self.c._climate("radiator_entity", True, "heat", 26)
        self.assertFalse(self.c.radiator_command["compensation_available"])
        self.assertEqual(self.c.radiator_command["setpoint"], 26)

    async def test_radiator_local_sensor_compensation_and_stop_at_room_target(self):
        self.c.config["control_enabled"] = True
        self.states["sensor.room"].state = "25.5"
        self.states["climate.rad"].attributes.update(current_temperature=26, temperature=26)
        await self.c.async_initialize()
        await self.c.async_set_target(26)
        self.assertEqual(self.states["climate.rad"].attributes["temperature"], 26.5)
        self.assertEqual(self.c.radiator_command["compensation"], 0.5)
        self.states["sensor.room"].state = "26"
        await self.c.async_tick()
        self.assertEqual(self.states["climate.rad"].state, "off")

    async def test_compensation_is_bounded_and_does_not_change_floor_or_ac(self):
        self.c.inputs.temperature = 20
        self.states["climate.rad"].attributes["current_temperature"] = 45
        await self.c._climate("radiator_entity", True, "heat", 22)
        self.assertEqual(self.states["climate.rad"].attributes["temperature"], 30)
        self.assertEqual(self.c.radiator_command["compensation"], 10)
        self.assertIn("radiator_entity: setpoint limit prevents heat demand", self.c.issues)
        self.c.config["floor_entity"] = "climate.floor"
        self.states["climate.floor"] = state("off", hvac_modes=["off", "heat"], current_temperature=30)
        await self.c._climate("floor_entity", True, "heat", 22)
        self.assertEqual(self.states["climate.floor"].attributes["temperature"], 22)

    async def test_compensation_fahrenheit_and_disabled(self):
        self.c.inputs.temperature = 25.5
        self.hass.config.units.temperature_unit = "°F"
        self.states["climate.rad"].attributes.update(current_temperature=78.8, min_temp=40, max_temp=90)
        await self.c._climate("radiator_entity", True, "heat", 26)
        self.assertEqual(self.states["climate.rad"].attributes["temperature"], 80)
        self.c.config["radiator_compensation"] = False
        await self.c._climate("radiator_entity", True, "heat", 26)
        self.assertEqual(self.states["climate.rad"].attributes["temperature"], 79)

    async def test_missing_or_invalid_local_reading_falls_back(self):
        self.c.inputs.temperature = 20
        for value in (None, float("nan"), "unavailable"):
            self.states["climate.rad"].attributes["current_temperature"] = value
            await self.c._climate("radiator_entity", True, "heat", 22)
            self.assertEqual(self.states["climate.rad"].attributes["temperature"], 22)
            self.assertFalse(self.c.radiator_command["compensation_available"])

    async def test_unchanged_off_devices_do_not_prevent_learning(self):
        self.c.config.update(outdoor_temperature_entity="sensor.outside", radiator_fan_entity="light.fan")
        self.states["sensor.outside"] = state("5", unit_of_measurement="°C")
        self.states["light.fan"] = state("off")
        for key in ("climate.rad", "climate.ac", "light.fan"):
            self.states[key].last_reported -= timedelta(days=2)
        self.c.inputs = self.c._read()
        self.assertEqual(self.c._thermal_regime(), "idle")
        for i in range(14):
            self.c.engine.model.observe(20, 5, self.c._thermal_regime(), i * 300)
        self.assertEqual(self.c.forecast_status, "ready")
        self.assertEqual(self.c.engine.model.forecast(20, 5, 30), 20)
        self.states["light.fan"].state = "unavailable"
        self.assertIsNone(self.c._thermal_regime())

    async def test_window_heating_is_excluded_from_closed_window_forecast(self):
        self.c.config["heat_with_window_open"] = True
        self.c.inputs.window = True
        self.assertIn("|w=True", self.c._thermal_regime())

    async def test_model_uses_actual_action_not_requested_mode(self):
        self.c.inputs = self.c._read()
        self.assertEqual(self.c._thermal_regime(), "idle")
        self.states["climate.rad"].state = "heat"
        self.assertIsNone(self.c._thermal_regime())
        self.states["climate.rad"].attributes["hvac_action"] = "heating"
        self.assertEqual(self.c._thermal_regime(), "radiator")
        self.c.manual_hold = True
        self.assertEqual(self.c._thermal_regime(), "radiator")

    async def test_learned_model_survives_restart(self):
        for i in range(14):
            self.c.engine.model.observe(20 + i / 12, 5, "idle", i * 300)
        saved = self.c._snapshot()
        restored = ComfortCoordinator(self.hass, self.entry)
        restored.store.data = saved
        await restored.async_initialize()
        self.assertEqual(restored.engine.model.rates, saved["thermal_model"])

    async def test_pi_setpoint_reaches_appliance(self):
        self.c.config.update(control_enabled=True, controller="pi")
        self.c.engine.config.update(self.c.config)
        self.states["sensor.room"].state = "21"
        await self.c.async_tick()
        self.assertEqual(self.c.decision.demand_percent, 50)
        self.assertEqual(self.states["climate.rad"].attributes["temperature"], 22.5)

    async def test_changed_equipment_invalidates_model(self):
        for i in range(14):
            self.c.engine.model.observe(20 + i / 12, 5, "idle", i * 300)
        saved = self.c._snapshot()
        self.entry.options = self.entry.data | {"radiator_entity": "climate.replacement"}
        restored = ComfortCoordinator(self.hass, self.entry)
        restored.store.data = saved
        await restored.async_initialize()
        self.assertEqual(restored.engine.model.rates, {})

    def setUp(self):
        self.states = {
            "sensor.room": state("20", unit_of_measurement="°C"),
            "input_select.period": state("day"),
            "climate.rad": state("off", hvac_modes=["off", "heat"], min_temp=5, max_temp=30),
            "climate.ac": state("off", hvac_modes=["off", "cool"], min_temp=16, max_temp=30),
        }
        self.hass = SimpleNamespace(
            states=SimpleNamespace(get=self.states.get),
            services=SimpleNamespace(async_call=AsyncMock(side_effect=self.apply_service)),
            config=SimpleNamespace(units=SimpleNamespace(temperature_unit="°C")),
            async_create_task=asyncio.create_task,
        )
        self.entry = SimpleNamespace(entry_id="test", data={
            "temperature_entity": "sensor.room", "period_entity": "input_select.period",
            "radiator_entity": "climate.rad", "ac_entity": "climate.ac",
        }, options={})
        self.c = ComfortCoordinator(self.hass, self.entry)

    async def apply_service(self, domain, service, data, **kwargs):
        s = self.states.get(data["entity_id"])
        if service == "set_hvac_mode":
            s.state = data["hvac_mode"]
        elif service == "set_temperature":
            s.attributes["temperature"] = data["temperature"]
        elif service in ("turn_on", "turn_off"):
            s.state = "on" if service == "turn_on" else "off"
        elif service == "set_percentage":
            s.state = "on" if data["percentage"] else "off"
            s.attributes["percentage"] = data["percentage"]

    async def test_relay_fan_on_off_and_no_speed_commands(self):
        for domain in ("switch", "light"):
            entity = f"{domain}.ventiliator"
            self.c.config["radiator_fan_entity"] = entity
            self.states[entity] = state("off")
            self.hass.services.async_call.reset_mock()
            await self.c._fan(30)
            self.assertEqual(self.states[entity].state, "on")
            self.hass.services.async_call.assert_awaited_once_with(
                domain, "turn_on", {"entity_id": entity}, blocking=True)
            await self.c._fan(60)
            self.assertEqual(self.hass.services.async_call.await_count, 1)
            await self.c._fan(0)
            self.assertEqual(self.states[entity].state, "off")
            self.assertNotIn(entity, self.c.owned)

    async def test_external_relay_is_not_seized(self):
        self.c.config["radiator_fan_entity"] = "light.ventiliator"
        self.states["light.ventiliator"] = state("on")
        await self.c._fan(30)
        await self.c._fan(0)
        self.hass.services.async_call.assert_not_awaited()

    async def test_fan_speed_control_is_preserved(self):
        self.c.config["radiator_fan_entity"] = "fan.radiator"
        self.states["fan.radiator"] = state("off", percentage=0)
        await self.c._fan(30)
        await self.c._fan(60)
        self.assertEqual(self.states["fan.radiator"].attributes["percentage"], 60)
        self.assertEqual(self.hass.services.async_call.await_count, 2)

    async def test_light_exhaust_is_read_without_commands(self):
        self.c.config["shower_exhaust_entity"] = "light.ventiliator"
        self.states["light.ventiliator"] = state("on")
        await self.c.async_tick()
        self.assertTrue(self.c.inputs.shower_exhaust)
        self.hass.services.async_call.assert_not_awaited()

    async def test_window_stops_owned_relay(self):
        self.c.config.update(control_enabled=True, radiator_fan_entity="light.ventiliator",
                             radiator_temperature_entity="sensor.surface", window_entity="binary_sensor.window")
        self.c.engine.config.update(self.c.config)
        self.states.update({"light.ventiliator": state("off"),
                            "sensor.surface": state("40", unit_of_measurement="°C"),
                            "binary_sensor.window": state("off")})
        await self.c.async_tick()
        self.assertEqual(self.states["light.ventiliator"].state, "on")
        self.states["binary_sensor.window"].state = "on"
        await self.c.async_tick()
        self.assertEqual(self.states["light.ventiliator"].state, "off")

    async def test_observation_never_issues_commands(self):
        await self.c.async_initialize()
        self.assertTrue(self.c.decision.radiator)
        self.hass.services.async_call.assert_not_awaited()

    async def test_enabled_control_uses_selected_radiator(self):
        self.c.config["control_enabled"] = True
        await self.c.async_initialize()
        self.assertEqual(self.states["climate.rad"].state, "heat")
        self.assertEqual(self.states["climate.rad"].attributes["temperature"], 22)

    async def test_missing_main_sensor_stops_owned_heat(self):
        self.c.config["control_enabled"] = True
        await self.c.async_initialize()
        self.states["sensor.room"].state = "unavailable"
        await self.c.async_tick()
        self.assertEqual(self.states["climate.rad"].state, "off")
        self.assertEqual(self.c.decision.reason, "temperature_unavailable")

    async def test_external_cooling_blocks_heat(self):
        self.c.config["control_enabled"] = True
        self.states["climate.ac"].state = "cool"
        await self.c.async_initialize()
        self.assertEqual(self.states["climate.rad"].state, "off")
        self.hass.services.async_call.assert_not_awaited()

    async def test_cannot_seize_active_external_radiator(self):
        self.c.config["control_enabled"] = True
        self.states["climate.rad"].state = "heat"
        await self.c.async_initialize()
        self.assertNotIn("climate.rad", self.c.owned)
        self.hass.services.async_call.assert_not_awaited()

    async def test_stop_requires_confirmation_not_successful_service(self):
        self.c.owned.add("climate.rad")
        self.states["climate.rad"].state = "heat"
        self.hass.services.async_call.side_effect = None
        self.assertFalse(await self.c._off("climate.rad"))
        self.assertIn("climate.rad", self.c.owned)
        self.states["climate.rad"].state = "off"
        self.assertTrue(await self.c._off("climate.rad"))

    async def test_failed_off_retains_ownership(self):
        self.c.owned.add("climate.rad")
        self.states["climate.rad"].state = "heat"
        self.hass.services.async_call.side_effect = FakeError("unreachable")
        self.assertFalse(await self.c._off("climate.rad"))
        self.assertIn("climate.rad", self.c.owned)

    async def test_ac_cooldown_after_restart(self):
        self.c.config["control_enabled"] = True
        self.c.mode = "cool"
        self.states["sensor.room"].state = "25"
        await self.c.async_tick()
        self.assertEqual(self.states["climate.ac"].state, "off")
        self.assertIn("ac_entity: minimum off interval", self.c.issues)

    async def test_manual_override_expires_on_external_period_change(self):
        await self.c.async_initialize()
        await self.c.async_set_target(23)
        self.assertEqual(self.c.decision.target, 23)
        self.states["input_select.period"].state = "night"
        await self.c.async_tick()
        self.assertEqual(self.c.decision.target, 20.5)
        self.assertIsNone(self.c.override)

    async def test_manual_hold_does_not_fight_user(self):
        self.c.config["control_enabled"] = True
        self.c.manual_hold = True
        await self.c.async_tick()
        self.hass.services.async_call.assert_not_awaited()

    async def test_stale_temperature_blocks(self):
        self.states["sensor.room"].last_reported -= timedelta(hours=3)
        await self.c.async_tick()
        self.assertIsNone(self.c.inputs.temperature)
        self.assertEqual(self.c.decision.status, "blocked")

    async def test_fahrenheit_input_and_output(self):
        self.states["sensor.room"] = state("68", unit_of_measurement="°F")
        self.hass.config.units.temperature_unit = "°F"
        self.states["climate.rad"].attributes.update(min_temp=40, max_temp=90)
        self.c.config["control_enabled"] = True
        await self.c.async_tick()
        self.assertAlmostEqual(self.c.inputs.temperature, 20)
        self.assertEqual(self.states["climate.rad"].attributes["temperature"], 71.5)

    async def test_unknown_temperature_unit_is_not_assumed_celsius(self):
        self.states["sensor.room"].attributes.clear()
        await self.c.async_tick()
        self.assertIsNone(self.c.inputs.temperature)

    async def test_shutdown_releases_owned_devices(self):
        self.c.config["control_enabled"] = True
        await self.c.async_initialize()
        self.assertTrue(await self.c.async_stop())
        self.assertEqual(self.states["climate.rad"].state, "off")
        self.assertTrue(self.c.stopping)

    async def test_restart_retains_floor_runtime_limit(self):
        from time import time
        self.entry.options = self.entry.data | {"floor_entity": "climate.floor", "floor_enabled": True,
                                                "heating_available_entity": "binary_sensor.heating"}
        self.states["binary_sensor.heating"] = state("off")
        self.c = ComfortCoordinator(self.hass, self.entry)
        self.c.store.data = {"floor_started_at": time() - 4000}
        await self.c.async_initialize()
        self.assertTrue(self.c.engine.floor_locked)
        self.assertFalse(self.c.decision.floor)

    async def test_removed_optional_binding_does_not_return_from_entry_data(self):
        self.entry.options = {key: value for key, value in self.entry.data.items() if key != "radiator_entity"}
        self.c = ComfortCoordinator(self.hass, self.entry)
        self.assertNotIn("radiator_entity", self.c.config)


def immediate_coroutine_test(test):
    """These boundary doubles do not suspend; no Windows socketpair is needed.

    Fail explicitly if a test starts requiring real event-loop scheduling.
    Real HA lifecycle/concurrency testing must run inside HA, separately.
    """
    @wraps(test)
    def wrapped(self):
        coro = test(self)
        try:
            coro.send(None)
        except StopIteration:
            return
        finally:
            coro.close()
        raise AssertionError("Test suspended: use a real HA event loop for this scenario")
    return wrapped


for _name, _test in list(vars(AdapterTests).items()):
    if _name.startswith("test_"):
        setattr(AdapterTests, _name, immediate_coroutine_test(_test))
