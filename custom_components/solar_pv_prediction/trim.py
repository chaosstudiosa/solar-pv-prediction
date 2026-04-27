"""Trim factor manager: sunrise reset + per-minute auto-adjust."""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_sunrise,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DOWN_RATE,
    CONF_INVERTER_ENTITY,
    CONF_LOAD_DEADBAND,
    CONF_MIN_PV_UPDATE,
    CONF_PV_ENTITIES,
    CONF_RECOVERY_DEADBAND,
    CONF_SOC_DEADBAND,
    CONF_SOC_ENTITY,
    CONF_SOC_THRESHOLD,
    CONF_SUNRISE_FALLBACK,
    CONF_UP_RATE,
    CONF_WEATHER_ENTITY,
    DEFAULT_DOWN_RATE,
    DEFAULT_LOAD_DEADBAND,
    DEFAULT_MIN_PV_UPDATE,
    DEFAULT_RECOVERY_DEADBAND,
    DEFAULT_SOC_DEADBAND,
    DEFAULT_SOC_THRESHOLD,
    DEFAULT_SUNRISE_FALLBACK,
    DEFAULT_UP_RATE,
    TRIM_MAX,
    TRIM_MIN,
    TRIM_UPDATE_INTERVAL_SECONDS,
    WEATHER_FACTOR_MAP,
)

if TYPE_CHECKING:
    from .coordinator import SolarPVCoordinator
    from .spline import HermiteSpline

_LOGGER = logging.getLogger(__name__)


class TrimManager:
    """Holds the trim factor state and the sunrise/auto-adjust loops."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: SolarPVCoordinator,
        spline: HermiteSpline,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.spline = spline
        self._factor: float = 1.0
        self._listeners: list[Callable[[float], None]] = []

    # --- Options proxies --------------------------------------------------
    def _opt(self, key: str, default: float) -> float:
        return float(self.entry.options.get(key, default))

    @property
    def up_rate(self) -> float:
        return self._opt(CONF_UP_RATE, DEFAULT_UP_RATE)

    @property
    def down_rate(self) -> float:
        return self._opt(CONF_DOWN_RATE, DEFAULT_DOWN_RATE)

    @property
    def soc_deadband(self) -> float:
        return self._opt(CONF_SOC_DEADBAND, DEFAULT_SOC_DEADBAND)

    @property
    def load_deadband(self) -> float:
        return self._opt(CONF_LOAD_DEADBAND, DEFAULT_LOAD_DEADBAND)

    @property
    def recovery_deadband(self) -> float:
        return self._opt(CONF_RECOVERY_DEADBAND, DEFAULT_RECOVERY_DEADBAND)

    @property
    def soc_threshold(self) -> float:
        return self._opt(CONF_SOC_THRESHOLD, DEFAULT_SOC_THRESHOLD)

    @property
    def min_pv_update(self) -> float:
        return self._opt(CONF_MIN_PV_UPDATE, DEFAULT_MIN_PV_UPDATE)

    @property
    def sunrise_fallback(self) -> float:
        return self._opt(CONF_SUNRISE_FALLBACK, DEFAULT_SUNRISE_FALLBACK)

    # --- State ------------------------------------------------------------
    @property
    def factor(self) -> float:
        return self._factor

    def set_factor(self, value: float, *, source: str = "auto") -> None:
        """Clamp, store, and broadcast a new trim factor."""
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        v = max(TRIM_MIN, min(TRIM_MAX, v))
        if abs(v - self._factor) < 1e-6:
            return
        self._factor = v
        _LOGGER.debug("Trim factor -> %.3f (source=%s)", v, source)
        for cb in list(self._listeners):
            try:
                cb(v)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Error in trim listener")

    def register_listener(
        self, cb: Callable[[float], None]
    ) -> Callable[[], None]:
        """Register a factor-change listener. Returns an unregister callable."""
        self._listeners.append(cb)

        def _unregister() -> None:
            try:
                self._listeners.remove(cb)
            except ValueError:
                pass

        return _unregister

    # --- Lifecycle --------------------------------------------------------
    def async_start(self) -> CALLBACK_TYPE:
        """Attach the sunrise and periodic-adjust listeners."""
        cancel_adjust = async_track_time_interval(
            self.hass,
            self._async_adjust_tick,
            timedelta(seconds=TRIM_UPDATE_INTERVAL_SECONDS),
        )
        cancel_sunrise = async_track_sunrise(self.hass, self._async_sunrise_reset)

        @callback
        def _cancel() -> None:
            cancel_adjust()
            cancel_sunrise()

        return _cancel

    # --- Sunrise reset ----------------------------------------------------
    @callback
    def _async_sunrise_reset(self) -> None:
        """At sunrise, pick a starting factor from the weather condition."""
        weather_id = self.entry.data.get(CONF_WEATHER_ENTITY)
        new_factor = self.sunrise_fallback
        if weather_id:
            state = self.hass.states.get(weather_id)
            if state and state.state not in (None, STATE_UNKNOWN, STATE_UNAVAILABLE):
                mapped = WEATHER_FACTOR_MAP.get(state.state)
                if mapped is not None:
                    new_factor = mapped
                else:
                    _LOGGER.debug(
                        "Unknown weather condition '%s' at sunrise; using fallback %.2f",
                        state.state,
                        new_factor,
                    )
        self.set_factor(new_factor, source="sunrise")

    # --- Per-minute auto-adjust ------------------------------------------
    @callback
    def _async_adjust_tick(self, now: datetime) -> None:
        """Nudge the factor towards actual/predicted, respecting deadbands."""
        local_now = dt_util.as_local(now) if now.tzinfo else dt_util.now()
        minute_of_day = local_now.hour * 60 + local_now.minute
        predicted = self.spline.value_at_minute(minute_of_day)

        if predicted < self.min_pv_update:
            return  # pre-dawn / post-dusk / not meaningful

        total_pv = _sum_states(self.hass, self.entry.data.get(CONF_PV_ENTITIES, []))
        if total_pv is None:
            return

        soc_value = _read_float(self.hass, self.entry.data.get(CONF_SOC_ENTITY))
        inverter_value = _read_float(self.hass, self.entry.data.get(CONF_INVERTER_ENTITY))

        # Curtailment guard: if the battery is near full AND inverter output is
        # low, the inverter is likely throttling PV. Don't let those readings
        # drag the trim factor down.
        if (
            soc_value is not None
            and inverter_value is not None
            and soc_value >= (self.soc_threshold - self.soc_deadband)
            and inverter_value < self.load_deadband
        ):
            _LOGGER.debug(
                "Skipping auto-adjust: curtailment suspected "
                "(SOC %.1f >= %.1f, inverter %.0f < %.0f)",
                soc_value,
                self.soc_threshold - self.soc_deadband,
                inverter_value,
                self.load_deadband,
            )
            return

        target = total_pv / predicted if predicted > 0 else self._factor
        delta = target - self._factor

        # Recovery deadband: ignore small wattage differences.
        if abs(delta * predicted) < self.recovery_deadband:
            return

        rate = self.up_rate if delta > 0 else self.down_rate
        self.set_factor(self._factor + rate * delta, source="auto")


# --- Helpers --------------------------------------------------------------
def _read_float(hass: HomeAssistant, entity_id: str | None) -> float | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in (None, STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def _sum_states(hass: HomeAssistant, entity_ids: list[str]) -> float | None:
    total = 0.0
    any_valid = False
    for ent in entity_ids:
        v = _read_float(hass, ent)
        if v is None:
            continue
        total += v
        any_valid = True
    return total if any_valid else None
