"""Trim factor manager.

Logic:
  - 1 hour before sunrise: reset trim factor to 0.
  - Every 60 s when actual PV > 10 W and spline > 0:

    1. VOLATILITY GUARD: if instantaneous PV diverges > 25 % from the
       rolling average, use the average instead — prevents chasing
       cloud spikes on partly-cloudy days.

    2. DIRECTION — depends on battery SOC vs Battery Threshold:

       SOC < threshold (battery charging):
         Adjust freely UP or DOWN toward target (tracks actual PV).

       SOC >= threshold (battery full):
         Adjust only when:
           effective_pv > trimmed_pv          → UP
           OR load > effective_pv             → UP or DOWN
           OR load > trimmed_pv               → UP or DOWN
         No adjustment if none of those conditions are met.

    3. TARGET: target_factor = clamp(effective_pv / spline, 0..TRIM_MAX)

    4. ADAPTIVE RATE: scales with the gap between target and current:
         gap < 10 %  → 0.15
         gap 10-30 % → 0.30  (2 × base)
         gap > 30 %  → 0.45  (3 × base)

    5. SUSTAIN GATE: direction must match the previous tick (60 s)
       before any change is applied — filters single-tick noise.

    6. APPLY: new_factor = clamp(current + delta × rate, 0..TRIM_MAX)
"""
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
    CONF_INVERTER_ENTITY,
    CONF_PV_ENTITIES,
    CONF_SOC_ENTITY,
    DATA_AVERAGER,
    DATA_BATTERY_THRESHOLD_NUMBER,
    DEFAULT_BATTERY_THRESHOLD,
    DOMAIN,
    TRIM_MAX,
    TRIM_MIN,
    TRIM_UPDATE_INTERVAL_SECONDS,
)

if TYPE_CHECKING:
    from .coordinator import SolarPVCoordinator
    from .spline import HermiteSpline

_LOGGER = logging.getLogger(__name__)

# Gate: only run the adjustment loop when actual PV exceeds this value.
# Prevents chasing standby draw or moonlight at night.
_MIN_PV_WATTS = 10.0

# Volatility guard threshold (fraction of rolling average).
_VOLATILITY_THRESHOLD = 0.25

# Adaptive rate tiers.
_BASE_RATE = 0.15
_RATE_TIERS = [
    (0.30, 3.0),   # gap > 30 % → 0.45
    (0.10, 2.0),   # gap > 10 % → 0.30
    (0.00, 1.0),   # gap ≤ 10 % → 0.15
]


def _adaptive_rate(gap_fraction: float) -> float:
    """Return the adaptive rate for a given gap fraction."""
    frac = abs(gap_fraction)
    for threshold, multiplier in _RATE_TIERS:
        if frac > threshold:
            return min(1.0, _BASE_RATE * multiplier)
    return _BASE_RATE


class TrimManager:
    """Holds the trim factor state and the pre-dawn reset / auto-adjust loops."""

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
        self._prev_direction: str | None = None

    # --- hass.data helpers ------------------------------------------------
    def _get_averager(self):
        return self.hass.data.get(DOMAIN, {}).get(
            self.entry.entry_id, {}
        ).get(DATA_AVERAGER)

    def _get_battery_threshold(self) -> float:
        entity = self.hass.data.get(DOMAIN, {}).get(
            self.entry.entry_id, {}
        ).get(DATA_BATTERY_THRESHOLD_NUMBER)
        if entity is not None:
            return float(entity.native_value)
        return DEFAULT_BATTERY_THRESHOLD

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
        self._listeners.append(cb)

        def _unregister() -> None:
            try:
                self._listeners.remove(cb)
            except ValueError:
                pass

        return _unregister

    # --- Lifecycle --------------------------------------------------------
    def async_start(self) -> CALLBACK_TYPE:
        cancel_adjust = async_track_time_interval(
            self.hass,
            self._async_adjust_tick,
            timedelta(seconds=TRIM_UPDATE_INTERVAL_SECONDS),
        )
        # Reset to 0 one hour before sunrise each day so the factor
        # ramps up naturally from zero as the sun rises.
        cancel_pre_dawn = async_track_sunrise(
            self.hass,
            self._async_pre_dawn_reset,
            offset=timedelta(hours=-1),
        )

        @callback
        def _cancel() -> None:
            cancel_adjust()
            cancel_pre_dawn()

        return _cancel

    # --- Pre-dawn reset ---------------------------------------------------
    @callback
    def _async_pre_dawn_reset(self) -> None:
        """Set trim factor to 0 one hour before sunrise."""
        self._prev_direction = None
        self.set_factor(0.0, source="pre-dawn")
        _LOGGER.debug("Pre-dawn reset: trim factor set to 0")

    # --- Per-minute auto-adjust -------------------------------------------
    @callback
    def _async_adjust_tick(self, now: datetime) -> None:
        """Adaptive trim adjustment — fires every TRIM_UPDATE_INTERVAL_SECONDS."""

        # Gate 1: actual PV must exceed the minimum threshold.
        # This naturally gates out nighttime standby/moonlight without
        # needing a sun-position check.
        instant_pv = _sum_states(self.hass, self.entry.data.get(CONF_PV_ENTITIES, []))
        if instant_pv is None or instant_pv < _MIN_PV_WATTS:
            self._prev_direction = None
            return

        # Gate 2: spline must be predicting something > 0.
        # This prevents dividing by zero and stops adjustment during hours
        # with no historical PV data.
        local_now = dt_util.as_local(now) if now.tzinfo else dt_util.now()
        minute_of_day = local_now.hour * 60 + local_now.minute
        spline = self.spline.value_at_minute(minute_of_day)
        if spline <= 0:
            self._prev_direction = None
            return

        # --- Step 1: VOLATILITY GUARD ---
        averager = self._get_averager()
        pv_avg = averager.pv_average if averager else None

        if pv_avg is not None and pv_avg > 0:
            divergence = abs(instant_pv - pv_avg) / pv_avg
            if divergence > _VOLATILITY_THRESHOLD:
                effective_pv = pv_avg
                _LOGGER.debug(
                    "Volatility guard: instant=%.0fW avg=%.0fW "
                    "divergence=%.0f%% > %.0f%%, using average",
                    instant_pv, pv_avg, divergence * 100,
                    _VOLATILITY_THRESHOLD * 100,
                )
            else:
                effective_pv = instant_pv
        else:
            effective_pv = instant_pv

        # --- Step 2: DIRECTION ---
        soc = _read_float(self.hass, self.entry.data.get(CONF_SOC_ENTITY))
        load = _read_float(self.hass, self.entry.data.get(CONF_INVERTER_ENTITY))
        trimmed_pv = spline * self._factor
        battery_threshold = self._get_battery_threshold()
        battery_full = soc is not None and soc >= battery_threshold

        target = max(TRIM_MIN, min(TRIM_MAX, effective_pv / spline))

        if not battery_full:
            # Battery charging: track actual PV freely in both directions.
            pass  # always proceed to apply
        else:
            # Battery full: only adjust when there is a meaningful signal.
            load_exceeds = load is not None and (
                load > effective_pv or load > trimmed_pv
            )
            if not (effective_pv > trimmed_pv or load_exceeds):
                _LOGGER.debug(
                    "No adjustment (battery full): eff_pv=%.0fW "
                    "trimmed=%.0fW load=%s",
                    effective_pv, trimmed_pv,
                    f"{load:.0f}W" if load is not None else "unavailable",
                )
                self._prev_direction = None
                return

        delta = target - self._factor

        # Skip if delta is negligible.
        if abs(delta) < 1e-4:
            return

        # --- Step 3: ADAPTIVE RATE ---
        gap_fraction = abs(delta) / self._factor if self._factor > 0 else 1.0
        rate = _adaptive_rate(gap_fraction)

        # --- Step 4: SUSTAIN GATE ---
        current_direction = "up" if delta > 0 else "down"
        if self._prev_direction != current_direction:
            self._prev_direction = current_direction
            _LOGGER.debug(
                "Sustain: direction=%s — waiting for confirmation "
                "(target=%.3f current=%.3f gap=%.0f%%)",
                current_direction, target, self._factor, gap_fraction * 100,
            )
            return

        self._prev_direction = current_direction

        # --- Step 5: APPLY ---
        new_factor = max(TRIM_MIN, min(TRIM_MAX, self._factor + (delta * rate)))

        _LOGGER.debug(
            "Trim adjust: eff_pv=%.0fW spline=%.0fW trimmed=%.0fW "
            "target=%.3f gap=%.0f%% rate=%.3f dir=%s -> %.3f",
            effective_pv, spline, trimmed_pv,
            target, gap_fraction * 100, rate, current_direction, new_factor,
        )
        self.set_factor(new_factor, source="auto")


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
