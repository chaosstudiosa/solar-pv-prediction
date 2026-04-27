"""Cubic Hermite spline smoothing over 24 hour-of-day buckets."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_SHIFT_MINUTES,
    CONF_TENSION,
    DEFAULT_SHIFT_MINUTES,
    DEFAULT_TENSION,
)

if TYPE_CHECKING:
    from .coordinator import SolarPVCoordinator

_LOGGER = logging.getLogger(__name__)


class HermiteSpline:
    """Cubic Hermite spline with tension, wrapped over 24 hourly control points.

    Tension = 0 behaves like Catmull-Rom (smooth through all points).
    Tension = 1 flattens tangents (curve hugs the hour-steps more tightly).
    ``shift_minutes`` > 0 moves the curve later in the day; < 0 earlier.
    """

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: SolarPVCoordinator,
    ) -> None:
        self.entry = entry
        self.coordinator = coordinator

    @property
    def tension(self) -> float:
        return float(self.entry.options.get(CONF_TENSION, DEFAULT_TENSION))

    @property
    def shift_minutes(self) -> int:
        return int(self.entry.options.get(CONF_SHIFT_MINUTES, DEFAULT_SHIFT_MINUTES))

    def _points(self) -> list[float]:
        data = self.coordinator.data or {}
        return [float(data.get(h, 0.0)) for h in range(24)]

    def value_at_minute(self, minute_of_day: int) -> float:
        """Interpolate smoothed PV max at a given minute (0..1439)."""
        points = self._points()

        # Positive shift delays the curve: at minute M we sample M - shift.
        shifted_min = (minute_of_day - self.shift_minutes) % 1440
        hour_float = shifted_min / 60.0
        idx = int(hour_float) % 24
        t = hour_float - int(hour_float)

        p0 = points[(idx - 1) % 24]
        p1 = points[idx % 24]
        p2 = points[(idx + 1) % 24]
        p3 = points[(idx + 2) % 24]

        c = max(0.0, min(1.0, 1.0 - self.tension))
        m1 = c * (p2 - p0) * 0.5
        m2 = c * (p3 - p1) * 0.5

        t2 = t * t
        t3 = t2 * t
        h00 = 2.0 * t3 - 3.0 * t2 + 1.0
        h10 = t3 - 2.0 * t2 + t
        h01 = -2.0 * t3 + 3.0 * t2
        h11 = t3 - t2

        val = h00 * p1 + h10 * m1 + h01 * p2 + h11 * m2
        return max(0.0, val)

    def raw_value_at_hour(self, hour: int) -> float:
        """Return the raw bucket max for ``hour`` (0..23)."""
        data = self.coordinator.data or {}
        return float(data.get(hour % 24, 0.0))
