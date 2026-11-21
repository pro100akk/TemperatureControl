"""Delayed, rate-limited mobile notifications, independent of control decisions."""

import re
from math import isfinite
from time import time

from homeassistant.exceptions import HomeAssistantError

from .const import ACTUATOR_FIELDS, entity_ids


def valid_notification_service(value):
    return isinstance(value, str) and re.fullmatch(r"notify\.mobile_app_[a-z0-9_]+", value) is not None


class ComfortNotifications:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.pending = {}
        self.sent = {}
        self.attempted = {}

    def restore(self, saved):
        if isinstance(saved, dict):
            self.sent = {key: value for key, value in saved.items()
                         if isinstance(value, (int, float)) and isfinite(value)}

    def snapshot(self):
        return dict(self.sent)

    def conditions(self):
        c = self.coordinator
        cfg, data, d = c.config, c.inputs, c.decision
        result = {}
        if cfg["notify_heating_outage"] and data.outdoor_temperature is not None:
            if data.outdoor_temperature <= cfg["notification_cold_outdoor"]:
                if cfg.get("heating_available_entity") and data.heating_available is False:
                    result["heating_outage"] = (
                        f"Датчик сообщает об отключении отопления. На улице {data.outdoor_temperature:.1f} °C. "
                        "Проверьте подачу тепла и возможность резервного обогрева.")
                elif (cfg.get("pipe_temperature_entity") and cfg.get("radiator_temperature_entity")
                      and cfg["control_enabled"] and not c.manual_hold and d and d.radiator
                      and data.temperature is not None and data.pipe_temperature is not None
                      and data.pipe_temperature <= data.temperature + cfg["radiator_cold_delta"]
                      and data.radiator_heating is False):
                    result["heating_outage_suspected"] = (
                        "Возможно, отсутствует подача тепла: при запросе нагрева труба и радиатор холодные. "
                        f"На улице {data.outdoor_temperature:.1f} °C. Проверьте отопление; причина не подтверждена.")
        if not cfg["notify_errors"]:
            return result
        if data.temperature is not None and data.temperature <= cfg["notification_cold_room"]:
            result["cold_room"] = f"Низкая температура в комнате: {data.temperature:.1f} °C. Проверьте отопление и окна."
        # A paused/observed controller must not report unfulfilled appliance commands.
        if not cfg["control_enabled"] or c.manual_hold:
            return result
        if data.temperature is None:
            result["room_sensor"] = "Нет актуальной температуры комнаты. Автоматическое управление ограничено. Проверьте датчик."
        for entity in entity_ids(cfg, ACTUATOR_FIELDS):
            state = c.hass.states.get(entity)
            if state is None or state.state in ("unknown", "unavailable"):
                result[f"unavailable:{entity}"] = f"Устройство {entity} недоступно. Проверьте питание и связь."
        if d:
            for key, requested, confirmed, label in (
                ("radiator_entity", d.radiator, data.radiator_heating, "радиатора"),
                ("floor_entity", d.floor, data.floor_heating, "тёплого пола"),
                ("ac_entity", d.cooling, data.ac_cooling, "кондиционера"),
            ):
                entity = cfg.get(key)
                state = c.hass.states.get(entity) if entity else None
                command = c.last_commands.get(entity, (None, 0))[0]
                mismatch = False
                if command and entity in c.owned and state is not None:
                    mismatch = (state.state != command[0]
                                or (len(command) == 2 and state.attributes.get("temperature") != command[1]))
                if entity and f"unavailable:{entity}" not in result and (mismatch or (requested and confirmed is not True)):
                    result[f"response:{key}"] = (
                        f"Команда для {label} не подтверждена состоянием устройства или датчиками. "
                        "Проверьте устройство, его датчики и подачу тепла/холода.")
            if d.fan_percent:
                for entity in entity_ids(cfg, ("radiator_fan_entity",)):
                    if f"unavailable:{entity}" in result:
                        continue
                    state = c.hass.states.get(entity)
                    matches = state is not None and state.state == "on" and entity in c.owned
                    if matches and entity.startswith("fan."):
                        actual = state.attributes.get("percentage")
                        matches = isinstance(actual, (float, int)) and abs(actual - d.fan_percent) <= 1
                    if not matches:
                        result[f"fan:{entity}"] = (
                            f"Запрошен обдув {d.fan_percent} %, но {entity} не подтвердил выполнение. "
                            "Проверьте контроллер вентилятора и внешнее управление.")
        # Stable keys keep changing exception text from restarting the delay.
        command_issues = [issue for issue in c.issues
                          if "unavailable" not in issue and "stale" not in issue
                          and not issue.startswith("notifications:")]
        if command_issues:
            result["control_issue"] = "Управление ограничено: " + "; ".join(command_issues[:3])
        return result

    async def async_update(self, now):
        c = self.coordinator
        cfg = c.config
        if not cfg["notifications_enabled"]:
            self.pending.clear()
            return
        service = cfg["notification_service"]
        if not valid_notification_service(service):
            c.issues.append("notifications: configure notify.mobile_app_<phone>")
            return
        conditions = self.conditions()
        self.pending = {key: started for key, started in self.pending.items() if key in conditions}
        wall = time()
        for key, message in conditions.items():
            started = self.pending.setdefault(key, now)
            if now - started < cfg["notification_delay_minutes"] * 60:
                continue
            if wall - self.sent.get(key, 0) < cfg["notification_repeat_minutes"] * 60:
                continue
            if now - self.attempted.get(key, float("-inf")) < 60:
                continue
            self.attempted[key] = now
            try:
                await c.hass.services.async_call("notify", service.removeprefix("notify."), {
                    "title": f"Apartment Comfort — {cfg['name']}",
                    "message": message,
                    "data": {"tag": f"{c.entry.entry_id}:{key}"},
                }, blocking=True)
            except (HomeAssistantError, ValueError) as err:
                c.issues.append(f"notifications: delivery failed ({type(err).__name__})")
            else:
                self.sent[key] = wall
