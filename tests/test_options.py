"""Exercise options routing and persistence with HA form boundary doubles."""

import importlib
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

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

    def async_abort(self, **kwargs):
        return {"type": "abort", **kwargs}


class OptionsFlow(Flow):
    pass


def fake_module(name, **attrs):
    module = ModuleType(name)
    module.__dict__.update(attrs)
    return module


class OptionsTests(unittest.TestCase):
    def setUp(self):
        self.form_defaults = {}

        def required(key, **kwargs):
            if "default" in kwargs:
                self.form_defaults[key] = kwargs["default"]
            return key

        selectors = SimpleNamespace(
            SelectSelector=lambda config: config,
            DateTimeSelector=lambda: "datetime",
            TextSelector=lambda: "text", NumberSelector=lambda config: config,
            BooleanSelector=lambda: "bool", EntitySelector=lambda config: config)
        registry = SimpleNamespace(async_get=lambda hass: SimpleNamespace(async_get=lambda entity: None))
        modules = {
            "probatio": fake_module("probatio", Required=required,
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
        self.flow.hass = SimpleNamespace(config=SimpleNamespace(time_zone="Asia/Vladivostok"),
                                        config_entries=SimpleNamespace(async_entries=lambda domain: [self.flow.config_entry]))

    async def test_multiple_fans_and_conflicts(self):
        fans = ["fan.one", "switch.two", "light.three"]
        result = await self.flow.async_step_devices({"radiator_fan_entity": fans})
        self.assertEqual(result["data"]["radiator_fan_entity"], fans)
        self.assertTrue(self.module.schema_for("devices", result["data"])["radiator_fan_entity"]["multiple"])
        result = await self.flow.async_step_user({"kitchen_exhaust_entity": "switch.two"})
        self.assertEqual(result["errors"], {"base": "duplicate_devices"})

    async def test_multiple_fans_cannot_overlap_other_entries(self):
        other = SimpleNamespace(entry_id="other", options={}, data={"radiator_fan_entity": ["fan.one", "fan.two"]})
        self.flow.hass.config_entries.async_entries = lambda domain: [self.flow.config_entry, other]
        result = await self.flow.async_step_devices({"radiator_fan_entity": ["fan.two", "fan.three"]})
        self.assertEqual(result["errors"], {"base": "device_in_use"})

    async def test_menu_exposes_all_sections(self):
        result = await self.flow.async_step_init()
        self.assertEqual(result["type"], "menu")
        self.assertEqual(result["menu_options"], ["user", "devices", "periods", "policy", "notifications", "debug", "export"])

    async def test_debug_can_be_enabled_and_disabled_without_losing_devices(self):
        for enabled in (True, False):
            result = await self.flow.async_step_debug({"debug_enabled": enabled,
                                                       "debug_max_file_mb": 2, "debug_backup_count": 1})
            self.assertEqual(result["data"]["debug_enabled"], enabled)
            self.assertEqual(result["data"]["radiator_entity"], "climate.rad")

    async def test_export_uses_ha_timezone_and_does_not_save_or_reload_options(self):
        self.flow.config_entry.runtime_data = SimpleNamespace(stopping=False)
        manager = SimpleNamespace(create_link=Mock(return_value="/api/export?authSig=test"))
        with patch.dict(sys.modules, {"custom_components.apartment_comfort.export":
                        fake_module("custom_components.apartment_comfort.export", setup_downloads=lambda hass: manager)}):
            result = await self.flow.async_step_export({"start": "2026-11-22T10:00:00",
                                                      "end": "2026-11-22T11:00:00", "anonymize": False})
        self.assertEqual(result["step_id"], "export_ready")
        args = manager.create_link.call_args.args
        self.assertEqual(args[1].isoformat(), "2026-11-22T00:00:00+00:00")
        self.assertFalse(args[3])
        self.assertEqual(self.flow.config_entry.options, {})
        self.assertEqual((await self.flow.async_step_export_ready({}))["type"], "abort")

    async def test_export_rejects_reversed_dates_and_unloaded_entry(self):
        result = await self.flow.async_step_export({"start": "2026-11-23T10:00:00",
                                                  "end": "2026-11-22T11:00:00", "anonymize": True})
        self.assertEqual(result["errors"]["base"], "invalid_export_range")
        result = await self.flow.async_step_export({"start": "2026-11-22T10:00:00",
                                                  "end": "2026-11-22T11:00:00", "anonymize": True})
        self.assertEqual(result["errors"]["base"], "export_not_loaded")

    async def test_export_datetime_defaults_survive_frontend_time_edit(self):
        self.flow.config_entry.runtime_data = SimpleNamespace(stopping=False)
        result = await self.flow.async_step_export()
        self.assertEqual(result["step_id"], "export")
        # ha-selector-datetime renders value.split(" ") then joins the date
        # and edited time with a space. This reproduces the user's UI action.
        start_parts = self.form_defaults["start"].split(" ")
        end_parts = self.form_defaults["end"].split(" ")
        self.assertEqual(len(start_parts), 2)
        self.assertEqual(len(end_parts), 2)
        self.assertNotIn("T", start_parts[0])
        manager = SimpleNamespace(create_link=Mock(return_value="/api/export?authSig=test"))
        with patch.dict(sys.modules, {"custom_components.apartment_comfort.export":
                        fake_module("custom_components.apartment_comfort.export", setup_downloads=lambda hass: manager)}):
            result = await self.flow.async_step_export({"start": f"{start_parts[0]} 01:00:00",
                                                      "end": f"{end_parts[0]} 02:30:00", "anonymize": True})
        self.assertEqual(result["step_id"], "export_ready")
        self.assertEqual(manager.create_link.call_args.args[1].hour, 15)

    async def test_export_repeated_form_and_broken_old_value_use_selector_format(self):
        result = await self.flow.async_step_export({"start": "2026-11-23T10:00:00",
                                                  "end": "2026-11-22T11:00:00", "anonymize": True})
        self.assertEqual(result["errors"]["base"], "invalid_export_range")
        self.assertEqual(self.form_defaults["start"], "2026-11-23 10:00:00")
        self.assertEqual(self.form_defaults["end"], "2026-11-22 11:00:00")
        result = await self.flow.async_step_export({"start": "2026-11-22T22:41:36 01:00:00",
                                                  "end": "2026-11-23 11:00:00", "anonymize": True})
        self.assertEqual(result["errors"]["base"], "invalid_export_range")
        self.assertEqual(len(self.form_defaults["start"].split(" ")), 2)
        self.assertNotIn("T", self.form_defaults["start"])

    async def test_download_link_is_escaped_for_html_attribute(self):
        self.flow._export_link = '/api/export?authSig=test&part="value"'
        result = await self.flow.async_step_export_ready()
        self.assertEqual(result["description_placeholders"]["download_open"],
                         '<a href="/api/export?authSig=test&amp;part=&quot;value&quot;" target="_blank">')
        self.assertEqual(result["description_placeholders"]["download_close"], "</a>")

    async def test_notification_settings_are_separate_and_validate_destination(self):
        result = await self.flow.async_step_notifications({"notifications_enabled": True, "notification_service": "notify.all"})
        self.assertEqual(result["errors"], {"base": "invalid_notification_service"})
        result = await self.flow.async_step_notifications({"notifications_enabled": True, "notification_service": "notify.mobile_app_phone"})
        self.assertTrue(result["data"]["notifications_enabled"])
        self.assertEqual(result["data"]["radiator_entity"], "climate.rad")

    async def test_surface_thresholds_must_have_hysteresis(self):
        result = await self.flow.async_step_policy(DEFAULTS | {"radiator_cold_delta": 3})
        self.assertEqual(result["errors"], {"base": "invalid_thresholds"})

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

    async def test_legacy_options_gain_pre_wake_defaults(self):
        self.flow.config_entry.options = {k: v for k, v in self.original.items() if not k.startswith("pre_wake_")}
        result = await self.flow.async_step_periods()
        self.assertIn("pre_wake_state", result["data_schema"])
        self.assertEqual(self.flow._values["pre_wake_temperature"], 22)
        result = await self.flow.async_step_periods(DEFAULTS | {"pre_wake_state": "До будильника", "pre_wake_temperature": 23})
        self.assertEqual(result["data"]["pre_wake_temperature"], 23)
        self.assertEqual(result["data"]["pre_wake_state"], "До будильника")

    async def test_pre_wake_state_must_be_unique_and_nonempty(self):
        for state in ("night", "  "):
            result = await self.flow.async_step_periods(DEFAULTS | {"pre_wake_state": state})
            self.assertEqual(result["errors"], {"base": "unique_periods"})

    async def test_invalid_humidity_threshold_stays_in_form(self):
        result = await self.flow.async_step_policy(DEFAULTS | {"humidity_clear": 80})
        self.assertEqual(result["errors"], {"base": "invalid_thresholds"})
        self.assertEqual(result["type"], "form")

    async def test_invalid_dimmer_curve_stays_in_form(self):
        result = await self.flow.async_step_policy(DEFAULTS | {"kitchen_speed_curve": "0:0,50:80,100:50"})
        self.assertEqual(result["errors"], {"base": "invalid_speed_curve"})

    async def test_exhaust_cannot_also_be_radiator_fan(self):
        result = await self.flow.async_step_user({
            "temperature_entity": "sensor.room", "period_entity": "input_select.period",
            "kitchen_exhaust_entity": "fan.old"})
        self.assertEqual(result["errors"], {"base": "duplicate_devices"})

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
        flow.hass = self.flow.hass
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
