"""Deterministic policy, independent of HA. All timestamps are monotonic seconds."""

from dataclasses import dataclass, field
from math import exp, isfinite

from .const import DEFAULTS, PERIODS
from .adaptive import PID, ThermalModel


def absolute_humidity(temperature: float, relative_humidity: float) -> float:
    """Return water concentration in g/m3, for ventilation comparison only."""
    vapor_pressure = 6.112 * exp(17.67 * temperature / (temperature + 243.5))
    return 216.7 * vapor_pressure * relative_humidity / 100 / (273.15 + temperature)


@dataclass
class Inputs:
    temperature: float | None = None
    period: str | None = None
    window: bool | None = False
    heating_available: bool | None = True
    presence: bool | None = None
    outdoor_temperature: float | None = None
    pipe_temperature: float | None = None
    radiator_temperature: float | None = None
    radiator_heating: bool | None = None
    room_humidity: float | None = None
    shower_humidity: float | None = None
    outdoor_humidity: float | None = None
    co2: float | None = None
    cooking: bool | None = None
    computer_power: float | None = None
    shower_exhaust: bool | None = None
    kitchen_exhaust: bool | None = None


@dataclass
class Decision:
    target: float | None
    status: str
    reason: str
    demand_percent: float = 0.0
    actuator_target: float | None = None
    predicted_temperature: float | None = None
    radiator: bool = False
    fan_percent: int = 0
    fan_reason: str = "no_heat_demand"
    floor: bool = False
    cooling: bool = False
    recommendation: str = "none"
    ventilation_reasons: list[str] = field(default_factory=list)
    exhaust_recommendations: list[str] = field(default_factory=list)


class Engine:
    def __init__(self, config: dict):
        self.config = DEFAULTS | config
        self.pid = PID()
        self.model = ThermalModel()
        self.heating = False
        self.cooling = False
        self.deficit_since: float | None = None
        self.floor_since: float | None = None
        self.floor_locked = False
        self.air_flags = {"co2": False, "humidity": False, "shower": False}

    def target(self, period: str | None, override: float | None) -> float | None:
        if override is not None:
            return override
        for key in PERIODS:
            if period == self.config[f"{key}_state"]:
                return float(self.config[f"{key}_temperature"])
        return None

    def _air(self, data: Inputs, decision: Decision) -> None:
        c = self.config
        for key, value, high, clear in (
            ("co2", data.co2, c["co2_high"], c["co2_clear"]),
            ("humidity", data.room_humidity, c["humidity_high"], c["humidity_clear"]),
            ("shower", data.shower_humidity, c["shower_high"], c["shower_clear"]),
        ):
            if value is not None:
                if value >= high:
                    self.air_flags[key] = True
                elif value <= clear:
                    self.air_flags[key] = False
        reasons = decision.ventilation_reasons
        if self.air_flags["co2"]:
            reasons.append("co2")
        if self.air_flags["humidity"]:
            values = (data.temperature, data.room_humidity,
                      data.outdoor_temperature, data.outdoor_humidity)
            if all(v is not None for v in values):
                inside = absolute_humidity(data.temperature, data.room_humidity)
                outside = absolute_humidity(data.outdoor_temperature, data.outdoor_humidity)
                if inside > outside + 0.5:
                    reasons.append("humidity")
                else:
                    decision.exhaust_recommendations.append("humidity_outdoors_not_drier")
            else:
                decision.exhaust_recommendations.append("humidity_check_outdoor_air")
        if self.air_flags["shower"]:
            decision.exhaust_recommendations.append("shower_exhaust")
        if data.cooking:
            reasons.append("cooking")
            decision.exhaust_recommendations.append("kitchen_exhaust")
        if (data.temperature is not None and decision.target is not None
                and data.outdoor_temperature is not None
                and data.temperature > decision.target + c["cooling_margin"]
                and data.outdoor_temperature < data.temperature - c["cooling_margin"]):
            reasons.append("free_cooling")
        if data.window is None:
            decision.recommendation = "check_window_sensor"
        elif data.window:
            if data.temperature is not None and data.temperature <= c["window_min_temperature"]:
                decision.recommendation = "close_window_cold_air_quality_unresolved" if reasons else "close_window"
            elif reasons:
                decision.recommendation = "keep_window_open"
            elif (any(value is None for value in (data.co2, data.cooking))
                  or "humidity_check_outdoor_air" in decision.exhaust_recommendations
                  or (c.get("room_humidity_entity") and data.room_humidity is None)):
                # Unknown values are not proof that the reason for airing disappeared.
                decision.recommendation = "check_air_quality_before_closing"
            else:
                decision.recommendation = "close_window"
        elif reasons:
            decision.recommendation = "open_window_briefly" if (
                data.temperature is not None and data.temperature <= c["window_min_temperature"]
            ) else "open_window"
        elif decision.exhaust_recommendations:
            decision.recommendation = "check_ventilation"

    def evaluate(self, data: Inputs, now: float, mode: str = "heat",
                 override: float | None = None) -> Decision:
        c = self.config
        target = self.target(data.period, override)
        d = Decision(target, "idle", "within_band")
        self._air(data, d)
        if mode == "off":
            d.status, d.reason = "off", "disabled_by_user"
        elif data.temperature is None or not isfinite(data.temperature):
            d.status, d.reason = "blocked", "temperature_unavailable"
        elif target is None:
            d.status, d.reason = "blocked", "unknown_day_period"
        elif data.window is None:
            d.status, d.reason = "blocked", "window_state_unknown"
        elif data.window and not (mode == "heat" and c["heat_with_window_open"]):
            d.status, d.reason = "ventilating", "window_open"
        else:
            error = target - data.temperature
            band = c["deadband"]
            if error >= band:
                self.heating = True
            elif error <= 0:
                self.heating = False
            if error <= -band:
                self.cooling = True
            elif error >= 0:
                self.cooling = False
            if c["controller"] in ("pi", "pid"):
                demand = self.pid.update(data.temperature, target, mode, now, c)
                self.heating = mode == "heat" and demand > 0 and error > -band
                self.cooling = mode == "cool" and demand > 0 and error < band
                d.demand_percent = round(demand * 100, 1)
                direction = 1 if mode == "heat" else -1
                d.actuator_target = max(target - c["pid_max_offset"], min(
                    target + c["pid_max_offset"],
                    data.temperature + direction * c["pid_max_offset"] * demand))
            else:
                self.pid.reset()
                d.actuator_target = target
                d.demand_percent = 100.0 if (self.heating if mode == "heat" else self.cooling) else 0.0
            if mode == "heat" and self.heating:
                d.radiator = bool(c.get("radiator_entity")) and data.heating_available is not False
                if d.radiator:
                    d.status, d.reason = "heating", "radiator_priority"
                    pipe_mode = c["fan_temperature_source"] == "pipe"
                    fan_temperature = data.pipe_temperature if pipe_mode else data.radiator_temperature
                    if not c.get("radiator_fan_entity"):
                        d.fan_reason = "not_configured"
                    elif pipe_mode and data.radiator_heating is not True:
                        d.fan_reason = "radiator_not_heating"
                    elif fan_temperature is None:
                        d.fan_reason = ("pipe_temperature_unavailable" if pipe_mode
                                        else "surface_temperature_unavailable")
                    else:
                        d.fan_reason = "radiator_not_hot"
                    # A hot supply pipe alone is not evidence of an open valve.
                    if (c.get("radiator_fan_entity") and fan_temperature is not None
                            and (not pipe_mode or data.radiator_heating is True)
                            and fan_temperature > data.temperature + 2):
                        maximum = c["night_fan_percent"] if data.period in (
                            c["night_state"], c["pre_sleep_state"], c["pre_wake_state"]
                        ) else c["max_fan_percent"]
                        d.fan_percent = int(min(maximum, max(20, error * 60)))
                        if c["controller"] in ("pi", "pid"):
                            d.fan_percent = int(min(maximum, max(20, d.demand_percent)))
                        d.fan_reason = "requested" if d.fan_percent else "speed_limit_zero"
                if error >= c["floor_deficit"]:
                    if self.deficit_since is None:
                        self.deficit_since = now
                    delay_elapsed = now - self.deficit_since >= c["floor_delay_minutes"] * 60
                    # v0.1: only confirmed outage (or no radiator), not speculative insufficiency.
                    reserve_needed = data.heating_available is False or not c.get("radiator_entity")
                    if (reserve_needed and c["floor_enabled"] and c.get("floor_entity")
                            and delay_elapsed and not self.floor_locked):
                        if self.floor_since is None:
                            self.floor_since = now
                        if now - self.floor_since < c["floor_max_minutes"] * 60:
                            d.floor = True
                            d.status, d.reason = "backup_heating", "confirmed_outage_reserve"
                        else:
                            self.floor_locked = True
                            d.reason = "floor_runtime_limit"
                    elif not d.radiator:
                        d.reason = "reserve_wait_or_disabled"
                else:
                    self.deficit_since = None
            elif mode == "cool" and self.cooling and c.get("ac_entity"):
                d.cooling = True
                d.status, d.reason = "cooling", "above_target"
            if error <= 0:
                self.floor_locked = False
            if not self.heating or mode != "heat":
                self.deficit_since = None
            if not d.floor:
                self.floor_since = None
            return d
        self.heating = self.cooling = False
        self.pid.reset()
        self.deficit_since = self.floor_since = None
        return d
