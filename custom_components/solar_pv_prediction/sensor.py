"""Sensors: PV Raw, PV History, PV Trimmed, PV Power Average,
Inverter Power Average (disabled), PV Available Power."""
from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .average import PowerAverager
from .const import (
    CONF_NAME,
    CONF_SOC_ENTITY,
    CONF_SOC_THRESHOLD,
    DATA_AVERAGER,
    DATA_COORDINATOR,
    DATA_SPLINE,
    DATA_TRIM,
    DEFAULT_SOC_THRESHOLD,
    DOMAIN,
    SENSOR_UPDATE_INTERVAL_SECONDS,
)
from .coordinator import SolarPVCoordinator
from .spline import HermiteSpline
from .trim import TrimManager

_LOGGER = logging.getLogger(__name__)

_MINUTE_INTERVAL = timedelta(seconds=SENSOR_UPDATE_INTERVAL_SECONDS)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up all prediction sensors."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: SolarPVCoordinator = data[DATA_COORDINATOR]
    spline: HermiteSpline = data[DATA_SPLINE]
    trim: TrimManager = data[DATA_TRIM]
    averager: PowerAverager = data[DATA_AVERAGER]

    async_add_entities(
        [
            PVRawSensor(coordinator, entry),                                     # disabled
            PVHistorySensor(coordinator, entry, spline),                         # enabled
            PVTrimmedSensor(coordinator, entry, spline, trim),                   # enabled
            PVPowerAverageSensor(coordinator, entry, averager),                  # disabled
            InverterPowerAverageSensor(coordinator, entry, averager),            # disabled
            PVAvailablePowerSensor(coordinator, entry, averager, spline, trim),  # enabled
        ]
    )


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.data.get(CONF_NAME, "Solar PV Prediction"),
        manufacturer="Solar PV Prediction",
        model="History-Based Forecast",
    )


class _BaseSolarSensor(CoordinatorEntity[SolarPVCoordinator], SensorEntity):
    """Common base: power sensor, coordinator-backed, minute-ticked."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _unsub_minute = None

    def __init__(self, coordinator: SolarPVCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = _device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._unsub_minute = async_track_time_interval(
            self.hass, self._minute_tick, _MINUTE_INTERVAL
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_minute is not None:
            self._unsub_minute()
            self._unsub_minute = None
        await super().async_will_remove_from_hass()

    @callback
    def _minute_tick(self, _now) -> None:
        self.async_write_ha_state()


# ---------------------------------------------------------------------------
# PV Raw  (disabled by default — useful for debugging but clutters dashboard)
# ---------------------------------------------------------------------------
class PVRawSensor(_BaseSolarSensor):
    """Raw hour-of-day max for the current hour bucket. Disabled by default."""

    _attr_translation_key = "pv_raw"
    _attr_icon = "mdi:solar-power"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_pv_raw"
        self._attr_name = "PV Raw"

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data or {}
        return round(float(data.get(dt_util.now().hour, 0.0)), 1)

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        return {
            f"hour_{h:02d}": round(float(data.get(h, 0.0)), 1) for h in range(24)
        }


# ---------------------------------------------------------------------------
# PV History  (spline-smoothed curve — the main forecast reference)
# ---------------------------------------------------------------------------
class PVHistorySensor(_BaseSolarSensor):
    """Hermite spline smoothed over 24 hourly max buckets, sampled at current minute."""

    _attr_translation_key = "pv_history"
    _attr_icon = "mdi:chart-bell-curve"

    def __init__(self, coordinator, entry, spline: HermiteSpline) -> None:
        super().__init__(coordinator, entry)
        self._spline = spline
        self._attr_unique_id = f"{entry.entry_id}_pv_history"
        self._attr_name = "PV History"

    @property
    def native_value(self) -> float | None:
        now = dt_util.now()
        return round(self._spline.value_at_minute(now.hour * 60 + now.minute), 1)

    @property
    def extra_state_attributes(self):
        return {
            "tension": self._spline.tension,
            "shift_minutes": self._spline.shift_minutes,
        }


# ---------------------------------------------------------------------------
# PV Trimmed  (spline × trim factor — the actionable forecast)
# ---------------------------------------------------------------------------
class PVTrimmedSensor(_BaseSolarSensor):
    """Spline-smoothed value multiplied by the current trim factor."""

    _attr_translation_key = "pv_trimmed"
    _attr_icon = "mdi:solar-power-variant"

    def __init__(self, coordinator, entry, spline: HermiteSpline, trim: TrimManager) -> None:
        super().__init__(coordinator, entry)
        self._spline = spline
        self._trim = trim
        self._attr_unique_id = f"{entry.entry_id}_pv_trimmed"
        self._attr_name = "PV Trimmed"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._trim.register_listener(lambda _v: self.async_write_ha_state())
        )

    @property
    def native_value(self) -> float | None:
        now = dt_util.now()
        smoothed = self._spline.value_at_minute(now.hour * 60 + now.minute)
        return round(smoothed * self._trim.factor, 1)

    @property
    def extra_state_attributes(self):
        return {"trim_factor": round(self._trim.factor, 3)}


# ---------------------------------------------------------------------------
# PV Power Average  (disabled by default — used internally by Available Power)
# ---------------------------------------------------------------------------
class _AverageSensorBase(_BaseSolarSensor):
    """Base for rolling-average sensors — display only, tick is driven by __init__.py."""

    def __init__(self, coordinator, entry, averager: PowerAverager) -> None:
        super().__init__(coordinator, entry)
        self._averager = averager


class PVPowerAverageSensor(_AverageSensorBase):
    """Rolling time-weighted average of the sum of all PV power entities.
    Disabled by default — still sampled every minute for use in Available Power.
    """

    _attr_translation_key = "pv_power_average"
    _attr_icon = "mdi:solar-power"

    def __init__(self, coordinator, entry, averager: PowerAverager) -> None:
        super().__init__(coordinator, entry, averager)
        self._attr_unique_id = f"{entry.entry_id}_pv_power_average"
        self._attr_name = "PV Power Average"

    @property
    def native_value(self) -> float | None:
        v = self._averager.pv_average
        return round(v, 1) if v is not None else None


class InverterPowerAverageSensor(_AverageSensorBase):
    """Rolling time-weighted average of the inverter power entity.
    Disabled by default — still sampled every minute for use in Available Power.
    """

    _attr_translation_key = "inverter_power_average"
    _attr_icon = "mdi:transmission-tower-export"

    def __init__(self, coordinator, entry, averager: PowerAverager) -> None:
        super().__init__(coordinator, entry, averager)
        self._attr_unique_id = f"{entry.entry_id}_inverter_power_average"
        self._attr_name = "Inverter Power Average"

    @property
    def native_value(self) -> float | None:
        v = self._averager.inverter_average
        return round(v, 1) if v is not None else None


# ---------------------------------------------------------------------------
# PV Available Power  (the main output — never goes below 0)
# ---------------------------------------------------------------------------
class PVAvailablePowerSensor(_BaseSolarSensor):
    """PV Available Power — floored at 0 W.

    If SOC >= soc_threshold: uses PV Trimmed (forecast).
    Otherwise: uses PV Power Average (live).
    Subtracts inverter average and a 200 W house-load reserve.
    """

    _attr_translation_key = "pv_available_power"
    _attr_icon = "mdi:lightning-bolt-circle"

    _RESERVE_W = 200.0

    def __init__(
        self,
        coordinator,
        entry,
        averager: PowerAverager,
        spline: HermiteSpline,
        trim: TrimManager,
    ) -> None:
        super().__init__(coordinator, entry)
        self._averager = averager
        self._spline = spline
        self._trim = trim
        self._attr_unique_id = f"{entry.entry_id}_pv_available_power"
        self._attr_name = "PV Available Power"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._trim.register_listener(lambda _v: self.async_write_ha_state())
        )

    @property
    def _soc_threshold(self) -> float:
        return float(self._entry.options.get(CONF_SOC_THRESHOLD, DEFAULT_SOC_THRESHOLD))

    @property
    def native_value(self) -> float | None:
        soc_id = self._entry.data.get(CONF_SOC_ENTITY)
        soc = _read_float_state(self.hass, soc_id)
        inverter = self._averager.inverter_average or 0.0
        using_forecast = soc is not None and soc >= self._soc_threshold

        if using_forecast:
            now = dt_util.now()
            pv = self._spline.value_at_minute(now.hour * 60 + now.minute) * self._trim.factor
        else:
            pv = self._averager.pv_average or 0.0

        raw = pv - inverter - self._RESERVE_W
        return round(max(0.0, raw), 0)   # ← never below zero

    @property
    def extra_state_attributes(self):
        soc_id = self._entry.data.get(CONF_SOC_ENTITY)
        soc = _read_float_state(self.hass, soc_id)
        using_forecast = soc is not None and soc >= self._soc_threshold
        return {
            "using_forecast": using_forecast,
            "soc": soc,
            "mode": "forecast (SOC high)" if using_forecast else "live average",
        }


def _read_float_state(hass, entity_id: str | None) -> float | None:
    from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in (None, STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None
