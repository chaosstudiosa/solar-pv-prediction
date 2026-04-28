"""The Solar PV Prediction (History Based) integration."""
from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .average import PowerAverager
from .const import (
    DATA_AVERAGER,
    DATA_COORDINATOR,
    DATA_SPLINE,
    DATA_TRIM,
    DOMAIN,
    PLATFORMS,
    SENSOR_UPDATE_INTERVAL_SECONDS,
)
from .coordinator import SolarPVCoordinator
from .spline import HermiteSpline
from .trim import TrimManager

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Solar PV Prediction instance from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    coordinator = SolarPVCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    spline = HermiteSpline(entry, coordinator)
    trim = TrimManager(hass, entry, coordinator, spline)
    averager = PowerAverager(hass, entry)

    hass.data[DOMAIN][entry.entry_id] = {
        DATA_COORDINATOR: coordinator,
        DATA_SPLINE: spline,
        DATA_TRIM: trim,
        DATA_AVERAGER: averager,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Start sunrise reset + per-minute auto-adjust AFTER platforms are up so
    # the Number entity has had a chance to restore its previous factor first.
    entry.async_on_unload(trim.async_start())

    # Independent averager tick — runs every minute regardless of whether the
    # PV Power Average / Inverter Power Average sensors are enabled in the
    # entity registry. This ensures PV Available Power always has fresh data.
    entry.async_on_unload(
        async_track_time_interval(
            hass,
            lambda _now: averager.tick(),
            timedelta(seconds=SENSOR_UPDATE_INTERVAL_SECONDS),
        )
    )

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
