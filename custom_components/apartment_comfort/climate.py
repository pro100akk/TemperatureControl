"""Virtual thermostat with targets driven by an external day-period entity."""

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACAction, HVACMode
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.exceptions import ServiceValidationError

from .entity import ComfortEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([ComfortClimate(entry.runtime_data)])


class ComfortClimate(ComfortEntity, ClimateEntity):
    _enable_turn_on_off_backwards_compatibility = False
    _attr_translation_key = "thermostat"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.1
    _attr_min_temp = 16
    _attr_max_temp = 28
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE
    _attr_preset_modes = ["schedule", "temporary"]

    def __init__(self, coordinator):
        super().__init__(coordinator, "thermostat")
        self._attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL]

    @property
    def current_temperature(self):
        return self.coordinator.inputs.temperature

    @property
    def current_humidity(self):
        return self.coordinator.inputs.room_humidity

    @property
    def target_temperature(self):
        return self.coordinator.decision.target if self.coordinator.decision else None

    @property
    def hvac_mode(self):
        return HVACMode(self.coordinator.mode)

    @property
    def hvac_action(self):
        c = self.coordinator
        if c.mode == "off":
            return HVACAction.OFF
        if not c.config["control_enabled"] or c.manual_hold:
            return HVACAction.IDLE
        actions = []
        for entity_id in c.owned:
            state = c.hass.states.get(entity_id)
            if state:
                actions.append(state.attributes.get("hvac_action"))
        if "cooling" in actions:
            return HVACAction.COOLING
        if "heating" in actions:
            return HVACAction.HEATING
        return HVACAction.IDLE

    @property
    def preset_mode(self):
        return "temporary" if self.coordinator.override is not None else "schedule"

    @property
    def extra_state_attributes(self):
        c = self.coordinator
        return {"control_enabled": c.config["control_enabled"], "manual_hold": c.manual_hold,
                "period": c.inputs.period, "planned_action": c.decision.status if c.decision else None,
                "reason": c.decision.reason if c.decision else None,
                "issues": c.issues, "owned_devices": sorted(c.owned)}

    async def async_set_hvac_mode(self, hvac_mode):
        if hvac_mode not in self.hvac_modes:
            raise ServiceValidationError("Unsupported HVAC mode")
        await self.coordinator.async_set_mode(hvac_mode)

    async def async_set_temperature(self, **kwargs):
        value = kwargs.get(ATTR_TEMPERATURE)
        if value is None or not self.min_temp <= value <= self.max_temp:
            raise ServiceValidationError("Temperature must be between 16 and 28 °C")
        await self.coordinator.async_set_target(float(value))

    async def async_set_preset_mode(self, preset_mode):
        if preset_mode == "schedule":
            await self.coordinator.async_resume_schedule()
        elif preset_mode == "temporary" and self.target_temperature is not None:
            await self.coordinator.async_set_target(self.target_temperature)
        else:
            raise ServiceValidationError("Choose schedule or set a temporary temperature")
