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
    CONF_SCAN_INTERVAL_CONFIG,
    CONF_SCAN_INTERVAL_PROGRAM,
    CONF_USERNAME,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_CONFIG,
    DEFAULT_SCAN_INTERVAL_PROGRAM,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


async def _get_satellites(
    hass: HomeAssistant, username: str, password: str
) -> list[dict]:
    """Validate credentials and return list of satellites."""
    auth = RainBirdAuth(username, password)
    api = RainBirdAPI(auth)

    satellites = await hass.async_add_executor_job(
        api.get_satellite_list
    )

    if not satellites:
        raise ValueError("No controllers found for this account")

    return satellites


class RainBirdConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the Rain Bird IQ4 config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._username: str = ""
        self._password: str = ""
        self._satellites: list[dict] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step — credentials form."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]

            try:
                self._satellites = await _get_satellites(
                    self.hass, self._username, self._password
                )
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
                if len(self._satellites) == 1:
                    # Single controller — skip selector
                    return await self._async_create_entry(self._satellites[0])
                # Multiple controllers — show selector
                return await self.async_step_select_controller()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
            }),
            errors=errors,
        )

    async def async_step_select_controller(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle controller selection when multiple controllers are found."""
        if user_input is not None:
            satellite_id = int(user_input["satellite_id"])
            satellite = next(
                (s for s in self._satellites if s["id"] == satellite_id), None
            )
            if satellite:
                return await self._async_create_entry(satellite)

        options = {
            str(s["id"]): s.get("name", f"Controller {s['id']}")
            for s in self._satellites
        }

        return self.async_show_form(
            step_id="select_controller",
            data_schema=vol.Schema({
                vol.Required("satellite_id"): vol.In(options),
            }),
        )

    async def _async_create_entry(self, satellite: dict) -> FlowResult:
        """Create a config entry for the given satellite."""
        await self.async_set_unique_id(str(satellite["id"]))
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=satellite.get("name", "Rain Bird IQ4"),
            data={
                CONF_USERNAME:     self._username,
                CONF_PASSWORD:     self._password,
                CONF_SATELLITE_ID: satellite["id"],
                CONF_COMPANY_ID:   satellite["companyId"],
            },
        )

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        """Return the options flow handler."""
        return RainBirdOptionsFlow(config_entry)


class RainBirdOptionsFlow(config_entries.OptionsFlow):
    """Handle Rain Bird IQ4 options."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show the options form."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_realtime = self._config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        current_config = self._config_entry.options.get(
            CONF_SCAN_INTERVAL_CONFIG, DEFAULT_SCAN_INTERVAL_CONFIG
        )
        current_program = self._config_entry.options.get(
            CONF_SCAN_INTERVAL_PROGRAM, DEFAULT_SCAN_INTERVAL_PROGRAM
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(CONF_SCAN_INTERVAL, default=current_realtime): vol.All(
                    int, vol.Range(min=10, max=300)
                ),
                vol.Optional(CONF_SCAN_INTERVAL_CONFIG, default=current_config): vol.All(
                    int, vol.Range(min=60, max=3600)
                ),
                vol.Optional(CONF_SCAN_INTERVAL_PROGRAM, default=current_program): vol.All(
                    int, vol.Range(min=300, max=86400)
                ),
            }),
        )
