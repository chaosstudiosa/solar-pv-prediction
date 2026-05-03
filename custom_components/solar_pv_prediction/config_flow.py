"""Config flow + options flow for Solar PV Prediction (History Based)."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_DOWN_RATE,
    CONF_INVERTER_ENTITY,
    CONF_LOAD_DEADBAND,
    CONF_MAX_PV_CLAMP,
    CONF_MIN_PV_UPDATE,
    CONF_NAME,
    CONF_PV_ENTITIES,
    CONF_RECOVERY_DEADBAND,
    CONF_SHIFT_MINUTES,
    CONF_SOC_DEADBAND,
    CONF_SOC_ENTITY,
    CONF_SOC_THRESHOLD,
    CONF_SUNRISE_FALLBACK,
    CONF_TENSION,
    CONF_UP_RATE,
    CONF_WEATHER_ENTITY,
    DEFAULT_DOWN_RATE,
    DEFAULT_LOAD_DEADBAND,
    DEFAULT_MAX_PV_CLAMP,
    DEFAULT_MIN_PV_UPDATE,
    DEFAULT_RECOVERY_DEADBAND,
    DEFAULT_SHIFT_MINUTES,
    DEFAULT_SOC_DEADBAND,
    DEFAULT_SOC_THRESHOLD,
    DEFAULT_SUNRISE_FALLBACK,
    DEFAULT_TENSION,
    DEFAULT_UP_RATE,
    DOMAIN,
)


def _user_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default="Solar PV Prediction"): str,
            vol.Required(CONF_PV_ENTITIES): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor", device_class="power", multiple=True
                )
            ),
            vol.Required(CONF_SOC_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Required(CONF_INVERTER_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor", device_class="power"
                )
            ),
            vol.Required(CONF_WEATHER_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="weather")
            ),
        }
    )


class SolarPVPredictionConfigFlow(ConfigFlow, domain=DOMAIN):
    """Initial setup: collect entities + a display name."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            if not user_input.get(CONF_PV_ENTITIES):
                errors[CONF_PV_ENTITIES] = "required"
            else:
                name = user_input[CONF_NAME]
                uid = f"{DOMAIN}_{name.lower().replace(' ', '_')}"
                await self.async_set_unique_id(uid)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=name, data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=_user_schema(), errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Allow changing the entity selectors via Settings → Configure."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            if not user_input.get(CONF_PV_ENTITIES):
                errors[CONF_PV_ENTITIES] = "required"
            else:
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, **user_input},
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reconfigure_successful")

        current = entry.data
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_PV_ENTITIES,
                    default=current.get(CONF_PV_ENTITIES, []),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor", device_class="power", multiple=True
                    )
                ),
                vol.Required(
                    CONF_SOC_ENTITY,
                    default=current.get(CONF_SOC_ENTITY, ""),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(
                    CONF_INVERTER_ENTITY,
                    default=current.get(CONF_INVERTER_ENTITY, ""),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor", device_class="power"
                    )
                ),
                vol.Required(
                    CONF_WEATHER_ENTITY,
                    default=current.get(CONF_WEATHER_ENTITY, ""),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="weather")
                ),
            }
        )
        return self.async_show_form(
            step_id="reconfigure", data_schema=schema, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SolarPVPredictionOptionsFlow()


class SolarPVPredictionOptionsFlow(OptionsFlow):
    """Two-step options: basic (curve) -> advanced (trim rates + deadbands)."""

    def __init__(self) -> None:
        self._basic: dict[str, Any] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return await self.async_step_basic(user_input)

    async def async_step_basic(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        opts = self.config_entry.options
        if user_input is not None:
            self._basic = user_input
            return await self.async_step_advanced()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SHIFT_MINUTES,
                    default=opts.get(CONF_SHIFT_MINUTES, DEFAULT_SHIFT_MINUTES),
                ): vol.All(int, vol.Range(min=-180, max=180)),
                vol.Required(
                    CONF_MAX_PV_CLAMP,
                    default=opts.get(CONF_MAX_PV_CLAMP, DEFAULT_MAX_PV_CLAMP),
                ): vol.All(vol.Coerce(float), vol.Range(min=0)),
            }
        )
        return self.async_show_form(step_id="basic", data_schema=schema)

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        opts = self.config_entry.options
        if user_input is not None:
            merged = {**self._basic, **user_input}
            return self.async_create_entry(title="", data=merged)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_TENSION,
                    default=opts.get(CONF_TENSION, DEFAULT_TENSION),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0)),
                vol.Required(
                    CONF_UP_RATE,
                    default=opts.get(CONF_UP_RATE, DEFAULT_UP_RATE),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0)),
                vol.Required(
                    CONF_DOWN_RATE,
                    default=opts.get(CONF_DOWN_RATE, DEFAULT_DOWN_RATE),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0)),
                vol.Required(
                    CONF_SOC_DEADBAND,
                    default=opts.get(CONF_SOC_DEADBAND, DEFAULT_SOC_DEADBAND),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=50.0)),
                vol.Required(
                    CONF_LOAD_DEADBAND,
                    default=opts.get(CONF_LOAD_DEADBAND, DEFAULT_LOAD_DEADBAND),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.0)),
                vol.Required(
                    CONF_RECOVERY_DEADBAND,
                    default=opts.get(
                        CONF_RECOVERY_DEADBAND, DEFAULT_RECOVERY_DEADBAND
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.0)),
                vol.Required(
                    CONF_SOC_THRESHOLD,
                    default=opts.get(CONF_SOC_THRESHOLD, DEFAULT_SOC_THRESHOLD),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=100.0)),
                vol.Required(
                    CONF_MIN_PV_UPDATE,
                    default=opts.get(CONF_MIN_PV_UPDATE, DEFAULT_MIN_PV_UPDATE),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.0)),
                vol.Required(
                    CONF_SUNRISE_FALLBACK,
                    default=opts.get(
                        CONF_SUNRISE_FALLBACK, DEFAULT_SUNRISE_FALLBACK
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.05, max=1.5)),
            }
        )
        return self.async_show_form(step_id="advanced", data_schema=schema)
