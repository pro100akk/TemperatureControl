"""UI-only configuration and reconfiguration."""

try:  # HA renamed its validation dependency; support both generations.
    import probatio as vol
except ImportError:
    import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er, selector

from .const import ACTUATOR_FIELDS, DEFAULTS, DOMAIN, PERIODS, SENSOR_FIELDS, EXHAUST_FIELDS
from .ventilation import speed_curve

POLICY_RANGES = {
    "kitchen_airflow_percent": (1, 100, 1),
    "kitchen_night_airflow_percent": (0, 100, 1),
    "window_exchange_per_hour": (0, 3, 0.05),
    "exhaust_exchange_per_hour": (0, 5, 0.05),
    "forecast_minutes": (5, 60, 5), "pid_kp": (0, 5, 0.01),
    "pid_ki": (0, 0.5, 0.001), "pid_kd": (0, 5, 0.01),
    "pid_max_offset": (0.5, 5, 0.1),
    "deadband": (0.1, 2, 0.1), "floor_deficit": (0.5, 5, 0.1),
    "floor_delay_minutes": (5, 180, 1), "floor_max_minutes": (5, 180, 1),
    "ac_min_off_minutes": (5, 30, 1), "night_fan_percent": (0, 100, 1),
    "max_fan_percent": (0, 100, 1), "stale_minutes": (5, 1440, 1),
    "co2_high": (600, 3000, 10), "co2_clear": (400, 2000, 10),
    "humidity_high": (40, 90, 1), "humidity_clear": (30, 85, 1),
    "shower_high": (45, 95, 1), "shower_clear": (35, 90, 1),
    "cooling_margin": (0.5, 5, 0.1), "window_min_temperature": (12, 24, 0.5),
}


def schema_for(step, values):
    fields = {}
    if step == "user":
        fields[vol.Required("name", default=values["name"])] = selector.TextSelector()
        entities = SENSOR_FIELDS
    elif step == "devices":
        entities = ACTUATOR_FIELDS
    else:
        entities = {}
    for key, domains in entities.items():
        if key in ("temperature_entity", "period_entity"):
            marker = vol.Required(key, description={"suggested_value": values.get(key)})
        else:
            marker = vol.Optional(key, description={"suggested_value": values.get(key)})
        fields[marker] = selector.EntitySelector({"domain": domains})
    if step == "periods":
        for period in PERIODS:
            for suffix in ("state", "temperature"):
                key = f"{period}_{suffix}"
                fields[vol.Required(key, default=values[key])] = (
                    selector.TextSelector() if suffix == "state" else
                    selector.NumberSelector({"min": 16, "max": 28, "step": 0.1, "mode": "box", "unit_of_measurement": "°C"})
                )
    if step == "policy":
        fields[vol.Required("kitchen_speed_curve", default=values["kitchen_speed_curve"])] = selector.TextSelector()
        fields[vol.Required("fan_temperature_source", default=values["fan_temperature_source"])] = selector.SelectSelector(
            {"options": ["surface", "pipe"], "mode": "dropdown", "translation_key": "fan_temperature_source"})
        fields[vol.Required("controller", default=values["controller"])] = selector.SelectSelector(
            {"options": ["hysteresis", "pi", "pid"], "mode": "dropdown"})
        for key in ("control_enabled", "floor_enabled", "learning_enabled",
                    "heat_with_window_open", "radiator_compensation",
                    "kitchen_control_enabled", "shower_control_enabled", "kitchen_night_allowed"):
            fields[vol.Required(key, default=values[key])] = selector.BooleanSelector()
        for key, (low, high, increment) in POLICY_RANGES.items():
            fields[vol.Required(key, default=values[key])] = selector.NumberSelector(
                {"min": low, "max": high, "step": increment, "mode": "box"}
            )
    return vol.Schema(fields)


class FlowMixin:
    """Share the same editable wizard between initial setup and options."""

    _values = None

    async def _step(self, step, user_input):
        if self._values is None:
            existing = (dict(self.config_entry.options or self.config_entry.data)
                        if isinstance(self, config_entries.OptionsFlow) else {})
            self._values = DEFAULTS | existing
        errors = {}
        if user_input is not None:
            if step in ("user", "devices"):
                keys = SENSOR_FIELDS if step == "user" else ACTUATOR_FIELDS
                for key in keys:
                    self._values.pop(key, None)
            self._values.update(user_input)
            if step == "periods":
                states = [user_input[f"{p}_state"].strip() for p in PERIODS]
                if not all(states) or len(set(states)) != len(PERIODS):
                    errors["base"] = "unique_periods"
                else:
                    self._values.update({f"{p}_state": value for p, value in zip(PERIODS, states)})
            if step in ("user", "devices"):
                ids = [self._values[k] for k in (*ACTUATOR_FIELDS, *EXHAUST_FIELDS) if self._values.get(k)]
                if len(ids) != len(set(ids)):
                    errors["base"] = "duplicate_devices"
                registry = er.async_get(self.hass)
                if any((registered := registry.async_get(e)) and registered.platform == DOMAIN for e in ids):
                    errors["base"] = "self_reference"
                for entry in self.hass.config_entries.async_entries(DOMAIN):
                    if isinstance(self, config_entries.OptionsFlow) and entry.entry_id == self.config_entry.entry_id:
                        continue
                    other = dict(entry.options or entry.data)
                    if set(ids) & {other.get(k) for k in (*ACTUATOR_FIELDS, *EXHAUST_FIELDS)}:
                        errors["base"] = "device_in_use"
            if step == "policy":
                try:
                    speed_curve(self._values["kitchen_speed_curve"])
                except (ValueError, TypeError):
                    errors["base"] = "invalid_speed_curve"
                if self._values["fan_temperature_source"] not in ("surface", "pipe"):
                    errors["base"] = "invalid_fan_source"
                if self._values["controller"] not in ("hysteresis", "pi", "pid"):
                    errors["base"] = "invalid_controller"
                for high, clear in (("co2_high", "co2_clear"), ("humidity_high", "humidity_clear"), ("shower_high", "shower_clear")):
                    if user_input[clear] >= user_input[high]:
                        errors["base"] = "invalid_thresholds"
            if not errors:
                if isinstance(self, config_entries.OptionsFlow) or step == "policy":
                    return self.async_create_entry(title=self._values["name"], data=self._values)
                next_step = {"user": "devices", "devices": "periods", "periods": "policy"}[step]
                return await self._step(next_step, None)
        return self.async_show_form(step_id=step, data_schema=schema_for(step, self._values), errors=errors)

    async def async_step_user(self, user_input=None):
        return await self._step("user", user_input)

    async def async_step_devices(self, user_input=None):
        return await self._step("devices", user_input)

    async def async_step_periods(self, user_input=None):
        return await self._step("periods", user_input)

    async def async_step_policy(self, user_input=None):
        return await self._step("policy", user_input)


class ComfortConfigFlow(FlowMixin, config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ComfortOptionsFlow()


class ComfortOptionsFlow(FlowMixin, config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        return self.async_show_menu(
            step_id="init", menu_options=["user", "devices", "periods", "policy"]
        )
