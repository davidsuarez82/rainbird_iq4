"""Rain Bird IQ4 integration for Home Assistant."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

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
from .coordinator import RainBirdCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "binary_sensor", "calendar", "number", "switch", "select"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Rain Bird IQ4 from a config entry."""
    username     = entry.data[CONF_USERNAME]
    password     = entry.data[CONF_PASSWORD]
    satellite_id = entry.data[CONF_SATELLITE_ID]
    company_id   = entry.data[CONF_COMPANY_ID]

    # Options flow values take priority over initial config values
    scan_interval = entry.options.get(
        CONF_SCAN_INTERVAL,
        entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
    )

    auth = RainBirdAuth(username, password)
    api  = RainBirdAPI(auth)

    coordinator = RainBirdCoordinator(
        hass,
        api,
        satellite_id,
        company_id,
        scan_interval,
    )

    # Fetch initial data — raises ConfigEntryNotReady on failure
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        raise ConfigEntryNotReady(f"Unable to connect to Rain Bird: {err}") from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(async_update_options))

    # Reload entry when options change so new scan_interval takes effect
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when options are updated."""
    await hass.config_entries.async_reload(entry.entry_id)

async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update — reload the integration to apply new settings."""
    await hass.config_entries.async_reload(entry.entry_id)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
