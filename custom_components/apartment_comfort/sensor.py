"""Explainable planned state and ventilation recommendations."""

from dataclasses import asdict

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass

from .entity import ComfortEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([ComfortSensor(entry.runtime_data, key)
                        for key in ("status", "recommendation", "target", "predicted_temperature", "demand_percent")])


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
        if self.key == "status":
            if self.coordinator.manual_hold:
                return "manual_hold"
            if not self.coordinator.config["control_enabled"]:
                return "observation"
        return getattr(d, self.key)

    @property
    def extra_state_attributes(self):
        c = self.coordinator
        if self.key == "recommendation":
            return {"reasons": c.decision.ventilation_reasons,
                    "exhaust_recommendations": c.decision.exhaust_recommendations}
        if self.key == "status":
            return {"decision": asdict(c.decision), "inputs": asdict(c.inputs),
                    "thermal_model": c.engine.model.rates,
                    "issues": c.issues, "floor_locked": c.engine.floor_locked,
                    "manual_hold": c.manual_hold}
        if self.key == "predicted_temperature":
            return {"horizon_minutes": c.config["forecast_minutes"],
                    "regime": c.engine.model.current,
                    "assumption": "unchanged_operating_state_and_outdoor_conditions",
                    "coefficients": c.engine.model.rates}
        return {"period": c.inputs.period, "temporary": c.override is not None}
