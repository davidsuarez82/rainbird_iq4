"""Config flow for Rain Bird IQ4 integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .api import RainBirdAPI
from .auth import RainBirdAuth
from .const import (
    CONF_COMPANY_ID,
    CONF_PASSWORD,
    CONF_SATELLITE_ID,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


async def _validate_credentials(
    hass: HomeAssistant, username: str, password: str
) -> dict[str, Any]:
    """
    Validate credentials and discover satellite and company IDs.

    Returns a dict with satellite_id and company_id on success,
    or raises an exception on failure.
    """
    auth = RainBirdAuth(username, password)
    api = RainBirdAPI(auth)

    satellites = await hass.async_add_executor_job(
        lambda: api._get("Satellite/GetSatelliteList", {"includeInvisibleToCurrentUser": False})
    )

    if not satellites:
        raise ValueError("No controllers found for this account")

    satellite = satellites[0]
    return {
        "satellite_id": satellite["id"],
        "company_id":   satellite["companyId"],
        "name":         satellite.get("name", "Rain Bird IQ4"),
    }


class RainBirdConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the Rain Bird IQ4 config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step — credentials form."""
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]

            try:
                info = await _validate_credentials(self.hass, username, password)
            except ValueError as err:
                errors["base"] = "no_controllers"
                _LOGGER.error("No controllers found: %s", err)
            except RuntimeError as err:
                errors["base"] = "cannot_connect"
                _LOGGER.error("Connection error: %s", err)
            except Exception as err:
                errors["base"] = "unknown"
                _LOGGER.exception("Unexpected error: %s", err)
            else:
                await self.async_set_unique_id(str(info["satellite_id"]))
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=info["name"],
                    data={
                        CONF_USERNAME:      username,
                        CONF_PASSWORD:      password,
                        CONF_SATELLITE_ID:  info["satellite_id"],
                        CONF_COMPANY_ID:    info["company_id"],
                        CONF_SCAN_INTERVAL: user_input.get(
                            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                        ),
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): int,
            }),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow handler."""
        return RainBirdOptionsFlow(config_entry)


class RainBirdOptionsFlow(config_entries.OptionsFlow):
    """Handle Rain Bird IQ4 options — allows changing scan interval after setup."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the options form."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_interval = self._config_entry.options.get(
            CONF_SCAN_INTERVAL,
            self._config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(CONF_SCAN_INTERVAL, default=current_interval): vol.All(
                    int, vol.Range(min=10, max=300)
                ),
            }),
        )
