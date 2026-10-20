"""State acquisition, serialized decisions and conservative HA actuation."""

import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta
import logging
from math import ceil, isfinite
from time import monotonic, time

from homeassistant.core import callback, Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import ACTUATOR_FIELDS, DEFAULTS, DOMAIN, SENSOR_FIELDS, EXHAUST_FIELDS
from .engine import Engine, Inputs
from .ventilation import speed_curve, interpolate

_LOGGER = logging.getLogger(__name__)


class ComfortCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry):
        super().__init__(hass, _LOGGER, name=DOMAIN)
        self.entry = entry
        # Options are a complete snapshot: omitted optional roles mean removal.
        self.config = DEFAULTS | dict(entry.options or entry.data)
        self.engine = Engine(self.config)
        self.mode = "heat"
        self.override = None
        self.override_period = None
        self.inputs = Inputs()
        self.decision = None
        self.lock = asyncio.Lock()
        self.unsubs = []
        self.owned = set()
        self.last_commands = {}
        self.ac_stopped = monotonic()  # Restart never bypasses compressor cooldown.
        self.issues = []
        self.regime_reason = "operating_state_unknown"
        self.radiator_command = {}
        self.manual_hold = False
        self.stopping = False
        self.exhaust_holds = set()
        self.exhaust_expected = {}
        self.exhaust_contexts = []
        self.weather_rows = []
        self.weather_updated = None
        self.estimate_status = None
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")

    async def async_initialize(self):
        saved = await self.store.async_load() or {}
        if saved.get("model_sources") == self._model_sources():
            self.engine.model.restore(saved.get("thermal_model"))
        self.mode = saved.get("mode", "heat")
        if self.mode not in ("off", "heat", "cool"):
            self.mode = "off"
        self.override = saved.get("override")
        self.override_period = saved.get("override_period")
        # Persist ownership so a restart can stop a previously enabled device.
        configured = {self.config.get(k) for k in (*ACTUATOR_FIELDS, *EXHAUST_FIELDS)}
        self.owned = set(saved.get("owned", [])) & configured
        self.exhaust_holds = set(saved.get("exhaust_holds", [])) & configured
        self.exhaust_expected = {k: tuple(v) for k, v in saved.get("exhaust_expected", {}).items() if k in configured}
        self.manual_hold = bool(saved.get("manual_hold", False))
        self.engine.floor_locked = bool(saved.get("floor_locked", False))
        if saved.get("floor_started_at") is not None:
            self.engine.floor_since = monotonic() - max(0, time() - saved["floor_started_at"])
            self.engine.deficit_since = monotonic() - self.config["floor_delay_minutes"] * 60
        await self.async_tick()

    def _snapshot(self):
        return {
            "model_sources": self._model_sources(),
            "thermal_model": {key: dict(value) for key, value in self.engine.model.rates.items()},
            "mode": self.mode, "override": self.override,
            "override_period": self.override_period, "owned": sorted(self.owned),
            "manual_hold": self.manual_hold, "floor_locked": self.engine.floor_locked,
            "exhaust_holds": sorted(self.exhaust_holds),
            "exhaust_expected": self.exhaust_expected,
            "floor_started_at": (time() - (monotonic() - self.engine.floor_since)
                                 if self.engine.floor_since is not None else None),
        }

    def _model_sources(self):
        """Changing sensors/appliances invalidates coefficients from the old room setup."""
        return {key: self.config.get(key) for key in (
            *SENSOR_FIELDS, *ACTUATOR_FIELDS, "kitchen_speed_curve",
            "window_exchange_per_hour", "exhaust_exchange_per_hour")}

    def _save(self):
        self.store.async_delay_save(self._snapshot, 1)

    @callback
    def start(self):
        watched = [self.config[k] for k in (*SENSOR_FIELDS, *ACTUATOR_FIELDS) if self.config.get(k)]
        self.unsubs.append(async_track_state_change_event(self.hass, watched, self._changed))
        self.unsubs.append(async_track_time_interval(self.hass, self.async_tick, timedelta(seconds=30)))

    @callback
    def _changed(self, event):
        new = event.data.get("new_state")
        old = event.data.get("old_state")
        entity = event.data["entity_id"]
        if (new and entity in {self.config.get(k) for k in EXHAUST_FIELDS}
                and (new.context.id in self.exhaust_contexts or new.context.parent_id in self.exhaust_contexts)
                and entity in self.owned):
            self.exhaust_expected[entity] = self._exhaust_signature(new)
        if (new and old and entity in {self.config.get(k) for k in EXHAUST_FIELDS}
                and self._exhaust_signature(new) != self._exhaust_signature(old)
                and new.state in ("on", "off")
                and new.context.id not in self.exhaust_contexts
                and new.context.parent_id not in self.exhaust_contexts):
            self.owned.discard(entity)
            self.exhaust_holds.add(entity)
            self.exhaust_expected.pop(entity, None)
            self.last_commands.pop(entity, None)
            self._save()
        if (new and old and event.data["entity_id"] == self.config.get("ac_entity")
                and new.state == "off" and old.state != "off"):
            self.ac_stopped = monotonic()
        # User-originated physical edits suspend automatic management until resume.
        if (new and event.data["entity_id"] in {self.config.get(k) for k in ACTUATOR_FIELDS}
                and new.context.user_id is not None and new.context.parent_id is None):
            self.manual_hold = True
            self.owned.discard(event.data["entity_id"])
            self._save()
        self.hass.async_create_task(self.async_tick())

    @staticmethod
    def _exhaust_signature(state):
        return (state.state, state.attributes.get("percentage"), state.attributes.get("brightness"))

    def _check_exhaust_overrides(self):
        # Compare actual output, including edits made by automations without user_id.
        for key in EXHAUST_FIELDS:
            entity = self.config.get(key)
            state = self.hass.states.get(entity) if entity else None
            expected = self.exhaust_expected.get(entity)
            if state and state.state in ("on", "off") and expected:
                if self._exhaust_signature(state) != expected:
                    self.owned.discard(entity)
                    self.exhaust_holds.add(entity)
                    self.exhaust_expected.pop(entity, None)
                    self.last_commands.pop(entity, None)

    async def _weather(self):
        entity = self.config.get("weather_entity")
        if not entity:
            return
        now = monotonic()
        if self.weather_updated is not None and now - self.weather_updated < 1800:
            return
        self.weather_updated = now
        self.weather_rows = []
        try:
            response = await self.hass.services.async_call(
                "weather", "get_forecasts", {"entity_id": entity, "type": "hourly"},
                blocking=True, return_response=True)
            self.weather_rows = (response or {}).get(entity, {}).get("forecast", [])
        except (HomeAssistantError, ValueError, TypeError) as err:
            self.issues.append(f"weather forecast: {err}")

    def _weather_inputs(self, data):
        weather = self._state("weather_entity", fresh=True)
        if weather is None:
            return
        data.weather_condition = weather.state
        attrs = weather.attributes
        unit = attrs.get("temperature_unit", "°C")
        def number(value, temperature=False):
            try:
                value = float(value)
                if temperature:
                    value = TemperatureConverter.convert(value, unit, "°C")
                return value if isfinite(value) and (-60 <= value <= 60 if temperature else 0 <= value <= 100) else None
            except (TypeError, ValueError):
                return None
        current = number(attrs.get("temperature"), True)
        if data.outdoor_temperature is None:
            data.outdoor_temperature = current
        if data.outdoor_humidity is None:
            data.outdoor_humidity = number(attrs.get("humidity"))
        if current is None:
            return
        now = dt_util.utcnow()
        points = [(0.0, current)]
        for row in self.weather_rows:
            try:
                stamp = datetime.fromisoformat(row["datetime"].replace("Z", "+00:00"))
                seconds = (stamp - now).total_seconds()
                temp = number(row.get("temperature"), True)
                if 0 < seconds <= 10800 and temp is not None:
                    points.append((seconds, temp))
            except (KeyError, ValueError, TypeError):
                continue
        points.sort()
        horizon = self.config["forecast_minutes"] * 60
        for (t0, v0), (t1, v1) in zip(points, points[1:]):
            if t0 <= horizon <= t1:
                data.weather_temperature_delta = v0 + (v1-v0)*(horizon-t0)/(t1-t0) - current
                break

    def _state(self, key, *, fresh=False):
        entity_id = self.config.get(key)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            self.issues.append(f"{key}: unavailable")
            return None
        if fresh:
            reported = getattr(state, "last_reported", state.last_updated)
            if (dt_util.utcnow() - reported).total_seconds() > self.config["stale_minutes"] * 60:
                self.issues.append(f"{key}: stale")
                return None
        return state

    def _number(self, key, kind=None):
        state = self._state(key, fresh=True)
        if state is None:
            return None
        try:
            value = float(state.state)
            unit = state.attributes.get("unit_of_measurement")
            if kind == "temperature":
                value = TemperatureConverter.convert(value, unit, "°C")
                if not -60 <= value <= 120:
                    raise ValueError("temperature outside plausible range")
            elif kind == "humidity" and (unit != "%" or not 0 <= value <= 100):
                raise ValueError("humidity must be 0..100 percent")
            elif kind == "co2" and (unit != "ppm" or value < 0):
                raise ValueError("CO2 must be in ppm")
            elif kind == "power":
                if unit == "kW":
                    value *= 1000
                elif unit != "W":
                    raise ValueError("power must be W or kW")
                if value < 0:
                    raise ValueError("negative power")
            if not isfinite(value):
                raise ValueError("non-finite")
            return value
        except (ValueError, TypeError, HomeAssistantError):
            self.issues.append(f"{key}: invalid value or unit")
            return None

    def _bool(self, key, missing=None):
        if not self.config.get(key):
            return missing
        state = self._state(key)
        if state is None:
            return None
        if state.state in ("on", "off"):
            return state.state == "on"
        self.issues.append(f"{key}: expected on/off")
        return None

    def _read(self):
        period = self._state("period_entity")
        radiator = self._state("radiator_entity")
        radiator_heating = None
        if radiator is not None:
            actual = radiator.attributes.get("hvac_action")
            if radiator.state == "off" or actual in ("idle", "off"):
                radiator_heating = False
            elif actual == "heating" and self._state("radiator_entity", fresh=True) is not None:
                radiator_heating = True
        data = Inputs(
            temperature=self._number("temperature_entity", "temperature"),
            period=period.state if period else None,
            window=self._bool("window_entity", False),
            heating_available=self._bool("heating_available_entity", True),
            presence=self._bool("presence_entity"),
            outdoor_temperature=self._number("outdoor_temperature_entity", "temperature"),
            pipe_temperature=self._number("pipe_temperature_entity", "temperature"),
            radiator_temperature=self._number("radiator_temperature_entity", "temperature"),
            radiator_heating=radiator_heating,
            room_humidity=self._number("room_humidity_entity", "humidity"),
            shower_humidity=self._number("shower_humidity_entity", "humidity"),
            outdoor_humidity=self._number("outdoor_humidity_entity", "humidity"),
            co2=self._number("co2_entity", "co2"), cooking=self._bool("cooking_entity"),
            computer_power=self._number("computer_power_entity", "power"),
            shower_exhaust=self._bool("shower_exhaust_entity"),
            kitchen_exhaust=self._bool("kitchen_exhaust_entity"),
        )
        exhaust = self._state("kitchen_exhaust_entity")
        if exhaust and exhaust.state == "off":
            data.kitchen_airflow = 0
        elif exhaust and exhaust.state == "on":
            command = exhaust.attributes.get("percentage")
            if exhaust.attributes.get("brightness") is not None:
                command = exhaust.attributes["brightness"] * 100 / 255
            try:
                data.kitchen_airflow = interpolate(float(command), speed_curve(self.config["kitchen_speed_curve"])) if command is not None else 100
            except (TypeError, ValueError):
                data.kitchen_airflow = None
        self._weather_inputs(data)
        return data

    async def async_tick(self, _now=None):
        async with self.lock:
            if self.stopping:
                return
            self.issues = []
            self.radiator_command = {}
            self._check_exhaust_overrides()
            await self._weather()
            self.inputs = self._read()
            now = monotonic()
            self.engine.model.observe(
                self.inputs.temperature, self.inputs.outdoor_temperature,
                self._thermal_regime(), now, self.config["learning_enabled"])
            if self.inputs.period != self.override_period and self.override is not None:
                self.override = None
            if self.manual_hold or not self.config["control_enabled"]:
                self.engine.pid.reset()
            self.decision = self.engine.evaluate(self.inputs, now, self.mode, self.override)
            self.decision.predicted_temperature, self.estimate_status = self.engine.model.estimate(
                self.inputs, self.config["forecast_minutes"], self.config)
            if self.config["control_enabled"] and not self.manual_hold:
                read_issue_count = len(self.issues)
                await self._apply()
                await self._exhaust("kitchen_exhaust_entity", self.decision.kitchen_airflow_percent)
                await self._exhaust("shower_exhaust_entity", 100 if self.decision.shower_requested else 0)
                if len(self.issues) > read_issue_count:
                    self.engine.pid.reset()
            elif not self.config["control_enabled"] and self.owned:
                await self._release_all()
            self._save()
            self.async_set_updated_data(asdict(self.decision))

    def _thermal_regime(self):
        """Use confirmed actions, never requested modes as evidence of heat output."""
        self.regime_reason = "operating_state_unknown"
        if self.inputs.window is None:
            return None
        for key, value in (("cooking_entity", self.inputs.cooking),
                           ("kitchen_exhaust_entity", self.inputs.kitchen_exhaust),
                           ("shower_exhaust_entity", self.inputs.shower_exhaust)):
            if self.config.get(key) and value is None:
                return None
        active = []
        for key, regime, action in (("radiator_entity", "radiator", "heating"),
                                    ("floor_entity", "floor", "heating"),
                                    ("ac_entity", "cooling", "cooling")):
            if not self.config.get(key):
                continue
            # Switch-like states need not report periodically. An unchanged off
            # or idle state is not a stale numeric temperature measurement.
            state = self._state(key)
            if state is None:
                return None
            actual = state.attributes.get("hvac_action")
            if state.state == "off" or actual in ("idle", "off"):
                continue
            if actual != action:
                return None
            if self._state(key, fresh=True) is None:
                return None
            active.append(regime)
        regime = "+".join(active) if active else "idle"
        if self.config.get("radiator_fan_entity"):
            fan = self._state("radiator_fan_entity")
            if fan is None or fan.state not in ("on", "off"):
                return None
            if fan.state == "on":
                regime += "_fan"
        if (self.inputs.window is not False or self.inputs.kitchen_exhaust
                or self.inputs.cooking or self.inputs.weather_condition):
            regime += f"|w={self.inputs.window}|k={round((self.inputs.kitchen_airflow or 0)/10)}"
            regime += f"|s={self.inputs.shower_exhaust if self.inputs.window else False}"
            regime += f"|cook={self.inputs.cooking}|weather={self.inputs.weather_condition}"
        return regime

    @property
    def forecast_status(self):
        if self.estimate_status is not None:
            return self.estimate_status
        status = self.engine.model.forecast_status(
            self.inputs.temperature, self.inputs.outdoor_temperature)
        return self.regime_reason if status == "operating_state_unknown" else status

    @property
    def fan_status(self):
        if not self.config.get("radiator_fan_entity"):
            return "not_configured"
        if not self.config["control_enabled"]:
            return "observation"
        if self.manual_hold:
            return "manual_hold"
        if not self.decision:
            return "no_heat_demand"
        if self.decision.fan_percent:
            state = self.hass.states.get(self.config["radiator_fan_entity"])
            if state and state.state == "on":
                return "on"
            return "waiting_or_blocked"
        return self.decision.fan_reason

    async def _call(self, entity_id, service, data=None):
        try:
            kwargs = {}
            if entity_id in {self.config.get(k) for k in EXHAUST_FIELDS}:
                context = Context()
                self.exhaust_contexts.append(context.id)
                self.exhaust_contexts = self.exhaust_contexts[-100:]
                kwargs["context"] = context
            await self.hass.services.async_call(entity_id.split(".")[0], service,
                                                {"entity_id": entity_id, **(data or {})}, blocking=True, **kwargs)
            return True
        except (HomeAssistantError, ValueError) as err:
            self.issues.append(f"{entity_id}: {err}")
            _LOGGER.warning("Command %s failed for %s: %s", service, entity_id, err)
            return False

    async def _off(self, entity_id):
        if entity_id not in self.owned:
            return True
        state = self.hass.states.get(entity_id)
        success = state is not None and state.state == "off"
        if not success:
            last, sent_at = self.last_commands.get(entity_id, (None, 0))
            if last != ("off",) or monotonic() - sent_at >= 60:
                if entity_id.startswith("climate."):
                    sent = await self._call(entity_id, "set_hvac_mode", {"hvac_mode": "off"})
                else:
                    sent = await self._call(entity_id, "turn_off")
                if sent:
                    self.last_commands[entity_id] = (("off",), monotonic())
            state = self.hass.states.get(entity_id)
            success = state is not None and state.state == "off"
            if not success:
                self.issues.append(f"{entity_id}: waiting for off confirmation")
        if success:
            self.owned.discard(entity_id)
            self.exhaust_expected.pop(entity_id, None)
            self.last_commands.pop(entity_id, None)
            if entity_id == self.config.get("ac_entity"):
                self.ac_stopped = monotonic()
        return success

    async def _release_all(self):
        results = [await self._off(entity_id) for entity_id in list(self.owned)]
        return all(results)

    async def _climate(self, key, enabled, mode, target):
        entity_id = self.config.get(key)
        if not entity_id:
            return True
        if not enabled:
            return await self._off(entity_id)
        state = self._state(key)
        if state is None:
            return False
        if mode not in state.attributes.get("hvac_modes", []) or "off" not in state.attributes.get("hvac_modes", []):
            self.issues.append(f"{key}: requires {mode} and off modes")
            return False
        # Do not seize an already running external appliance on first contact.
        if entity_id not in self.owned and state.state != "off":
            self.issues.append(f"{key}: externally active; turn off before transferring control")
            return False
        if key == "ac_entity" and entity_id not in self.owned:
            if monotonic() - self.ac_stopped < self.config["ac_min_off_minutes"] * 60:
                self.issues.append("ac_entity: minimum off interval")
                return False
        # Climate attributes are expressed in HA's configured temperature unit.
        unit = self.hass.config.units.temperature_unit
        local = None
        compensation = 0.0
        if key == "radiator_entity" and self.config["radiator_compensation"]:
            try:
                local = TemperatureConverter.convert(
                    float(state.attributes.get("current_temperature")), unit, "°C")
                if not isfinite(local) or not -20 <= local <= 60:
                    raise ValueError("invalid local temperature")
                if self._state(key, fresh=True) is None:
                    local = None
                elif self.inputs.temperature is not None:
                    compensation = min(10.0, max(0.0, local - self.inputs.temperature))
            except (ValueError, TypeError, HomeAssistantError):
                local = None
        target += compensation
        native = TemperatureConverter.convert(target, "°C", unit)
        low, high = state.attributes.get("min_temp"), state.attributes.get("max_temp")
        if low is not None:
            native = max(native, low)
        if high is not None:
            native = min(native, high)
        step = state.attributes.get("target_temp_step", 0.5) or 0.5
        # Do not round a positive heat demand back down to the local reading.
        native = (ceil(native / step - 1e-9) if local is not None
                  else round(native / step)) * step
        if low is not None:
            native = max(native, low)
        if high is not None:
            native = min(native, high)
        if key == "radiator_entity":
            self.radiator_command = {"local_temperature": local,
                                     "compensation_available": local is not None,
                                     "compensation": compensation,
                                     "setpoint": TemperatureConverter.convert(native, unit, "°C")}
            if local is not None and self.radiator_command["setpoint"] <= local:
                self.issues.append("radiator_entity: setpoint limit prevents heat demand")
        desired = (mode, native)
        current = (state.state, state.attributes.get("temperature"))
        if desired == current:
            return True
        if (self.last_commands.get(entity_id, (None, 0))[0] == desired
                and monotonic() - self.last_commands[entity_id][1] < 60):
            return True
        # Record ownership before actuation: a partially successful command needs cleanup.
        self.owned.add(entity_id)
        await self._persist()
        if state.state != mode and not await self._call(entity_id, "set_hvac_mode", {"hvac_mode": mode}):
            return False
        success = await self._call(entity_id, "set_temperature", {"temperature": native})
        if success:
            self.last_commands[entity_id] = (desired, monotonic())
        return success

    async def _fan(self, percent):
        entity_id = self.config.get("radiator_fan_entity")
        if not entity_id:
            return
        if not percent:
            await self._off(entity_id)
            return
        state = self._state("radiator_fan_entity")
        if state is None:
            return
        if entity_id not in self.owned and state.state != "off":
            self.issues.append("radiator_fan_entity: externally active")
            return
        relay = entity_id.split(".")[0] in ("switch", "light")
        if state.state == "on" and (relay or state.attributes.get("percentage") == percent):
            return
        desired = ("on",) if relay else ("percentage", percent)
        if (self.last_commands.get(entity_id, (None, 0))[0] == desired
                and monotonic() - self.last_commands[entity_id][1] < 60):
            return
        self.owned.add(entity_id)
        await self._persist()
        # A relay only supports on/off; a positive speed request means on.
        success = (await self._call(entity_id, "turn_on") if relay else
                   await self._call(entity_id, "set_percentage", {"percentage": percent}))
        if success:
            self.last_commands[entity_id] = (desired, monotonic())

    async def _apply(self):
        d = self.decision
        # First release opposite mode; never start the other side after a failed stop.
        wanted = {"radiator_entity": d.radiator, "floor_entity": d.floor, "ac_entity": d.cooling}
        released = True
        for key, enabled in wanted.items():
            if not enabled and self.config.get(key):
                released = await self._off(self.config[key]) and released
        if not released:
            self.issues.append("interlock: failed to stop an appliance")
            await self._fan(0)
            return
        # External opposite-mode appliances also block conflicting operation.
        opposite = ("radiator_entity", "floor_entity") if d.cooling else ("ac_entity",)
        for key in opposite:
            if (d.cooling or d.radiator or d.floor) and self.config.get(key):
                state = self.hass.states.get(self.config[key])
                if state is None or state.state != "off":
                    self.issues.append(f"interlock: {key} not confirmed off")
                    await self._fan(0)
                    return
        radiator_ok = await self._climate("radiator_entity", d.radiator, "heat", d.actuator_target or d.target)
        await self._climate("floor_entity", d.floor, "heat", d.target)
        await self._climate("ac_entity", d.cooling, "cool", d.actuator_target or d.target)
        await self._fan(d.fan_percent if radiator_ok and d.radiator else 0)

    async def _persist(self):
        await self.store.async_save(self._snapshot())

    async def _exhaust(self, key, airflow):
        entity = self.config.get(key)
        if not entity or entity in self.exhaust_holds:
            return
        state = self._state(key)
        if state is None:
            return
        if key == "kitchen_exhaust_entity" and self.inputs.window is not True:
            airflow = 0
        if not airflow:
            await self._off(entity)
            if entity not in self.owned:
                self.exhaust_expected.pop(entity, None)
            return
        if entity not in self.owned and state.state != "off":
            return
        command = round(interpolate(airflow, speed_curve(self.config["kitchen_speed_curve"]), True)) if key == "kitchen_exhaust_entity" else 100
        domain = entity.split(".")[0]
        dimmer = domain == "light" and any(mode not in ("onoff", "unknown") for mode in state.attributes.get("supported_color_modes", []))
        variable = domain == "fan" and bool(state.attributes.get("supported_features", 0) & 1)
        # A relay cannot meet a reduced nighttime airflow limit.
        night = self.inputs.period in (self.config["night_state"], self.config["pre_sleep_state"], self.config["pre_wake_state"])
        if key == "kitchen_exhaust_entity" and night and airflow < 100 and not (dimmer or variable):
            await self._off(entity)
            self.exhaust_expected.pop(entity, None)
            return
        service, payload = ("turn_on", {})
        if dimmer:
            payload = {"brightness_pct": command}
        elif variable:
            service, payload = "set_percentage", {"percentage": command}
        desired = (service, command if dimmer or variable else 100)
        last, sent_at = self.last_commands.get(entity, (None, 0))
        if last == desired and entity in self.owned:
            matches = state.state == "on"
            if dimmer:
                matches = matches and abs(state.attributes.get("brightness", 0) * 100 / 255 - command) <= 1
            elif variable:
                matches = matches and state.attributes.get("percentage") == command
            if matches or monotonic() - sent_at < 60:
                return
        self.owned.add(entity)
        await self._persist()
        if await self._call(entity, service, payload):
            self.last_commands[entity] = (desired, monotonic())
            state = self.hass.states.get(entity)
            if state and state.state == "on":
                self.exhaust_expected[entity] = self._exhaust_signature(state)

    async def async_set_mode(self, mode):
        async with self.lock:
            self.mode = mode
            self.manual_hold = False
            if mode == "off":
                self.override = None
        await self.async_tick()

    async def async_set_target(self, target):
        async with self.lock:
            self.override = target
            self.override_period = self.inputs.period
        await self.async_tick()

    async def async_resume_schedule(self):
        async with self.lock:
            self.override = None
            self.manual_hold = False
            self.exhaust_holds.clear()
        await self.async_tick()

    async def async_stop(self):
        async with self.lock:
            if not await self._release_all():
                return False
            self.stopping = True
            for unsub in self.unsubs:
                unsub()
            self.unsubs.clear()
            await self._persist()
            return True
