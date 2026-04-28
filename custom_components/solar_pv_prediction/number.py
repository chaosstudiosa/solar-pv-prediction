"""Number entities: Trim Factor, History Days, Average Minutes."""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    AVERAGE_MINUTES_MAX,
    AVERAGE_MINUTES_MIN,
    AVERAGE_MINUTES_STEP,
    CONF_NAME,
    DATA_AVERAGE_MINUTES_NUMBER,
    DATA_AVERAGER,
    DATA_HISTORY_DAYS_NUMBER,
    DATA_TRIM,
    DEFAULT_AVERAGE_MINUTES,
    DEFAULT_HISTORY_DAYS,
    DOMAIN,
    HISTORY_DAYS_MAX,
    HISTORY_DAYS_MIN,
    HISTORY_DAYS_STEP,
    TRIM_MAX,
    TRIM_MIN,
    TRIM_STEP,
)
from .trim import TrimManager

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Trim Factor, History Days, and Average Minutes number entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    trim: TrimManager = data[DATA_TRIM]

    history_number = HistoryDaysNumber(entry)
    average_number = AverageMinutesNumber(entry)

    # Store references so coordinator and averager can read the live values.
    data[DATA_HISTORY_DAYS_NUMBER] = history_number
    data[DATA_AVERAGE_MINUTES_NUMBER] = average_number

    async_add_entities([
        TrimFactorNumber(entry, trim),
        history_number,
        average_number,
    ])


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.data.get(CONF_NAME, "Solar PV Prediction"),
        manufacturer="Solar PV Prediction",
        model="History-Based Forecast",
    )


class TrimFactorNumber(RestoreNumber):
    """User-adjustable trim factor; restored after HA restarts."""

    _attr_has_entity_name = True
    _attr_name = "Trim Factor"
    _attr_translation_key = "trim_factor"
    _attr_icon = "mdi:tune-variant"
    _attr_native_min_value = TRIM_MIN
    _attr_native_max_value = TRIM_MAX
    _attr_native_step = TRIM_STEP
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: ConfigEntry, trim: TrimManager) -> None:
        self._entry = entry
        self._trim = trim
        self._attr_unique_id = f"{entry.entry_id}_trim_factor"
        self._attr_device_info = _device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._trim.set_factor(float(last.native_value), source="restore")
        self.async_on_remove(
            self._trim.register_listener(self._on_trim_changed)
        )

    def _on_trim_changed(self, _value: float) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> float:
        return round(self._trim.factor, 3)

    async def async_set_native_value(self, value: float) -> None:
        self._trim.set_factor(value, source="user")


class HistoryDaysNumber(RestoreNumber):
    """How many days of recorder history to use per hour-of-day bucket."""

    _attr_has_entity_name = True
    _attr_name = "History Days"
    _attr_translation_key = "history_days"
    _attr_icon = "mdi:calendar-range"
    _attr_native_min_value = HISTORY_DAYS_MIN
    _attr_native_max_value = HISTORY_DAYS_MAX
    _attr_native_step = HISTORY_DAYS_STEP
    _attr_mode = NumberMode.SLIDER
    _attr_native_unit_of_measurement = "days"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._value: float = float(DEFAULT_HISTORY_DAYS)
        self._attr_unique_id = f"{entry.entry_id}_history_days"
        self._attr_device_info = _device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._value = float(last.native_value)

    @property
    def native_value(self) -> float:
        return self._value

    @property
    def value_int(self) -> int:
        return int(self._value)

    async def async_set_native_value(self, value: float) -> None:
        self._value = value
        self.async_write_ha_state()
        # Trigger a coordinator refresh so the new window takes effect immediately.
        from .const import DATA_COORDINATOR
        coordinator = self.hass.data[DOMAIN][self._entry.entry_id][DATA_COORDINATOR]
        await coordinator.async_request_refresh()


class AverageMinutesNumber(RestoreNumber):
    """Rolling-average window for PV Power Average and Inverter Power Average."""

    _attr_has_entity_name = True
    _attr_name = "Average Minutes"
    _attr_translation_key = "average_minutes"
    _attr_icon = "mdi:timer-sand"
    _attr_native_min_value = AVERAGE_MINUTES_MIN
    _attr_native_max_value = AVERAGE_MINUTES_MAX
    _attr_native_step = AVERAGE_MINUTES_STEP
    _attr_mode = NumberMode.SLIDER
    _attr_native_unit_of_measurement = "min"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._value: float = float(DEFAULT_AVERAGE_MINUTES)
        self._attr_unique_id = f"{entry.entry_id}_average_minutes"
        self._attr_device_info = _device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._value = float(last.native_value)
            # Apply restored value to the averager immediately.
            self._apply_to_averager()

    def _apply_to_averager(self) -> None:
        from .const import DATA_AVERAGER
        averager = self.hass.data[DOMAIN][self._entry.entry_id].get(DATA_AVERAGER)
        if averager is not None:
            averager.set_window(int(self._value))

    @property
    def native_value(self) -> float:
        return self._value

    @property
    def value_int(self) -> int:
        return int(self._value)

    async def async_set_native_value(self, value: float) -> None:
        self._value = value
        self.async_write_ha_state()
        self._apply_to_averager()
