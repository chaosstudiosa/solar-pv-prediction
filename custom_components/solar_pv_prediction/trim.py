"""Trim factor manager: sunrise reset + adaptive per-minute auto-adjust.

Logic:
  - At sunrise: reset factor from weather condition.
  - Every 60s (sun above horizon, spline > 0, PV sensor readable):

    1. VOLATILITY GUARD: compare instantaneous PV to PV Power Average.
       If they diverge by > 25%, use the average instead — this prevents
       chasing cloud spikes on partly-cloudy days.

    2. SOC GATE (when SOC >= threshold):
       Only allowed to adjust if:
         (a) load > (effective_pv + load_deadband), OR
         (b) effective_pv > (trimmed_pv + recovery_deadband)

    3. TARGET: target_factor = clamp(effective_pv / spline, 0..1)

    4. ADAPTIVE RATE: rate scales with the gap size (same up and down):
         gap < 10%  → base_rate (0.15)
         gap 10-30% → 2× base_rate
         gap > 30%  → 3× base_rate
       Capped at 1.0.

    5. SUSTAIN GATE: the adjustment direction (up or down) must be the same
       for 2 consecutive ticks (60s each = 120s total) before applying.
       This filters out transient spikes and dips.

    6. APPLY: new_factor = current + (target - current) × adaptive_rate
"""
from __future__ import annotations

from datetime import datetime
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
    CONF_LOAD_DEADBAND,
    CONF_MIN_PV_UPDATE,
    CONF_PV_ENTITIES,
    CONF_RECOVERY_DEADBAND,
    CONF_SOC_ENTITY,
    CONF_SOC_THRESHOLD,
    CONF_SUNRISE_FALLBACK,
    CONF_WEATHER_ENTITY,
    DATA_AVERAGER,
    DEFAULT_LOAD_DEADBAND,
    DEFAULT_MIN_PV_UPDATE,
    DEFAULT_RECOVERY_DEADBAND,
    DEFAULT_SOC_THRESHOLD,
    DEFAULT_SUNRISE_FALLBACK,
    DOMAIN,
    TRIM_MAX,
    TRIM_MIN,
    TRIM_UPDATE_INTERVAL_SECONDS,
    WEATHER_FACTOR_MAP,
)

if TYPE_CHECKING:
    from .average import PowerAverager
    from .coordinator import SolarPVCoordinator
    from .spline import HermiteSpline

_LOGGER = logging.getLogger(__name__)

# Volatility threshold: if instantaneous PV diverges more than this fraction
# from the rolling average, use the average instead.
_VOLATILITY_THRESHOLD = 0.25

# Adaptive rate: base rate and gap-tier multipliers.
_BASE_RATE = 0.15
_RATE_TIERS = [
    (0.30, 3.0),   # gap > 30% → 3× base
    (0.10, 2.0),   # gap > 10% → 2× base
    (0.00, 1.0),   # gap ≤ 10% → 1× base
]


def _adaptive_rate(gap_fraction: float) -> float:
    """Return the rate multiplied by a tier factor based on how big the gap is."""
    frac = abs(gap_fraction)
    for threshold, multiplier in _RATE_TIERS:
        if frac > threshold:
            return min(1.0, _BASE_RATE * multiplier)
    return _BASE_RATE


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
        # Sustain tracking: direction from previous tick ("up", "down", or None).
        self._prev_direction: str | None = None

    # --- Options proxies --------------------------------------------------
    def _opt(self, key: str, default: float) -> float:
        return float(self.entry.options.get(key, default))

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

    def _get_averager(self):
        """Get the PowerAverager from hass.data."""
        return self.hass.data.get(DOMAIN, {}).get(
            self.entry.entry_id, {}
        ).get(DATA_AVERAGER)

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
        from datetime import timedelta
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
                        "Unknown weather '%s' at sunrise; fallback %.2f",
                        state.state, new_factor,
                    )
        self._prev_direction = None
        self.set_factor(new_factor, source="sunrise")

    # --- Per-minute auto-adjust ------------------------------------------
    @callback
    def _async_adjust_tick(self, now: datetime) -> None:
        """Adaptive trim adjustment with volatility guard and sustain gate."""

        # Gate 1: sun must be above horizon.
        sun_state = self.hass.states.get("sun.sun")
        if sun_state is None or sun_state.state != "above_horizon":
            self._prev_direction = None
            return

        # Gate 2: spline > 0.
        local_now = dt_util.as_local(now) if now.tzinfo else dt_util.now()
        minute_of_day = local_now.hour * 60 + local_now.minute
        spline = self.spline.value_at_minute(minute_of_day)
        if spline <= 0:
            self._prev_direction = None
            return

        # Gate 3: PV sensor must be readable (sun gates already cover time-of-day).
        instant_pv = _sum_states(self.hass, self.entry.data.get(CONF_PV_ENTITIES, []))
        if instant_pv is None:
            self._prev_direction = None
            return

        # --- Step 1: VOLATILITY GUARD ---
        # Compare instantaneous PV to the rolling average. If they diverge
        # by more than 25%, use the average — this prevents chasing
        # cloud spikes on partly-cloudy days.
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

        # Read SOC and inverter/load.
        soc = _read_float(self.hass, self.entry.data.get(CONF_SOC_ENTITY))
        load = _read_float(self.hass, self.entry.data.get(CONF_INVERTER_ENTITY))

        # Current trimmed PV estimate.
        trimmed_pv = spline * self._factor

        # --- Step 2: SOC GATE ---
        if soc is not None and soc >= self.soc_threshold:
            load_exceeds = (
                load is not None
                and load > (effective_pv + self.load_deadband)
            )
            pv_proves_underestimate = (
                effective_pv > (trimmed_pv + self.recovery_deadband)
            )

            if not load_exceeds and not pv_proves_underestimate:
                _LOGGER.debug(
                    "Skipping: SOC %.0f%% >= %.0f%%, no update condition met "
                    "(load=%.0f, eff_pv=%.0f, trimmed=%.0f)",
                    soc, self.soc_threshold, load or 0,
                    effective_pv, trimmed_pv,
                )
                self._prev_direction = None
                return

        # --- Step 3: TARGET ---
        target = max(0.0, min(1.0, effective_pv / spline))
        delta = target - self._factor

        # --- Step 4: ADAPTIVE RATE ---
        gap_fraction = abs(delta) / self._factor if self._factor > 0 else abs(delta)
        rate = _adaptive_rate(gap_fraction)

        # --- Step 5: SUSTAIN GATE ---
        # The adjustment direction must be the same for 2 consecutive ticks
        # (60s + 60s = ~120s) before we apply it.
        current_direction = "up" if delta > 0 else "down"

        if self._prev_direction != current_direction:
            # Direction changed or first tick — record and wait.
            self._prev_direction = current_direction
            _LOGGER.debug(
                "Sustain: direction=%s, waiting for confirmation next tick "
                "(target=%.3f, current=%.3f, gap=%.0f%%)",
                current_direction, target, self._factor, gap_fraction * 100,
            )
            return

        # Direction confirmed for 2 consecutive ticks — apply.
        self._prev_direction = current_direction  # keep tracking

        # --- Step 6: APPLY ---
        new_factor = self._factor + (delta * rate)
        new_factor = max(0.0, min(1.0, new_factor))

        _LOGGER.debug(
            "Trim adjust: eff_pv=%.0fW spline=%.0fW target=%.3f "
            "gap=%.0f%% rate=%.3f direction=%s -> %.3f",
            effective_pv, spline, target, gap_fraction * 100,
            rate, current_direction, new_factor,
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
