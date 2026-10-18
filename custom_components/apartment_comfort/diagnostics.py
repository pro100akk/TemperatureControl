"""Download anonymous control diagnostics, without entity identifiers."""

from dataclasses import asdict


async def async_get_config_entry_diagnostics(hass, entry):
    c = entry.runtime_data
    return {
        "version": "0.1.2", "mode": c.mode,
        "control_enabled": c.config["control_enabled"],
        "manual_hold": c.manual_hold,
        "controller": c.config["controller"],
        "thermal_model": c.engine.model.rates,
        "forecast_status": c.forecast_status, "fan_status": c.fan_status,
        "radiator_command": c.radiator_command,
        "inputs": asdict(c.inputs),
        "heat_with_window_open": c.config["heat_with_window_open"],
        "decision": asdict(c.decision) if c.decision else None,
        "configured_roles": sorted(k for k in c.config if k.endswith("_entity")),
        "issue_count": len(c.issues),
    }
