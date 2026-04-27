"""DataUpdateCoordinator that fetches hour-of-day max PV from recorder."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import logging

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_MAX_PV_CLAMP,
    CONF_PV_ENTITIES,
    COORDINATOR_UPDATE_INTERVAL_HOURS,
    DATA_HISTORY_DAYS_NUMBER,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_MAX_PV_CLAMP,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class SolarPVCoordinator(DataUpdateCoordinator[dict[int, float]]):
    """Build a 24-slot hour-of-day max map from recorder long-term statistics.

    The history window is read live from the HistoryDaysNumber entity so the
    user can change it from the dashboard without reloading the integration.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(hours=COORDINATOR_UPDATE_INTERVAL_HOURS),
        )
        self.entry = entry
        self.pv_entities: list[str] = list(entry.data.get(CONF_PV_ENTITIES, []))

    @property
    def history_days(self) -> int:
        """Read live value from the HistoryDaysNumber entity, fall back to default."""
        number = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id, {}).get(
            DATA_HISTORY_DAYS_NUMBER
        )
        if number is not None:
            return number.value_int
        return DEFAULT_HISTORY_DAYS

    @property
    def max_pv_clamp(self) -> float:
        return float(self.entry.options.get(CONF_MAX_PV_CLAMP, DEFAULT_MAX_PV_CLAMP))

    async def _async_update_data(self) -> dict[int, float]:
        """Fetch stats and build the {hour_of_day: max_watts} map."""
        if not self.pv_entities:
            return {h: 0.0 for h in range(24)}

        end = dt_util.utcnow()
        start = end - timedelta(days=self.history_days)

        try:
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                start,
                end,
                set(self.pv_entities),
                "hour",
                None,
                {"max"},
            )
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(f"Error fetching statistics: {err}") from err

        # Sum across PV entities per absolute hourly timestamp first.
        combined_hourly: dict[datetime, float] = defaultdict(float)
        for _entity_id, series in stats.items():
            for row in series:
                ts = row.get("start")
                if ts is None:
                    continue
                if isinstance(ts, (int, float)):
                    ts_dt = dt_util.utc_from_timestamp(ts)
                else:
                    ts_dt = ts
                ts_dt = dt_util.as_utc(ts_dt).replace(
                    minute=0, second=0, microsecond=0
                )
                value = row.get("max")
                if value is None:
                    continue
                try:
                    combined_hourly[ts_dt] += float(value)
                except (TypeError, ValueError):
                    continue

        # Project onto 24 local hour-of-day slots, keeping the maximum.
        hod_max: dict[int, float] = {h: 0.0 for h in range(24)}
        local_tz = dt_util.DEFAULT_TIME_ZONE
        clamp = self.max_pv_clamp
        for ts_dt, total in combined_hourly.items():
            local_hour = ts_dt.astimezone(local_tz).hour
            val = min(total, clamp) if clamp and clamp > 0 else total
            if val > hod_max[local_hour]:
                hod_max[local_hour] = val

        _LOGGER.debug(
            "Updated HoD max map (history_days=%d): %s", self.history_days, hod_max
        )
        return hod_max
