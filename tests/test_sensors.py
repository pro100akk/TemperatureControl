"""Check diagnostic entity values and translations against adapter scenarios."""

import importlib
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import bootstrap  # noqa: F401
import test_coordinator as adapter_tools


class Entity:
    def __init__(self, coordinator, key):
        self.coordinator = coordinator


class SensorTests(unittest.TestCase):
    def setUp(self):
        modules = {}
        for name, attrs in (
            ("homeassistant.components.sensor", {"SensorEntity": type("Sensor", (), {}),
             "SensorDeviceClass": SimpleNamespace(TEMPERATURE="temperature", ENUM="enum")}),
            ("custom_components.apartment_comfort.entity", {"ComfortEntity": Entity}),
        ):
            module = ModuleType(name)
            module.__dict__.update(attrs)
            modules[name] = module
        with patch.dict(sys.modules, modules):
            sys.modules.pop("custom_components.apartment_comfort.sensor", None)
            self.module = importlib.import_module("custom_components.apartment_comfort.sensor")
        adapter = adapter_tools.AdapterTests()
        adapter.setUp()
        self.c = adapter.c
        self.states = adapter.states

    @adapter_tools.immediate_coroutine_test
    async def test_diagnostics_report_missing_forecast_and_surface_sensor(self):
        await self.c.async_tick()
        forecast = self.module.ComfortSensor(self.c, "forecast_status")
        self.assertEqual(forecast.native_value, "estimated")
        self.assertEqual(forecast.extra_state_attributes["required_samples"], 12)
        self.c.config.update(control_enabled=True, radiator_fan_entity="light.fan")
        self.c.engine.config.update(self.c.config)
        self.states["light.fan"] = adapter_tools.state("off")
        await self.c.async_tick()
        fan = self.module.ComfortSensor(self.c, "fan_status")
        self.assertEqual(fan.native_value, "surface_temperature_unavailable")
        self.assertEqual(fan.extra_state_attributes["required_temperature_above"], 22)

    def test_diagnostic_states_have_bilingual_translations(self):
        root = Path(__file__).resolve().parents[1] / "custom_components/apartment_comfort/translations"
        for language in ("en", "ru"):
            data = json.loads((root / f"{language}.json").read_text(encoding="utf-8"))
            for key, states in (("forecast_status", self.module.FORECAST_STATUSES),
                                ("fan_status", self.module.FAN_STATUSES)):
                self.assertEqual(set(data["entity"]["sensor"][key]["state"]), set(states))

    def test_attributes_are_safe_before_first_decision(self):
        for key in ("status", "forecast_status", "fan_status", "predicted_temperature"):
            sensor = self.module.ComfortSensor(self.c, key)
            self.assertIsNone(sensor.native_value)
            self.assertEqual(sensor.extra_state_attributes, {})
