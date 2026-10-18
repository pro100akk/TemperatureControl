"""Build bilingual HA strings and a clean installable archive, using stdlib only."""

import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "apartment_comfort"

LABELS = {
    "controller": ("Controller (hysteresis / PI / PID)", "Регулятор (hysteresis / PI / PID)"),
    "learning_enabled": ("Learn thermal rates", "Самообучение тепловых коэффициентов"),
    "forecast_minutes": ("Forecast horizon (minutes)", "Горизонт прогноза (минуты)"),
    "pid_kp": ("Kp (1/°C)", "Kp (1/°C)"),
    "pid_ki": ("Ki (1/(°C·minute))", "Ki (1/(°C·мин))"),
    "pid_kd": ("Kd (minute/°C; PID only)", "Kd (мин/°C; только PID)"),
    "pid_max_offset": ("PI/PID maximum setpoint offset (°C)", "Максимальное смещение уставки PI/PID (°C)"),
    "name": ("Name", "Название"),
    "temperature_entity": ("Main room temperature", "Основной датчик температуры"),
    "period_entity": ("Existing day period", "Существующая сущность периода дня"),
    "window_entity": ("Window (on = open)", "Окно (on = открыто)"),
    "heating_available_entity": ("District heating available (on = available)", "Центральное отопление доступно (on = работает)"),
    "presence_entity": ("Presence (observation only)", "Присутствие (наблюдение)"),
    "outdoor_temperature_entity": ("Outdoor temperature", "Уличная температура"),
    "pipe_temperature_entity": ("Pipe temperature", "Температура трубы"),
    "radiator_temperature_entity": ("Radiator surface temperature", "Температура поверхности батареи"),
    "room_humidity_entity": ("Room humidity", "Влажность в квартире"),
    "shower_humidity_entity": ("Shower humidity", "Влажность в душе"),
    "outdoor_humidity_entity": ("Outdoor humidity (optional)", "Уличная влажность (необязательно)"),
    "co2_entity": ("CO2 (ppm)", "CO₂ (ppm)"),
    "cooking_entity": ("Cooking (on = cooking)", "Готовка (on = идёт приготовление пищи)"),
    "computer_power_entity": ("Computer power (observation only)", "Мощность компьютера (наблюдение)"),
    "shower_exhaust_entity": ("Shower exhaust (observation only)", "Вентилятор душа (наблюдение)"),
    "kitchen_exhaust_entity": ("Kitchen exhaust (observation only)", "Кухонная вытяжка (наблюдение)"),
    "radiator_entity": ("Radiator thermostat", "Термоголовка батареи"),
    "radiator_fan_entity": ("Radiator fan or relay (fan / switch / light)", "Вентилятор батареи или реле (fan / switch / light)"),
    "floor_entity": ("Floor thermostat", "Термостат тёплого пола"),
    "ac_entity": ("Air conditioner", "Кондиционер"),
    "control_enabled": ("Enable appliance control (off = observe)", "Управлять приборами (выключено = наблюдать)"),
    "floor_enabled": ("Allow backup floor heating", "Разрешить резервный нагрев пола"),
    "heat_with_window_open": ("Allow heating while the window is open", "Разрешить отопление при открытом окне"),
    "radiator_compensation": ("Compensate radiator sensor temperature (up to 10 °C)", "Компенсировать датчик термоголовки (до 10 °C)"),
    "fan_temperature_source": ("Radiator fan temperature source", "Режим контроля обдува батареи"),
    "deadband": ("Heating/cooling start deviation (°C)", "Отклонение для начала нагрева/охлаждения (°C)"),
    "floor_deficit": ("Minimum floor-heating deficit (°C)", "Минимальный дефицит для включения пола (°C)"),
    "floor_delay_minutes": ("Delay before backup floor (minutes)", "Задержка резервного пола (минуты)"),
    "floor_max_minutes": ("Maximum continuous floor request (minutes)", "Максимальное непрерывное включение пола (минуты)"),
    "ac_min_off_minutes": ("AC minimum off time (minutes)", "Минимальная пауза кондиционера (минуты)"),
    "night_fan_percent": ("Pre-sleep/night fan limit (%)", "Предел обдува перед сном и ночью (%)"),
    "max_fan_percent": ("Daytime fan limit (%)", "Предел обдува днём (%)"),
    "stale_minutes": ("Numeric sensor freshness limit (minutes)", "Срок актуальности числовых датчиков (минуты)"),
    "co2_high": ("CO2 ventilation threshold (ppm)", "CO₂: порог проветривания (ppm)"),
    "co2_clear": ("CO2 clear threshold (ppm)", "CO₂: порог завершения проветривания (ppm)"),
    "humidity_high": ("Room humidity high (%)", "Высокая влажность квартиры (%)"),
    "humidity_clear": ("Room humidity clear (%)", "Нормализация влажности квартиры (%)"),
    "shower_high": ("Shower humidity high (%)", "Высокая влажность душа (%)"),
    "shower_clear": ("Shower humidity clear (%)", "Нормализация влажности душа (%)"),
    "cooling_margin": ("Outdoor cooling recommendation margin (°C)", "Разница для рекомендации охлаждения улицей (°C)"),
    "window_min_temperature": ("Minimum room temperature while airing (°C)", "Минимальная температура квартиры при проветривании (°C)"),
}
for period, en, ru in (
    ("morning", "Morning", "Утро"), ("day", "Day", "День"),
    ("evening", "Evening", "Вечер"), ("pre_sleep", "Preparing for sleep", "Подготовка к ночи"),
    ("night", "Night", "Ночь"),
):
    LABELS[f"{period}_state"] = (f"{en}: exact HA state", f"{ru}: точное состояние в HA")
    LABELS[f"{period}_temperature"] = (f"{en}: target temperature", f"{ru}: целевая температура")

ERRORS = {
    "invalid_fan_source": ("Select surface or pipe.", "Выберите поверхность батареи или трубу."),
    "invalid_controller": ("Select hysteresis, pi or pid.", "Выберите hysteresis, pi или pid."),
    "unique_periods": ("Provide five distinct nonempty period states.", "Укажите пять различных непустых состояний периодов."),
    "duplicate_devices": ("An appliance cannot occupy multiple roles.", "Один прибор нельзя назначить нескольким ролям."),
    "self_reference": ("Do not select an Apartment Comfort thermostat as an appliance.", "Нельзя выбрать виртуальный термостат Apartment Comfort как прибор."),
    "device_in_use": ("An appliance is already assigned to another zone.", "Прибор уже назначен другой зоне этой интеграции."),
    "invalid_thresholds": ("Clear thresholds must be below high thresholds.", "Пороги нормализации должны быть ниже порогов включения."),
}
RECOMMENDATIONS = {
    "none": ("No recommendation", "Нет рекомендаций"),
    "check_window_sensor": ("Check window sensor", "Проверьте датчик окна"),
    "close_window": ("Window may be closed", "Окно можно закрыть"),
    "close_window_cold_air_quality_unresolved": ("Close window: room is cold; air quality still needs attention", "Закройте окно: прохладно; качество воздуха ещё требует внимания"),
    "keep_window_open": ("Continue airing", "Продолжайте проветривание"),
    "check_air_quality_before_closing": ("Check air quality before closing", "Проверьте качество воздуха перед закрытием"),
    "open_window_briefly": ("Consider brief airing: room is already cold", "Проветрите кратковременно: в помещении уже прохладно"),
    "open_window": ("Open window for airing", "Откройте окно для проветривания"),
    "check_ventilation": ("Check ventilation recommendations", "Проверьте рекомендации по вентиляции"),
}
STATUSES = {
    "observation": ("Observation only", "Наблюдение"), "manual_hold": ("Paused after manual control", "Пауза после ручного управления"),
    "idle": ("Maintaining temperature", "Поддержание температуры"), "off": ("Off", "Выключено"),
    "blocked": ("Required data unavailable", "Нет необходимых данных"), "ventilating": ("Window open", "Проветривание"),
    "heating": ("Radiator requested", "Запрошен нагрев батареей"), "backup_heating": ("Backup floor requested", "Запрошен резервный пол"),
    "cooling": ("Cooling requested", "Запрошено охлаждение"),
}

FORECAST_STATUSES = {
    "disabled": ("Learning disabled", "Обучение выключено"),
    "room_temperature_unavailable": ("Room temperature unavailable", "Нет актуальной комнатной температуры"),
    "outdoor_temperature_unavailable": ("Outdoor temperature unavailable", "Нет актуальной уличной температуры"),
    "operating_state_unknown": ("Appliance operation unconfirmed", "Работа приборов не подтверждена"),
    "manual_hold": ("Paused after manual control", "Пауза после ручного управления"),
    "window_open_or_unknown": ("Window open or unknown", "Окно открыто или состояние неизвестно"),
    "disturbance_active_or_unknown": ("Cooking/exhaust active or unknown", "Готовка или вытяжка активна либо неизвестна"),
    "learning": ("Collecting stable observations", "Накопление стабильных наблюдений"),
    "model_error_high": ("Model error too high", "Слишком большая ошибка модели"),
    "outdoor_conditions_changed": ("Outdoor conditions changed", "Изменились уличные условия"),
    "ready": ("Forecast ready", "Прогноз готов"),
}
FAN_STATUSES = {
    "not_configured": ("Fan not configured", "Вентилятор не выбран"),
    "observation": ("Observation only", "Наблюдение"),
    "manual_hold": ("Paused after manual control", "Пауза после ручного управления"),
    "no_heat_demand": ("No radiator demand", "Нет запроса нагрева батареи"),
    "surface_temperature_unavailable": ("Surface temperature unavailable", "Нет актуальной температуры поверхности батареи"),
    "pipe_temperature_unavailable": ("Pipe temperature unavailable", "Нет актуальной температуры трубы"),
    "radiator_not_heating": ("Radiator heating unconfirmed", "Нагрев термоголовки не подтверждён"),
    "radiator_not_hot": ("Radiator not hot enough", "Батарея недостаточно горячая"),
    "speed_limit_zero": ("Fan limit is zero", "Предел скорости равен нулю"),
    "waiting_or_blocked": ("Waiting for fan or control blocked", "Ожидание вентилятора или блокировка управления"),
    "on": ("Fan on", "Вентилятор включён"),
    "requested": ("Fan requested", "Запрошен обдув"),
}


def build_translations():
    sensors = [k for k in LABELS if k.endswith("_entity") and k not in (
        "radiator_entity", "radiator_fan_entity", "floor_entity", "ac_entity")]
    devices = ["radiator_entity", "radiator_fan_entity", "floor_entity", "ac_entity"]
    periods = [k for k in LABELS if k.endswith("_state") or k.endswith("_temperature") and k != "window_min_temperature"]
    policy = [k for k in LABELS if k not in sensors + devices + periods + ["name"]]
    titles = [("Sensors and existing day periods", "Датчики и существующие периоды дня"),
              ("Appliances", "Приборы"), ("Period mapping and temperatures", "Периоды и температуры"),
              ("Control and ventilation limits", "Управление и пороги проветривания")]
    descriptions = [
        ("Select existing HA entities. All fields except name, main temperature and period are optional. For cooking use a binary sensor/helper; numeric stove power needs a template binary sensor.",
         "Выберите существующие сущности HA. Обязательны только название, основная температура и период дня. Для готовки нужен бинарный датчик/помощник; числовую мощность плиты предварительно преобразуйте в шаблонный бинарный датчик."),
        ("Climate appliances must support heat/cool and off. Fan control uses a radiator surface sensor by default; policy settings can select a pipe sensor plus confirmed radiator heating instead. For switch/light relays, any positive speed request means fully on; 0 means off. Relays cannot enforce a reduced night speed. Exhausts support fan/switch/light for observation only.",
         "Климатические приборы должны поддерживать heat/cool и off. Для обдува по умолчанию нужен датчик поверхности батареи; в настройках управления можно выбрать датчик трубы вместе с подтверждённым нагревом термоголовки. Реле switch/light: положительный запрос скорости означает полное включение, 0 — выключение. Снизить ночную скорость реле не может. Вытяжки поддерживают fan/switch/light только для наблюдения."),
        ("Copy the exact states from Developer Tools → States, not translated display labels. Existing alarm and day-period automations remain authoritative.",
         "Скопируйте точные состояния из Инструменты разработчика → Состояния, а не отображаемые переводы. Переходами по будильнику и времени управляют ваши существующие автоматизации."),
        ("Control is disabled by default. Before enabling, switch selected appliances off and disable conflicting climate automations. Thresholds are adjustable starting values, not air-quality guarantees.",
         "Управление по умолчанию отключено. Перед включением выключите выбранные приборы и отключите конфликтующие климатические автоматизации. Пороги — настраиваемые начальные значения, а не гарантия качества воздуха."),
    ]
    for language, index in (("en", 0), ("ru", 1)):
        steps = {}
        for n, (step, keys) in enumerate((("user", ["name"] + sensors), ("devices", devices), ("periods", periods), ("policy", policy))):
            steps[step] = {"title": titles[n][index], "description": descriptions[n][index],
                           "data": {k: LABELS[k][index] for k in keys}}
        section = {"step": steps, "error": {k: v[index] for k, v in ERRORS.items()}}
        options_section = {"step": dict(steps), "error": section["error"]}
        options_section["step"]["init"] = {
            "title": ("Settings", "Настройки")[index],
            "description": ("Choose a section. Saving applies that section and preserves the other settings.",
                            "Выберите раздел. Сохранение применяет изменения этого раздела; остальные настройки сохраняются.")[index],
            "menu_options": {
                "user": ("Sensors and period entity", "Датчики и сущность периода")[index],
                "devices": ("Appliances and relays", "Приборы и реле")[index],
                "periods": ("Periods and target temperatures", "Периоды и целевые температуры")[index],
                "policy": ("Control and ventilation thresholds", "Управление и пороги проветривания")[index],
            },
        }
        data = {
            "title": "Apartment Comfort", "config": section, "options": options_section,
            "selector": {"fan_temperature_source": {"options": {
                "surface": ("Radiator surface sensor", "По датчику поверхности батареи")[index],
                "pipe": ("Pipe sensor and confirmed radiator heating", "По трубе и нагреву термоголовки")[index],
            }}},
            "entity": {
                "climate": {"thermostat": {"name": ("Thermostat", "Термостат")[index]}},
                "sensor": {
                    "status": {"name": ("Controller status", "Состояние контроллера")[index],
                               "state": {k: v[index] for k, v in STATUSES.items()}},
                    "recommendation": {"name": ("Ventilation recommendation", "Рекомендация по проветриванию")[index],
                                       "state": {k: v[index] for k, v in RECOMMENDATIONS.items()}},
                    "target": {"name": ("Effective target temperature", "Действующая целевая температура")[index]},
                    "predicted_temperature": {"name": ("Temperature forecast", "Прогноз температуры")[index]},
                    "demand_percent": {"name": ("Requested output", "Запрошенная мощность")[index]},
                    "forecast_status": {"name": ("Forecast status", "Состояние прогноза")[index],
                                        "state": {k: v[index] for k, v in FORECAST_STATUSES.items()}},
                    "fan_status": {"name": ("Radiator fan status", "Состояние обдува батареи")[index],
                                   "state": {k: v[index] for k, v in FAN_STATUSES.items()}},
                },
            },
        }
        destination = COMPONENT / "translations" / f"{language}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        rendered = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        destination.write_text(rendered, encoding="utf-8", newline="\n")
        if language == "en":
            (COMPONENT / "strings.json").write_text(rendered, encoding="utf-8", newline="\n")


def build_archive():
    version = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))["version"]
    output = ROOT / "dist" / f"apartment_comfort-{version}.zip"
    output.parent.mkdir(exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path in sorted(COMPONENT.rglob("*")):
            if path.is_file() and path.suffix in (".py", ".json") and "__pycache__" not in path.parts:
                archive.write(path, path.relative_to(ROOT).as_posix())
        for path in [ROOT / "README.md", ROOT / "LICENSE", *sorted((ROOT / "docs").glob("*.md"))]:
            if path.exists():
                archive.write(path, path.relative_to(ROOT).as_posix())
    print(output)


if __name__ == "__main__":
    build_translations()
    build_archive()
