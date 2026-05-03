"""Trim factor manager: sunrise reset + adaptive auto-adjust.

Logic:
  - At sunrise: reset factor from weather condition.
  - Every TRIM_TICK_SECONDS (10 s) when sun is above horizon, spline > 0,
    and PV sensor is readable:

    1. VOLATILITY GUARD: compare instantaneous PV to PV Power Average.
       If they diverge by > 25%, use the average instead — prevents
       chasing cloud spikes on partly-cloudy days.

    2. SOC GATE: if SOC is below battery threshold, do nothing.
       Trim only adjusts when the battery is at or above threshold.

    3. DIRECTION:
         UP   — effective_pv > pv_trimmed (we're under-predicting)
         DOWN — load > effective_pv OR load > pv_trimmed (over-predicting)

    4. SUSTAIN GATE: direction must match the previous tick for
       TRIM_SUSTAIN_TICKS (1 tick = 10 s) before counting toward an apply.

    5. APPLY GATE: every TRIM_APPLY_TICKS (6 ticks = 60 s) of sustained
       direction the adjustment is committed:
         new_factor = current + (target − current) × adaptive_rate

    6. ADAPTIVE RATE: scales with the gap between target and current:
         gap < 10%  → 0.15
         gap 10-30% → 0.30  (2× base)
         gap > 30%  → 0.45  (3× base)
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
    CONF_SUNRISE_FALLBACK,
    CONF_WEATHER_ENTITY,
    DATA_AVERAGER,
    DATA_BATTERY_THRESHOLD_NUMBER,
    DEFAULT_BATTERY_THRESHOLD,
    DEFAULT_SUNRISE_FALLBACK,
    DOMAIN,
    TRIM_APPLY_TICKS,
    TRIM_MAX,
    TRIM_MIN,
    TRIM_SUSTAIN_TICKS,
    TRIM_TICK_SECONDS,
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
    (0.30, 3.0),   # gap > 30% → 3× base = 0.45
    (0.10, 2.0),   # gap > 10% → 2× base = 0.30
    (0.00, 1.0),   # gap ≤ 10% → 1× base = 0.15
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
        # Tick state for sustain + apply gating.
        self._prev_direction: str | None = None
        self._sustain_count: int = 0
        self._apply_ticks: int = 0

    # --- Options proxy -------------------------------------------------------
    def _opt(self, key: str, default: float) -> float:
        return float(self.entry.options.get(key, default))

    @property
    def sunrise_fallback(self) -> float:
        return self._opt(CONF_SUNRISE_FALLBACK, DEFAULT_SUNRISE_FALLBACK)

    # --- hass.data helpers ---------------------------------------------------
    def _get_averager(self):
        """Get the PowerAverager from hass.data."""
        return self.hass.data.get(DOMAIN, {}).get(
            self.entry.entry_id, {}
        ).get(DATA_AVERAGER)

    def _get_battery_threshold(self) -> float:
        """Get the live battery threshold from BatteryThresholdNumber."""
        entity = self.hass.data.get(DOMAIN, {}).get(
            self.entry.entry_id, {}
        ).get(DATA_BATTERY_THRESHOLD_NUMBER)
        if entity is not None:
            return float(entity.native_value)
        return DEFAULT_BATTERY_THRESHOLD

    # --- State ---------------------------------------------------------------
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

    # --- Lifecycle -----------------------------------------------------------
    def async_start(self) -> CALLBACK_TYPE:
        cancel_adjust = async_track_time_interval(
            self.hass,
            self._async_adjust_tick,
            timedelta(seconds=TRIM_TICK_SECONDS),
        )
        cancel_sunrise = async_track_sunrise(self.hass, self._async_sunrise_reset)

        @callback
        def _cancel() -> None:
            cancel_adjust()
            cancel_sunrise()

        return _cancel

    # --- Sunrise reset -------------------------------------------------------
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
        self._reset_tick_state()
        self.set_factor(new_factor, source="sunrise")

    # --- Per-tick auto-adjust ------------------------------------------------
    @callback
    def _async_adjust_tick(self, now: datetime) -> None:
        """Adaptive trim adjustment — runs every TRIM_TICK_SECONDS seconds."""

        # Gate 1: sun must be above horizon.
        sun_state = self.hass.states.get("sun.sun")
        if sun_state is None or sun_state.state != "above_horizon":
            self._reset_tick_state()
            return

        # Gate 2: spline > 0.
        local_now = dt_util.as_local(now) if now.tzinfo else dt_util.now()
        minute_of_day = local_now.hour * 60 + local_now.minute
        spline = self.spline.value_at_minute(minute_of_day)
        if spline <= 0:
            self._reset_tick_state()
            return

        # Gate 3: PV sensor must be readable.
        instant_pv = _sum_states(self.hass, self.entry.data.get(CONF_PV_ENTITIES, []))
        if instant_pv is None:
            self._reset_tick_state()
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

        # --- Step 2: SOC GATE ---
        soc = _read_float(self.hass, self.entry.data.get(CONF_SOC_ENTITY))
        battery_threshold = self._get_battery_threshold()

        if soc is None or soc < battery_threshold:
            _LOGGER.debug(
                "SOC gate: soc=%s threshold=%.0f%% — skipping adjust",
                f"{soc:.0f}%" if soc is not None else "unavailable",
                battery_threshold,
            )
            self._reset_tick_state()
            return

        # --- Step 3: DIRECTION ---
        load = _read_float(self.hass, self.entry.data.get(CONF_INVERTER_ENTITY))
        trimmed_pv = spline * self._factor

        if effective_pv > trimmed_pv:
            direction = "up"
        elif load is not None and (load > effective_pv or load > trimmed_pv):
            direction = "down"
        else:
            _LOGGER.debug(
                "No adjustment needed: eff_pv=%.0fW trimmed=%.0fW load=%s",
                effective_pv, trimmed_pv,
                f"{load:.0f}W" if load is not None else "unavailable",
            )
            self._reset_tick_state()
            return

        # --- Step 4: SUSTAIN GATE ---
        if direction != self._prev_direction:
            _LOGGER.debug(
                "Sustain: direction changed to '%s' — waiting for confirmation",
                direction,
            )
            self._prev_direction = direction
            self._sustain_count = 1
            self._apply_ticks = 0
            return

        self._sustain_count += 1
        if self._sustain_count <= TRIM_SUSTAIN_TICKS:
            _LOGGER.debug(
                "Sustain: %d/%d ticks (%s) — not yet confirmed",
                self._sustain_count, TRIM_SUSTAIN_TICKS, direction,
            )
            return

        # --- Step 5: APPLY GATE ---
        self._apply_ticks += 1
        if self._apply_ticks < TRIM_APPLY_TICKS:
            _LOGGER.debug(
                "Apply gate: tick %d/%d (%s)",
                self._apply_ticks, TRIM_APPLY_TICKS, direction,
            )
            return

        self._apply_ticks = 0  # reset for next apply window

        # --- Step 6: APPLY ---
        target = max(TRIM_MIN, min(TRIM_MAX, effective_pv / spline))
        delta = target - self._factor
        gap_fraction = abs(delta) / self._factor if self._factor > 0 else abs(delta)
        rate = _adaptive_rate(gap_fraction)
        new_factor = max(TRIM_MIN, min(TRIM_MAX, self._factor + (delta * rate)))

        _LOGGER.debug(
            "Trim adjust: eff_pv=%.0fW spline=%.0fW trimmed=%.0fW "
            "target=%.3f gap=%.0f%% rate=%.3f dir=%s -> %.3f",
            effective_pv, spline, trimmed_pv,
            target, gap_fraction * 100, rate, direction, new_factor,
        )
        self.set_factor(new_factor, source="auto")

    def _reset_tick_state(self) -> None:
        """Reset sustain and apply counters."""
        self._prev_direction = None
        self._sustain_count = 0
        self._apply_ticks = 0


# --- Helpers -----------------------------------------------------------------
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
