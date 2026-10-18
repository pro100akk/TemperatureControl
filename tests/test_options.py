"""Exercise options routing and persistence with HA form boundary doubles."""

import importlib
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import bootstrap  # noqa: F401
from test_coordinator import immediate_coroutine_test
from custom_components.apartment_comfort.const import DEFAULTS


class Flow:
    def __init_subclass__(cls, **kwargs):
        pass

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def async_show_menu(self, **kwargs):
        return {"type": "menu", **kwargs}

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}


class OptionsFlow(Flow):
    pass


def fake_module(name, **attrs):
    module = ModuleType(name)
    module.__dict__.update(attrs)
    return module


class OptionsTests(unittest.TestCase):
    def setUp(self):
        selectors = SimpleNamespace(
            SelectSelector=lambda config: config,
            TextSelector=lambda: "text", NumberSelector=lambda config: config,
            BooleanSelector=lambda: "bool", EntitySelector=lambda config: config)
        registry = SimpleNamespace(async_get=lambda hass: SimpleNamespace(async_get=lambda entity: None))
        modules = {
            "probatio": fake_module("probatio", Required=lambda key, **kw: key,
                                     Optional=lambda key, **kw: key, Schema=lambda fields: fields),
            "homeassistant": fake_module("homeassistant"),
            "homeassistant.core": fake_module("homeassistant.core", callback=lambda f: f),
            "homeassistant.config_entries": fake_module("homeassistant.config_entries", ConfigFlow=Flow, OptionsFlow=OptionsFlow),
            "homeassistant.helpers": fake_module("homeassistant.helpers", entity_registry=registry, selector=selectors),
        }
        name = "custom_components.apartment_comfort.config_flow"
        with patch.dict(sys.modules, modules):
            sys.modules.pop(name, None)
            self.module = importlib.import_module(name)
        self.flow = self.module.ComfortOptionsFlow()
        self.original = DEFAULTS | {"temperature_entity": "sensor.room", "period_entity": "input_select.period",
                                    "radiator_entity": "climate.rad", "radiator_fan_entity": "fan.old"}
        self.flow.config_entry = SimpleNamespace(entry_id="zone", data=self.original, options={})
        self.flow.hass = SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda domain: [self.flow.config_entry]))

    async def test_menu_exposes_all_sections(self):
        result = await self.flow.async_step_init()
        self.assertEqual(result["type"], "menu")
        self.assertEqual(result["menu_options"], ["user", "devices", "periods", "policy"])

    async def test_device_section_saves_relay_and_preserves_other_sections(self):
        result = await self.flow.async_step_devices({"radiator_fan_entity": "light.ventiliator"})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"]["radiator_fan_entity"], "light.ventiliator")
        self.assertNotIn("radiator_entity", result["data"])
        self.assertEqual(result["data"]["temperature_entity"], "sensor.room")
        self.assertEqual(result["data"]["night_temperature"], DEFAULTS["night_temperature"])

    async def test_period_section_preserves_existing_options(self):
        self.flow.config_entry.options = self.original | {"co2_high": 1200}
        periods = {k: v for k, v in DEFAULTS.items() if k.endswith("_state") or k.endswith("_temperature") and k != "window_min_temperature"}
        result = await self.flow.async_step_periods(periods | {"night_temperature": 19})
        self.assertEqual(result["data"]["co2_high"], 1200)
        self.assertEqual(result["data"]["night_temperature"], 19)

    async def test_invalid_humidity_threshold_stays_in_form(self):
        result = await self.flow.async_step_policy(DEFAULTS | {"humidity_clear": 80})
        self.assertEqual(result["errors"], {"base": "invalid_thresholds"})
        self.assertEqual(result["type"], "form")

    async def test_window_heating_option_is_available_and_persisted(self):
        schema = self.module.schema_for("policy", DEFAULTS)
        self.assertIn("heat_with_window_open", schema)
        self.assertFalse(DEFAULTS["heat_with_window_open"])
        result = await self.flow.async_step_policy(DEFAULTS | {"heat_with_window_open": True})
        self.assertTrue(result["data"]["heat_with_window_open"])
        self.assertEqual(result["data"]["radiator_entity"], "climate.rad")

    async def test_duplicate_device_stays_in_form(self):
        result = await self.flow.async_step_devices({"radiator_entity": "climate.same", "floor_entity": "climate.same"})
        self.assertEqual(result["errors"], {"base": "duplicate_devices"})

    async def test_initial_setup_still_advances_through_wizard(self):
        flow = self.module.ComfortConfigFlow()
        result = await flow.async_step_user({"name": "Zone", "temperature_entity": "sensor.room", "period_entity": "input_select.period"})
        self.assertEqual(result["step_id"], "devices")

    def test_relay_domains_in_all_three_selectors(self):
        sensors = self.module.schema_for("user", DEFAULTS)
        devices = self.module.schema_for("devices", DEFAULTS)
        for schema, field in ((sensors, "shower_exhaust_entity"), (sensors, "kitchen_exhaust_entity"), (devices, "radiator_fan_entity")):
            self.assertEqual(set(schema[field]["domain"]), {"fan", "switch", "light"})


for _name, _test in list(vars(OptionsTests).items()):
    if _name.startswith("test_") and __import__("inspect").iscoroutinefunction(_test):
        setattr(OptionsTests, _name, immediate_coroutine_test(_test))
