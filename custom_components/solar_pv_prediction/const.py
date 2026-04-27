"""Constants for the Solar PV Prediction (History Based) integration."""
from __future__ import annotations

DOMAIN = "solar_pv_prediction"
PLATFORMS = ["sensor", "number"]

# --- Config entry data keys (entities, set once at setup time) -------------
CONF_NAME = "name"
CONF_PV_ENTITIES = "pv_entities"
CONF_SOC_ENTITY = "soc_entity"
CONF_INVERTER_ENTITY = "inverter_entity"
CONF_WEATHER_ENTITY = "weather_entity"

# --- Options: basic --------------------------------------------------------
CONF_SHIFT_MINUTES = "shift_minutes"
CONF_MAX_PV_CLAMP = "max_pv_clamp"

# --- Options: advanced -----------------------------------------------------
CONF_TENSION = "tension"
CONF_UP_RATE = "up_rate"
CONF_DOWN_RATE = "down_rate"
CONF_SOC_DEADBAND = "soc_deadband"
CONF_LOAD_DEADBAND = "load_deadband"
CONF_RECOVERY_DEADBAND = "recovery_deadband"
CONF_SOC_THRESHOLD = "soc_threshold"
CONF_MIN_PV_UPDATE = "min_pv_update"
CONF_SUNRISE_FALLBACK = "sunrise_fallback"

# --- Defaults: options -----------------------------------------------------
DEFAULT_SHIFT_MINUTES = 0
DEFAULT_MAX_PV_CLAMP = 10000.0  # W; 0 disables

DEFAULT_TENSION = 0.5
DEFAULT_UP_RATE = 0.7
DEFAULT_DOWN_RATE = 0.1
DEFAULT_SOC_DEADBAND = 2.0
DEFAULT_LOAD_DEADBAND = 100.0    # W — inverter deadband for curtailment guard
DEFAULT_RECOVERY_DEADBAND = 50.0 # W
DEFAULT_SOC_THRESHOLD = 95.0     # %
DEFAULT_MIN_PV_UPDATE = 50.0     # W
DEFAULT_SUNRISE_FALLBACK = 0.85

# --- Number entity defaults (runtime-tunable from the dashboard) -----------
DEFAULT_HISTORY_DAYS = 45
HISTORY_DAYS_MIN = 15
HISTORY_DAYS_MAX = 60
HISTORY_DAYS_STEP = 1

DEFAULT_AVERAGE_MINUTES = 5
AVERAGE_MINUTES_MIN = 3
AVERAGE_MINUTES_MAX = 10
AVERAGE_MINUTES_STEP = 1

# --- Trim factor bounds ----------------------------------------------------
TRIM_MIN = 0.05
TRIM_MAX = 1.50
TRIM_STEP = 0.01

# --- Weather condition -> trim factor map (sunrise reset) ------------------
WEATHER_FACTOR_MAP: dict[str, float] = {
    "sunny": 1.00,
    "clear-night": 1.00,
    "partlycloudy": 0.85,
    "cloudy": 0.60,
    "fog": 0.50,
    "hail": 0.30,
    "lightning": 0.40,
    "lightning-rainy": 0.35,
    "pouring": 0.25,
    "rainy": 0.45,
    "snowy": 0.30,
    "snowy-rainy": 0.30,
    "windy": 0.80,
    "windy-variant": 0.75,
    "exceptional": 0.50,
}

# --- hass.data keys --------------------------------------------------------
DATA_COORDINATOR = "coordinator"
DATA_TRIM = "trim"
DATA_SPLINE = "spline"
DATA_AVERAGER = "averager"
DATA_HISTORY_DAYS_NUMBER = "history_days_number"
DATA_AVERAGE_MINUTES_NUMBER = "average_minutes_number"

# --- Update intervals ------------------------------------------------------
COORDINATOR_UPDATE_INTERVAL_HOURS = 1
TRIM_UPDATE_INTERVAL_SECONDS = 60
SENSOR_UPDATE_INTERVAL_SECONDS = 60
