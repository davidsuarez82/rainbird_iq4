"""Rain Bird IQ4 integration for Home Assistant."""
from __future__ import annotations

import logging
from pathlib import Path

import voluptuous as vol

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv

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
from .coordinator import RainBirdCoordinator, RainBirdConfigCoordinator, RainBirdProgramCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "binary_sensor", "calendar", "button"]
FRONTEND_URL = f"/{DOMAIN}/rainbird_iq4_card.js"
FRONTEND_PATH = Path(__file__).parent / "frontend" / "rainbird_iq4_card.js"
_FRONTEND_REGISTERED = False


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Register the bundled Lovelace card frontend file."""
    global _FRONTEND_REGISTERED
    if _FRONTEND_REGISTERED:
        return
    await hass.http.async_register_static_paths(
        [StaticPathConfig(FRONTEND_URL, str(FRONTEND_PATH), cache_headers=False)]
    )
    _FRONTEND_REGISTERED = True


def _resolve_station(hass: HomeAssistant, entity_id: str) -> tuple[int, Any, Any]:
    """Resolve a station sensor entity_id to its Rain Bird station_id, api and coordinator.
    
    Searches across all configured Rain Bird entries to find the correct one.
    Returns (station_id, api, realtime_coordinator).
    """
    state = hass.states.get(entity_id)
    if not state:
        raise ServiceValidationError(f"Entity {entity_id} not found")
    friendly_name = state.attributes.get("friendly_name", "")
    
    for entry_id, coordinators in hass.data[DOMAIN].items():
        realtime = coordinators["realtime"]
        config   = coordinators["config"]
        if not realtime.data or not config.data:
            continue
        satellite_name = config.data.get("satellite", {}).get("name", "")
        for station in realtime.data.get("stations", []):
            if friendly_name == f"{satellite_name} {station['name']}":
                return station["id"], coordinators["api"], realtime
    
    raise ServiceValidationError(
        f"Could not match entity {entity_id} to a station. "
        f"Please select a Station sensor (e.g. ESP-TM2 Station 001)."
    )


def _resolve_program(hass: HomeAssistant, program_coordinator: RainBirdProgramCoordinator, entity_id: str) -> int:
    """Resolve a program sensor entity_id to its Rain Bird program_id."""
    state = hass.states.get(entity_id)
    if not state:
        raise ServiceValidationError(f"Entity {entity_id} not found")
    friendly_name = state.attributes.get("friendly_name", "")
    for program in program_coordinator.data.get("programs", []):
        # Match against Program X Status name
        for entry_id, coordinators in hass.data[DOMAIN].items():
            satellite_name = coordinators["config"].data.get("satellite", {}).get("name", "") if coordinators["config"].data else ""
            if friendly_name == f"{satellite_name} Program {program['shortName']} Status":
                return program["id"]
    raise ServiceValidationError(
        f"Could not match entity {entity_id} to a program. "
        f"Please select a Program Status sensor."
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Rain Bird IQ4 from a config entry."""
    username     = entry.data[CONF_USERNAME]
    password     = entry.data[CONF_PASSWORD]
    satellite_id = entry.data[CONF_SATELLITE_ID]
    company_id   = entry.data[CONF_COMPANY_ID]

    scan_realtime = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    scan_config   = entry.options.get(CONF_SCAN_INTERVAL_CONFIG, DEFAULT_SCAN_INTERVAL_CONFIG)
    scan_program  = entry.options.get(CONF_SCAN_INTERVAL_PROGRAM, DEFAULT_SCAN_INTERVAL_PROGRAM)

    auth = RainBirdAuth(username, password)
    api  = RainBirdAPI(auth)

    coordinator         = RainBirdCoordinator(hass, api, satellite_id, company_id, scan_realtime)
    config_coordinator  = RainBirdConfigCoordinator(hass, api, satellite_id, scan_config)
    program_coordinator = RainBirdProgramCoordinator(hass, api, satellite_id, scan_program)

    try:
        await coordinator.async_config_entry_first_refresh()
        await config_coordinator.async_config_entry_first_refresh()
        await program_coordinator.async_config_entry_first_refresh()
    except Exception as err:
        raise ConfigEntryNotReady(f"Unable to connect to Rain Bird: {err}") from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "realtime": coordinator,
        "config":   config_coordinator,
        "program":  program_coordinator,
        "api":      api,
    }

    await _async_register_frontend(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    # ── Service handlers ──────────────────────────────────────────────────────

    async def handle_start_zone(call: ServiceCall) -> None:
        entity_id = call.data["station_entity"]
        duration  = call.data["duration"]
        station_id, entry_api, entry_coordinator = _resolve_station(hass, entity_id)
        await hass.async_add_executor_job(entry_api.start_station, station_id, duration * 60)
        await entry_coordinator.async_request_refresh()

    async def handle_stop_zone(call: ServiceCall) -> None:
        entity_id = call.data["station_entity"]
        station_id, entry_api, entry_coordinator = _resolve_station(hass, entity_id)
        await hass.async_add_executor_job(entry_api.stop_station, station_id)
        await entry_coordinator.async_request_refresh()

    async def handle_set_rain_delay(call: ServiceCall) -> None:
        await hass.async_add_executor_job(api.set_rain_delay, satellite_id, call.data["days"])
        await config_coordinator.async_request_refresh()

    async def handle_enable_forecast(call: ServiceCall) -> None:
        await hass.async_add_executor_job(
            api.set_forecast, satellite_id, True,
            int(call.data["percent"]), float(call.data["rainfall"]), int(call.data["delay_days"]),
        )
        await config_coordinator.async_request_refresh()

    async def handle_disable_forecast(call: ServiceCall) -> None:
        await hass.async_add_executor_job(api.set_forecast, satellite_id, False)
        await config_coordinator.async_request_refresh()

    async def handle_weather_adjust_automatic(call: ServiceCall) -> None:
        entity_id = call.data["program_entity"]
        state = hass.states.get(entity_id)
        if not state:
            raise ServiceValidationError(f"Entity {entity_id} not found")
        friendly_name  = state.attributes.get("friendly_name", "")
        satellite_name = config_coordinator.data.get("satellite", {}).get("name", "")
        program_id = None
        for program in program_coordinator.data.get("programs", []):
            if friendly_name == f"{satellite_name} Program {program['shortName']} Status":
                program_id = program["id"]
                break
        if not program_id:
            raise ServiceValidationError(f"Could not match entity {entity_id} to a program.")
        await hass.async_add_executor_job(api.set_weather_adjust_method, program_id, 7)
        await program_coordinator.async_request_refresh()

    async def handle_weather_adjust_manual(call: ServiceCall) -> None:
        entity_id = call.data["program_entity"]
        state = hass.states.get(entity_id)
        if not state:
            raise ServiceValidationError(f"Entity {entity_id} not found")
        friendly_name  = state.attributes.get("friendly_name", "")
        satellite_name = config_coordinator.data.get("satellite", {}).get("name", "")
        program_id = None
        for program in program_coordinator.data.get("programs", []):
            if friendly_name == f"{satellite_name} Program {program['shortName']} Status":
                program_id = program["id"]
                break
        if not program_id:
            raise ServiceValidationError(f"Could not match entity {entity_id} to a program.")
        seasonal_adjust = call.data.get("seasonal_adjust", 100)
        await hass.async_add_executor_job(api.set_weather_adjust_method, program_id, 6)
        await hass.async_add_executor_job(api.set_seasonal_adjust, program_id, seasonal_adjust)
        await program_coordinator.async_request_refresh()

    hass.services.async_register(DOMAIN, "start_zone", handle_start_zone)
    hass.services.async_register(DOMAIN, "stop_zone", handle_stop_zone)
    hass.services.async_register(DOMAIN, "set_rain_delay", handle_set_rain_delay)
    hass.services.async_register(DOMAIN, "enable_forecast_rain_delay", handle_enable_forecast)
    hass.services.async_register(DOMAIN, "disable_forecast_rain_delay", handle_disable_forecast)
    hass.services.async_register(DOMAIN, "set_weather_adjust_automatic", handle_weather_adjust_automatic)
    hass.services.async_register(DOMAIN, "set_weather_adjust_manual", handle_weather_adjust_manual)

    return True


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update — reload the integration."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    for service in [
        "start_zone", "stop_zone", "set_rain_delay",
        "enable_forecast_rain_delay", "disable_forecast_rain_delay",
        "set_weather_adjust_automatic", "set_weather_adjust_manual",
    ]:
        hass.services.async_remove(DOMAIN, service)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
