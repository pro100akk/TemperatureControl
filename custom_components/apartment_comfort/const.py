"""Configuration shared by the UI and control engine (temperatures in Celsius)."""

DOMAIN = "apartment_comfort"
PLATFORMS = ["climate", "sensor"]
PERIODS = ("morning", "day", "evening", "pre_sleep", "night", "pre_wake")
DEFAULTS = {
    "name": "Apartment Comfort",
    "morning_state": "morning", "day_state": "day", "evening_state": "evening",
    "pre_sleep_state": "pre_sleep", "night_state": "night", "pre_wake_state": "pre_wake",
    "morning_temperature": 22.0, "day_temperature": 22.0,
    "evening_temperature": 22.0, "pre_sleep_temperature": 21.0,
    "night_temperature": 20.5, "pre_wake_temperature": 22.0,
    "control_enabled": False, "floor_enabled": False,
    "heat_with_window_open": False,
    "radiator_compensation": True,
    "fan_temperature_source": "surface",
    "controller": "hysteresis", "learning_enabled": True,
    "forecast_minutes": 30, "pid_kp": 0.5, "pid_ki": 0.01, "pid_kd": 0.1,
    "pid_max_offset": 3.0,
    "deadband": 0.3, "floor_deficit": 1.0, "floor_delay_minutes": 30,
    "floor_max_minutes": 60, "ac_min_off_minutes": 5,
    "night_fan_percent": 30, "max_fan_percent": 100,
    "stale_minutes": 120, "co2_high": 1000, "co2_clear": 800,
    "humidity_high": 65, "humidity_clear": 58,
    "kitchen_control_enabled": False, "shower_control_enabled": False,
    "kitchen_night_allowed": False, "kitchen_airflow_percent": 50,
    "kitchen_night_airflow_percent": 25,
    "kitchen_speed_curve": "0:0,25:10,50:35,75:65,100:100",
    "window_exchange_per_hour": 0.3, "exhaust_exchange_per_hour": 0.6,
    "shower_high": 70, "shower_clear": 62,
    "cooling_margin": 1.0, "window_min_temperature": 18.0,
}
SENSOR_FIELDS = {
    "weather_entity": ["weather"],
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
EXHAUST_FIELDS = ("kitchen_exhaust_entity", "shower_exhaust_entity")
ACTUATOR_FIELDS = {
    "radiator_entity": ["climate"], "radiator_fan_entity": ["fan", "switch", "light"],
    "floor_entity": ["climate"], "ac_entity": ["climate"],
}
