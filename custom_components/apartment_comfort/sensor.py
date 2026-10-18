"""Explainable planned state and ventilation recommendations."""

from dataclasses import asdict

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass

from .entity import ComfortEntity

FORECAST_STATUSES = ["disabled", "room_temperature_unavailable", "outdoor_temperature_unavailable",
                     "operating_state_unknown", "manual_hold", "window_open_or_unknown",
                     "disturbance_active_or_unknown", "learning", "model_error_high",
                     "outdoor_conditions_changed", "ready"]
FAN_STATUSES = ["not_configured", "observation", "manual_hold", "no_heat_demand",
                "surface_temperature_unavailable", "radiator_not_hot", "speed_limit_zero",
                "pipe_temperature_unavailable", "radiator_not_heating",
                "waiting_or_blocked", "on", "requested"]


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([ComfortSensor(entry.runtime_data, key)
                        for key in ("status", "recommendation", "target", "predicted_temperature",
                                    "demand_percent", "forecast_status", "fan_status")])


class ComfortSensor(ComfortEntity, SensorEntity):
    def __init__(self, coordinator, key):
        super().__init__(coordinator, key)
        self.key = key
        self._attr_translation_key = key
        if key in ("target", "predicted_temperature"):
            self._attr_native_unit_of_measurement = "°C"
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
        elif key == "demand_percent":
            self._attr_native_unit_of_measurement = "%"
            self._attr_icon = "mdi:gauge"
        elif key in ("forecast_status", "fan_status"):
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = FORECAST_STATUSES if key == "forecast_status" else FAN_STATUSES
            self._attr_icon = "mdi:chart-line" if key == "forecast_status" else "mdi:fan"
        elif key == "recommendation":
            self._attr_icon = "mdi:window-open-variant"
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = ["none", "check_window_sensor", "close_window",
                                  "close_window_cold_air_quality_unresolved", "keep_window_open",
                                  "check_air_quality_before_closing", "open_window_briefly",
                                  "open_window", "check_ventilation"]
        else:
            self._attr_icon = "mdi:thermostat-auto"
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = ["observation", "manual_hold", "idle", "off", "blocked",
                                  "ventilating", "heating", "backup_heating", "cooling"]

    @property
    def native_value(self):
        d = self.coordinator.decision
        if not d:
            return None
        if self.key in ("forecast_status", "fan_status"):
            return getattr(self.coordinator, self.key)
        if self.key == "status":
            if self.coordinator.manual_hold:
                return "manual_hold"
            if not self.coordinator.config["control_enabled"]:
                return "observation"
        return getattr(d, self.key)

    @property
    def extra_state_attributes(self):
        c = self.coordinator
        if not c.decision:
            return {}
        if self.key == "fan_status":
            return {"requested_percent": c.decision.fan_percent,
                    "temperature_source": c.config["fan_temperature_source"],
                    "pipe_temperature": c.inputs.pipe_temperature,
                    "radiator_heating": c.inputs.radiator_heating,
                    "reason": c.decision.fan_reason,
                    "surface_temperature": c.inputs.radiator_temperature,
                    "required_temperature_above": (c.inputs.temperature + 2
                                                   if c.inputs.temperature is not None else None)}
        if self.key == "recommendation":
            return {"reasons": c.decision.ventilation_reasons,
                    "exhaust_recommendations": c.decision.exhaust_recommendations}
        if self.key == "status":
            return {"decision": asdict(c.decision), "inputs": asdict(c.inputs),
                    "thermal_model": c.engine.model.rates,
                    "issues": c.issues, "floor_locked": c.engine.floor_locked,
                    "manual_hold": c.manual_hold,
                    "radiator_command": c.radiator_command,
                    "forecast_status": c.forecast_status, "fan_status": c.fan_status}
        if self.key in ("predicted_temperature", "forecast_status"):
            return {"horizon_minutes": c.config["forecast_minutes"],
                    "status": c.forecast_status,
                    "samples": c.engine.model.rates.get(c.engine.model.current, {}).get("count", 0),
                    "required_samples": 12,
                    "regime": c.engine.model.current,
                    "assumption": "unchanged_operating_state_and_outdoor_conditions",
                    "coefficients": c.engine.model.rates}
        return {"period": c.inputs.period, "temporary": c.override is not None}
