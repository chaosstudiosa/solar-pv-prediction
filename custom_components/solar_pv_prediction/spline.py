"""Cubic Hermite spline smoothing over 24 hour-of-day buckets.

Matches the logic of the original template sensor:
  - 5-point window centred on the current hour (prev2, prev1, now, next1, next2)
  - t = progress through the current hour (0..1), shifted by shift_minutes
  - Tensioned Hermite: tangents = (1-tension) * (next - prev) / 2
  - No wraparound — points outside 0..23 clamp to 0 so night hours stay at 0
"""
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
    """Tensioned cubic Hermite spline over 24 hourly control points.

    Uses a 5-point window (prev2, prev1, now, next1, next2) centred on the
    current hour, with NO wraparound — points outside [0,23] are treated as 0.
    This matches the original template sensor behaviour exactly.

    shift_minutes: positive = curve delayed (peak appears later).
                   negative = curve advanced (peak appears earlier).
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

    def _point(self, points: list[float], idx: int) -> float:
        """Return point at idx, clamping to 0 outside [0,23] (no wraparound)."""
        if idx < 0 or idx > 23:
            return 0.0
        return points[idx]

    def value_at_minute(self, minute_of_day: int) -> float:
        """Interpolate smoothed PV max at a given minute of day (0..1439).

        Replicates the original template:
          - shift_minutes applied to t (progress through the hour)
          - 5-point Hermite window, no wraparound
          - clamped to [0, max_point]
        """
        points = self._points()

        # Apply shift to the raw minute, then split into hour index + fractional t.
        shifted_min = minute_of_day - self.shift_minutes
        # Keep within day bounds for the hour index, but allow t to go slightly
        # outside [0,1] — clamp it afterwards.
        hour_idx = int(shifted_min // 60) % 24
        t = (shifted_min % 60) / 60.0
        # Clamp t to valid range (handles edge of day)
        t = max(0.0, min(1.0, t))

        # 5-point window — no wraparound (clamps to 0 outside day)
        p_2 = self._point(points, hour_idx - 2)
        p_1 = self._point(points, hour_idx - 1)
        p0  = self._point(points, hour_idx)
        p1  = self._point(points, hour_idx + 1)
        p2  = self._point(points, hour_idx + 2)

        # Tensioned Hermite tangents (matches original template)
        s = 1.0 - self.tension
        m0 = s * (p1 - p_2) / 2.0   # tangent at p0 using prev2 and next1
        m1 = s * (p2 - p_1) / 2.0   # tangent at p1 using prev1 and next2

        t2 = t * t
        t3 = t2 * t
        h00 =  2.0 * t3 - 3.0 * t2 + 1.0
        h10 =  t3 - 2.0 * t2 + t
        h01 = -2.0 * t3 + 3.0 * t2
        h11 =  t3 - t2

        val = h00 * p0 + h10 * m0 + h01 * p1 + h11 * m1

        # Clamp: never below 0, never above highest point in the day
        max_point = max(points) if points else 0.0
        return max(0.0, min(val, max_point))

    def raw_value_at_hour(self, hour: int) -> float:
        """Return the raw bucket max for ``hour`` (0..23)."""
        data = self.coordinator.data or {}
        return float(data.get(hour % 24, 0.0))
