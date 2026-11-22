"""Passive command confirmation and bounded session quality measurements."""

from collections import deque
from math import isfinite


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


class CommandMonitor:
    """Confirm HA-reported state, not physical heat output or motor rotation."""

    def __init__(self, timeout=60, failure_threshold=3):
        self.timeout = timeout
        self.failure_threshold = failure_threshold
        self.pending = {}
        self.stats = {}
        self.sequence = 0

    def begin(self, entity, service, payload, now):
        key = (entity, service)
        # Opposite requests supersede pending confirmations, not device failures.
        conflicts = {"turn_off": ("turn_on", "set_percentage"),
                     "turn_on": ("turn_off",), "set_percentage": ("turn_off",)}
        for other in conflicts.get(service, ()):
            self.pending.pop((entity, other), None)
        if service == "set_hvac_mode" and payload.get("hvac_mode") == "off":
            self.pending.pop((entity, "set_temperature"), None)
        previous = self.pending.get(key)
        if previous and previous["payload"] == payload:
            return previous["id"]  # Retries must not move the confirmation deadline.
        self.sequence += 1
        self.pending[key] = {"id": self.sequence, "payload": dict(payload), "started": now}
        self.stats.setdefault(entity, {
            "confirmed": 0, "timeouts": 0, "service_errors": 0,
            "consecutive_failures": 0, "latency_total_seconds": 0.0,
            "last_latency_seconds": None, "max_latency_seconds": 0.0,
        })
        return self.sequence

    def cancel(self, entity):
        for key in list(self.pending):
            if key[0] == entity:
                self.pending.pop(key)

    @staticmethod
    def matches(service, payload, state):
        if state is None or state.state in ("unknown", "unavailable"):
            return False
        attrs = state.attributes
        if service == "set_hvac_mode":
            return state.state == payload.get("hvac_mode")
        if service == "set_temperature":
            actual, expected = attrs.get("temperature"), payload.get("temperature")
            return number(actual) and number(expected) and abs(actual - expected) <= 0.05
        if service == "set_percentage":
            actual, expected = attrs.get("percentage"), payload.get("percentage")
            return (number(actual) and number(expected) and abs(actual - expected) <= 1
                    and state.state == ("on" if expected else "off"))
        if service == "turn_off":
            return state.state == "off"
        if service == "turn_on":
            if "brightness_pct" in payload:
                actual = attrs.get("brightness")
                return (state.state == "on" and number(actual)
                        and abs(actual * 100 / 255 - payload["brightness_pct"]) <= 1)
            return state.state == "on"
        return False

    def _finish(self, key, now, outcome):
        pending = self.pending.pop(key)
        stats = self.stats[key[0]]
        stats[outcome] += 1
        elapsed = max(0, now - pending["started"])
        if outcome == "confirmed":
            stats["consecutive_failures"] = 0
            stats["latency_total_seconds"] += elapsed
            stats["last_latency_seconds"] = elapsed
            stats["max_latency_seconds"] = max(stats["max_latency_seconds"], elapsed)
        else:
            stats["consecutive_failures"] += 1
        return {"entity_id": key[0], "service": key[1], "command_id": pending["id"],
                "outcome": outcome, "latency_seconds": round(elapsed, 3)}

    def failed(self, entity, service, now):
        key = (entity, service)
        return self._finish(key, now, "service_errors") if key in self.pending else None

    def check(self, states, now):
        events = []
        for key, pending in list(self.pending.items()):
            if self.matches(key[1], pending["payload"], states.get(key[0])):
                events.append(self._finish(key, now, "confirmed"))
            elif now - pending["started"] >= self.timeout:
                events.append(self._finish(key, now, "timeouts"))
        return events

    def snapshot(self):
        return {"pending": len(self.pending), "devices": {
            entity: dict(stats, mean_latency_seconds=(
                round(stats["latency_total_seconds"] / stats["confirmed"], 3)
                if stats["confirmed"] else None),
                repeated_failure=stats["consecutive_failures"] >= self.failure_threshold)
            for entity, stats in self.stats.items()}}

    def issues(self):
        return [f"{entity}: repeated command confirmation failures ({stats['consecutive_failures']})"
                for entity, stats in self.stats.items()
                if stats["consecutive_failures"] >= self.failure_threshold]


class QualityMetrics:
    """Time-weighted control quality; long gaps do not count as observations."""

    def __init__(self):
        self.started = None
        self.latest = None
        self.previous = None
        self.seconds = self.absolute_error = self.squared_error = self.overheat_seconds = 0.0
        self.max_overshoot = 0.0
        self.switches = {}
        self.device_states = {}
        self.forecasts = deque(maxlen=120)
        self.last_forecast = None
        self.forecast_context = None
        self.forecast_count = 0
        self.forecast_error = 0.0
        self.forecast_discarded = 0

    def device(self, entity, state):
        value = state.state if state else None
        previous = self.device_states.get(entity)
        active = {"off", "on", "heat", "cool", "auto", "heat_cool", "dry", "fan_only"}
        if previous in active and value in active and previous != value:
            self.switches[entity] = self.switches.get(entity, 0) + 1
        self.device_states[entity] = value

    def observe(self, now, temperature, target, deadband, active, predicted, horizon, context):
        if self.started is None:
            self.started = now
        self.latest = now
        valid = active and number(temperature) and number(target)
        if valid:
            error = temperature - target
            if self.previous is not None:
                then, old_error, old_band, old_target = self.previous
                elapsed = now - then
                if 0 < elapsed <= 120 and target == old_target:
                    self.seconds += elapsed
                    self.absolute_error += abs(old_error) * elapsed
                    self.squared_error += old_error ** 2 * elapsed
                    self.overheat_seconds += elapsed if old_error > old_band else 0
            self.max_overshoot = max(self.max_overshoot, error)
            self.previous = (now, error, deadband, target)
        else:
            self.previous = None
        if context != self.forecast_context or not number(temperature):
            self.forecast_discarded += len(self.forecasts)
            self.forecasts.clear()
            self.forecast_context = context
        while self.forecasts and self.forecasts[0][0] <= now:
            due, forecast = self.forecasts.popleft()
            if now - due <= 60 and number(temperature):
                self.forecast_count += 1
                self.forecast_error += abs(temperature - forecast)
            else:
                self.forecast_discarded += 1
        if (number(temperature) and number(predicted)
                and (self.last_forecast is None or now - self.last_forecast >= 60)):
            self.forecasts.append((now + horizon * 60, predicted))
            self.last_forecast = now

    def snapshot(self):
        hours = (self.latest - self.started) / 3600 if self.started is not None else 0
        return {
            "scope": "since_reload", "observed_minutes": round(self.seconds / 60, 2),
            "mae_c": round(self.absolute_error / self.seconds, 3) if self.seconds else None,
            "rmse_c": round((self.squared_error / self.seconds) ** 0.5, 3) if self.seconds else None,
            "overheat_minutes": round(self.overheat_seconds / 60, 2),
            "max_overshoot_c": round(self.max_overshoot, 3), "switches": dict(self.switches),
            "switches_per_hour": {entity: round(count / hours, 2) if hours > 0 else None
                                  for entity, count in self.switches.items()},
            "forecast_mae_c": (round(self.forecast_error / self.forecast_count, 3)
                               if self.forecast_count else None),
            "forecast_samples": self.forecast_count, "forecast_discarded": self.forecast_discarded,
        }
