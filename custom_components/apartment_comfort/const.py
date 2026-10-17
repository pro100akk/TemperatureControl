"""Configuration shared by the UI and control engine (temperatures in Celsius)."""

DOMAIN = "apartment_comfort"
PLATFORMS = ["climate", "sensor"]
PERIODS = ("morning", "day", "evening", "pre_sleep", "night")
DEFAULTS = {
    "name": "Apartment Comfort",
    "morning_state": "morning", "day_state": "day", "evening_state": "evening",
    "pre_sleep_state": "pre_sleep", "night_state": "night",
    "morning_temperature": 22.0, "day_temperature": 22.0,
    "evening_temperature": 22.0, "pre_sleep_temperature": 21.0,
    "night_temperature": 20.5,
    "control_enabled": False, "floor_enabled": False,
    "deadband": 0.3, "floor_deficit": 1.0, "floor_delay_minutes": 30,
    "floor_max_minutes": 60, "ac_min_off_minutes": 5,
    "night_fan_percent": 30, "max_fan_percent": 100,
    "stale_minutes": 120, "co2_high": 1000, "co2_clear": 800,
    "humidity_high": 65, "humidity_clear": 58,
    "shower_high": 70, "shower_clear": 62,
    "cooling_margin": 1.0, "window_min_temperature": 18.0,
}
SENSOR_FIELDS = {
    "temperature_entity": ["sensor"],
    "period_entity": ["input_select", "select", "sensor"],
    "window_entity": ["binary_sensor", "input_boolean"],
    "heating_available_entity": ["binary_sensor", "input_boolean"],
    "presence_entity": ["binary_sensor", "input_boolean"],
    "outdoor_temperature_entity": ["sensor"],
    "pipe_temperature_entity": ["sensor"],
    "radiator_temperature_entity": ["sensor"],
    "room_humidity_entity": ["sensor"],
    "shower_humidity_entity": ["sensor"],
    "outdoor_humidity_entity": ["sensor"],
    "co2_entity": ["sensor"],
    "cooking_entity": ["binary_sensor", "input_boolean"],
    "computer_power_entity": ["sensor"],
    "shower_exhaust_entity": ["fan", "switch", "light"],
    "kitchen_exhaust_entity": ["fan", "switch", "light"],
}
ACTUATOR_FIELDS = {
    "radiator_entity": ["climate"], "radiator_fan_entity": ["fan", "switch", "light"],
    "floor_entity": ["climate"], "ac_entity": ["climate"],
}
