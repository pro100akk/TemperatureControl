"""Replay isolated policy decisions from versioned checkpoints, without HA."""

from dataclasses import asdict

from .engine import Engine, Inputs

ENGINE_FIELDS = ("heating", "cooling", "deficit_since", "floor_since", "floor_locked", "air_flags")
PID_FIELDS = ("integral", "previous", "derivative", "output", "context")


def checkpoint(engine, now, predicted):
    return {
        "version": 1, "now": now, "predicted": predicted,
        "config": {k: v for k, v in engine.config.items() if k not in ("name", "notification_service")},
        "engine": {k: dict(v) if isinstance(v, dict) else v
                   for k in (*ENGINE_FIELDS, "fan_ramp") for v in (getattr(engine, k),)},
        "pid": {k: getattr(engine.pid, k) for k in PID_FIELDS},
    }


def replay_cycle(data, config_overrides=None):
    """Evaluate a recorded input with its pre-decision state; never actuate."""
    saved = data["replay"]
    if saved["version"] != 1:
        raise ValueError("Unsupported replay checkpoint version")
    engine = Engine(saved["config"] | (config_overrides or {}))
    if saved["engine"].get("fan_ramp") is not None:
        engine.fan_ramp = dict(saved["engine"]["fan_ramp"])
    for key in ENGINE_FIELDS:
        value = saved["engine"][key]
        setattr(engine, key, dict(value) if isinstance(value, dict) else value)
    for key in PID_FIELDS:
        value = saved["pid"][key]
        if key in ("previous", "context") and value is not None:
            value = tuple(value)
        setattr(engine.pid, key, value)
    result = asdict(engine.evaluate(Inputs(**data["inputs"]), saved["now"], data["mode"],
                                   data.get("override"), predicted_temperature=saved["predicted"]))
    differences = {key: {"recorded": data["decision"].get(key), "replayed": value}
                   for key, value in result.items() if data["decision"].get(key) != value}
    return {"decision": result, "differences": differences}
