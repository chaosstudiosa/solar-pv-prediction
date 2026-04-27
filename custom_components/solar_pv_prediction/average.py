"""Rolling time-weighted average sampler for PV total and Inverter power."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    CONF_INVERTER_ENTITY,
    CONF_PV_ENTITIES,
    DEFAULT_AVERAGE_MINUTES,
)

_LOGGER = logging.getLogger(__name__)

_Sample = tuple[datetime, float]


class RollingAverage:
    """Time-ordered deque of (ts, value) samples with a configurable window."""

    def __init__(self, window_seconds: int) -> None:
        self._window = timedelta(seconds=window_seconds)
        self._samples: deque[_Sample] = deque()

    def set_window(self, window_seconds: int) -> None:
        self._window = timedelta(seconds=window_seconds)

    def add(self, value: float, ts: datetime | None = None) -> None:
        now = ts or dt_util.utcnow()
        self._samples.append((now, value))
        self._prune(now)

    def get(self) -> float | None:
        if not self._samples:
            return None
        now = dt_util.utcnow()
        self._prune(now)
        samples = list(self._samples)
        if not samples:
            return None
        if len(samples) == 1:
            return samples[0][1]

        total_weight = 0.0
        total_value = 0.0
        for i in range(len(samples) - 1):
            t0, v0 = samples[i]
            t1, _ = samples[i + 1]
            dt_sec = (t1 - t0).total_seconds()
            if dt_sec <= 0:
                continue
            total_value += v0 * dt_sec
            total_weight += dt_sec
        dt_last = (now - samples[-1][0]).total_seconds()
        if dt_last > 0:
            total_value += samples[-1][1] * dt_last
            total_weight += dt_last

        return (total_value / total_weight) if total_weight > 0 else samples[-1][1]

    def _prune(self, now: datetime) -> None:
        cutoff = now - self._window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()


class PowerAverager:
    """Holds one RollingAverage each for total PV and inverter power.

    The window is read live from the AverageMinutesNumber entity so the user
    can change it from the dashboard without reloading the integration.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        window = DEFAULT_AVERAGE_MINUTES * 60
        self._pv_avg = RollingAverage(window)
        self._inv_avg = RollingAverage(window)

    def set_window(self, minutes: int) -> None:
        """Update the rolling window on both averagers (called by AverageMinutesNumber)."""
        seconds = max(1, minutes) * 60
        self._pv_avg.set_window(seconds)
        self._inv_avg.set_window(seconds)
        _LOGGER.debug("Average window set to %d minutes", minutes)

    def tick(self) -> None:
        """Sample live HA states and append to the rolling averages."""
        pv_val = self._read_pv_total()
        if pv_val is not None:
            self._pv_avg.add(pv_val)

        inv_id = self.entry.data.get(CONF_INVERTER_ENTITY)
        inv_val = _read_float(self.hass, inv_id)
        if inv_val is not None:
            self._inv_avg.add(inv_val)

    @property
    def pv_average(self) -> float | None:
        return self._pv_avg.get()

    @property
    def inverter_average(self) -> float | None:
        return self._inv_avg.get()

    def _read_pv_total(self) -> float | None:
        entities: list[str] = list(self.entry.data.get(CONF_PV_ENTITIES, []))
        total = 0.0
        any_valid = False
        for ent in entities:
            v = _read_float(self.hass, ent)
            if v is None:
                continue
            total += v
            any_valid = True
        return total if any_valid else None


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
