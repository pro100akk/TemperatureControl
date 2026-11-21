"""Download anonymous control diagnostics, without entity identifiers."""

from dataclasses import asdict
from .version import VERSION
from .const import SENSOR_FIELDS, ACTUATOR_FIELDS, entity_ids


async def async_get_config_entry_diagnostics(hass, entry):
    c = entry.runtime_data
    # Replace configured identifiers with stable role-local labels, including
    # identifiers embedded in learned multi-fan regime names.
    aliases = {}
    for role in (*SENSOR_FIELDS, *ACTUATOR_FIELDS):
        for index, entity in enumerate(entity_ids(c.config, (role,)), 1):
            aliases[entity] = f"{role}[{index}]"

    def anonymize(value):
        for entity in sorted(aliases, key=len, reverse=True):
            value = value.replace(entity, aliases[entity])
        return value

    return {
        "version": VERSION, "mode": c.mode,
        "control_enabled": c.config["control_enabled"],
        "manual_hold": c.manual_hold,
        "controller": c.config["controller"],
        "thermal_model": {anonymize(key): value for key, value in c.engine.model.rates.items()},
        "forecast_status": c.forecast_status, "fan_status": c.fan_status,
        "radiator_command": c.radiator_command,
        "inputs": asdict(c.inputs),
        "heat_with_window_open": c.config["heat_with_window_open"],
        "decision": asdict(c.decision) if c.decision else None,
        "configured_roles": sorted(k for k in c.config if k.endswith("_entity")),
        "issue_count": len(c.issues),
        "issues": [anonymize(issue) for issue in c.issues],
        "radiator_fans": c.fan_details,
    }
