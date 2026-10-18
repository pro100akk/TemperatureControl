"""Bounded empirical thermal learning and a time-based PI/PID controller.

Thermal coefficients are observed net temperature rates, not appliance power.
No experiments or actuator commands are performed by the learner.
"""

from math import isfinite


class ThermalModel:
    """Learn separate rates for stable, confirmed appliance operating states."""

    def __init__(self):
        self.rates = {}
        self.sample = None
        self.current = None

    def restore(self, saved):
        if not isinstance(saved, dict):
            return
        for key, row in saved.items():
            if key not in ("idle", "radiator", "radiator_fan", "floor", "cooling"):
                continue
            if (isinstance(row, dict) and all(
                    isinstance(row.get(k), (int, float)) and isfinite(row[k])
                    for k in ("rate", "count", "mae", "outdoor"))
                    and abs(row["rate"]) <= 6 and 0 <= row["mae"] <= 12
                    and 1 <= row["count"] <= 100000 and -60 <= row["outdoor"] <= 60):
                self.rates[key] = dict(row)

    def observe(self, temperature, outdoor, regime, now, enabled=True):
        valid = (enabled and regime is not None and temperature is not None
                 and outdoor is not None and isfinite(temperature) and isfinite(outdoor))
        self.current = regime if valid else None
        if not valid:
            self.sample = None
            return
        previous = self.sample
        if previous is None:
            self.sample = (now, temperature, outdoor, regime)
            return
        then, old_temp, old_outdoor, old_regime = previous
        elapsed = now - then
        if regime != old_regime or elapsed <= 0 or elapsed > 900 or abs(outdoor - old_outdoor) > 2:
            self.sample = (now, temperature, outdoor, regime)
            return
        if elapsed < 300:
            return
        self.sample = (now, temperature, outdoor, regime)
        rate = (temperature - old_temp) * 3600 / elapsed
        if abs(rate) > 6:  # Sensor jumps and rapid disturbances are not training data.
            self.current = None
            return
        row = self.rates.get(regime)
        if row is None or abs(old_outdoor - row["outdoor"]) > 3:
            self.rates[regime] = dict(rate=rate, count=1, mae=0.0, outdoor=old_outdoor)
            return
        # Error is measured against the previous coefficient, before learning.
        alpha = 0.15
        row["mae"] = (1 - alpha) * row["mae"] + alpha * abs(rate - row["rate"])
        row["rate"] += alpha * (rate - row["rate"])
        row["outdoor"] += alpha * (old_outdoor - row["outdoor"])
        row["count"] = min(100000, row["count"] + 1)

    def forecast_status(self, temperature, outdoor):
        if temperature is None:
            return "room_temperature_unavailable"
        if outdoor is None:
            return "outdoor_temperature_unavailable"
        if self.current is None:
            return "operating_state_unknown"
        row = self.rates.get(self.current)
        if row is None or row["count"] < 12:
            return "learning"
        if row["mae"] > 1.5:
            return "model_error_high"
        if abs(outdoor - row["outdoor"]) > 3:
            return "outdoor_conditions_changed"
        return "ready"

    def forecast(self, temperature, outdoor, minutes):
        if self.forecast_status(temperature, outdoor) != "ready":
            return None
        row = self.rates[self.current]
        return round(temperature + row["rate"] * minutes / 60, 2)


class PID:
    """Output 0..1; gains use error in °C and elapsed time in minutes."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.integral = 0.0
        self.previous = None
        self.derivative = 0.0
        self.context = None
        self.output = 0.0

    def update(self, temperature, target, mode, now, config):
        context = (mode, target, config["controller"])
        if self.context != context:
            self.reset()
            self.context = context
        direction = 1 if mode == "heat" else -1
        error = direction * (target - temperature)
        dt = 0.0
        if self.previous is not None:
            dt = (now - self.previous[0]) / 60
            if dt <= 0:
                return self.output
            if dt > 5:
                self.reset()
                self.context = context
                dt = 0.0
            else:
                # Derivative on measurement avoids a setpoint kick; one-minute filter.
                slope = -direction * (temperature - self.previous[1]) / dt
                self.derivative += dt / (1 + dt) * (slope - self.derivative)
        self.previous = (now, temperature)
        p = config["pid_kp"] * error
        d = config["pid_kd"] * self.derivative if config["controller"] == "pid" else 0
        proposed = max(0.0, min(1.0, self.integral + config["pid_ki"] * error * dt))
        raw = p + proposed + d
        # Conditional integration: never accumulate into output saturation.
        if 0 <= raw <= 1 or (raw > 1 and error < 0) or (raw < 0 and error > 0):
            self.integral = proposed
        self.output = max(0.0, min(1.0, p + self.integral + d))
        return self.output
