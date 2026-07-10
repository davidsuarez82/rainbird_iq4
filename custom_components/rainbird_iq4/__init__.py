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
from homeassistant.helpers import entity_registry as er

from .api import RainBirdAPI
from .auth import RainBirdAuth
from .const import (
    CONF_AUTH_CHANNEL,
    CONF_COMPANY_ID,
    CONF_PASSWORD,
    CONF_SATELLITE_ID,
    CONF_SCAN_INTERVAL,
    CONF_SCAN_INTERVAL_CONFIG,
    CONF_SCAN_INTERVAL_PROGRAM,
    CONF_USERNAME,
    DEFAULT_AUTH_CHANNEL,
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

SERVICES = [
    "start_zone",
    "stop_zone",
    "stop_all_zones",
    "set_rain_delay",
    "enable_forecast_rain_delay",
    "disable_forecast_rain_delay",
    "set_weather_adjust_automatic",
    "set_weather_adjust_manual",
]


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Register the bundled Lovelace card frontend file."""
    global _FRONTEND_REGISTERED
    if _FRONTEND_REGISTERED:
        return
    await hass.http.async_register_static_paths(
        [StaticPathConfig(FRONTEND_URL, str(FRONTEND_PATH), cache_headers=False)]
    )
    _FRONTEND_REGISTERED = True


# ── Entity resolution via unique_id (stable, no name matching) ────────────────

def _resolve_unique_id(hass: HomeAssistant, entity_id: str, kind: str) -> tuple[str, int]:
    """Resolve an entity_id to (satellite_id, object_id) via the entity registry.

    kind is "station" or "program"; the unique_id format is
    "{satellite_id}_station_{station_id}" / "{satellite_id}_program_{program_id}".
    """
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get(entity_id)
    if not entry:
        raise ServiceValidationError(
            f"Entity {entity_id} not found in the entity registry"
        )

    marker = f"_{kind}_"
    unique_id = entry.unique_id or ""
    if entry.platform != DOMAIN or marker not in unique_id:
        raise ServiceValidationError(
            f"Entity {entity_id} is not a Rain Bird IQ4 {kind} sensor. "
            f"Please select a {kind.capitalize()} sensor."
        )

    satellite_id_str, _, object_id_str = unique_id.rpartition(marker)
    try:
        object_id = int(object_id_str)
    except ValueError:
        raise ServiceValidationError(
            f"Entity {entity_id} has an unexpected unique_id format: {unique_id}"
        )
    return satellite_id_str, object_id


def _coordinators_for_satellite(hass: HomeAssistant, satellite_id_str: str) -> dict:
    """Find the coordinators dict for a given satellite id string."""
    for coordinators in hass.data.get(DOMAIN, {}).values():
        realtime = coordinators.get("realtime")
        if realtime and str(realtime.satellite_id) == satellite_id_str:
            return coordinators
    raise ServiceValidationError(
        f"No active Rain Bird IQ4 controller found for satellite {satellite_id_str}. "
        f"Is the integration loaded?"
    )


def _resolve_station(
    hass: HomeAssistant, entity_id: str
) -> tuple[int, RainBirdAPI, RainBirdCoordinator]:
    """Resolve a station sensor entity_id to (station_id, api, realtime_coordinator)."""
    satellite_id_str, station_id = _resolve_unique_id(hass, entity_id, "station")
    coordinators = _coordinators_for_satellite(hass, satellite_id_str)
    return station_id, coordinators["api"], coordinators["realtime"]


def _resolve_program(
    hass: HomeAssistant, entity_id: str
) -> tuple[int, RainBirdAPI, RainBirdProgramCoordinator]:
    """Resolve a program sensor entity_id to (program_id, api, program_coordinator)."""
    satellite_id_str, program_id = _resolve_unique_id(hass, entity_id, "program")
    coordinators = _coordinators_for_satellite(hass, satellite_id_str)
    return program_id, coordinators["api"], coordinators["program"]


def _resolve_controller(hass: HomeAssistant, entity_id: str | None) -> dict:
    """Return the coordinators dict for a controller-level service call.

    If an entity is provided, any Rain Bird IQ4 entity of that controller works:
    the satellite id is the prefix of every unique_id this integration creates.
    If omitted, fall back to the single configured entry (backwards compatible),
    or raise a clear error when multiple controllers exist.
    """
    if entity_id:
        ent_reg = er.async_get(hass)
        entry = ent_reg.async_get(entity_id)
        if not entry or entry.platform != DOMAIN or not entry.unique_id:
            raise ServiceValidationError(
                f"Entity {entity_id} is not a Rain Bird IQ4 entity."
            )
        satellite_id_str = entry.unique_id.split("_")[0]
        return _coordinators_for_satellite(hass, satellite_id_str)

    entries = list(hass.data.get(DOMAIN, {}).values())
    if len(entries) == 1:
        return entries[0]
    raise ServiceValidationError(
        "Multiple Rain Bird IQ4 controllers are configured. "
        "Please select a controller entity in the service call."
    )


# ── Domain-level service handlers (registered once) ───────────────────────────

async def _handle_start_zone(call: ServiceCall) -> None:
    hass = call.hass
    station_id, api, coordinator = _resolve_station(hass, call.data["station_entity"])
    duration = call.data["duration"]
    await hass.async_add_executor_job(api.start_station, station_id, duration * 60)
    await coordinator.async_request_refresh()


async def _handle_stop_zone(call: ServiceCall) -> None:
    hass = call.hass
    station_id, api, coordinator = _resolve_station(hass, call.data["station_entity"])
    await hass.async_add_executor_job(api.stop_station, station_id)
    await coordinator.async_request_refresh()


async def _handle_stop_all_zones(call: ServiceCall) -> None:
    hass = call.hass
    coordinators = _resolve_controller(hass, call.data.get("controller_entity"))
    api = coordinators["api"]
    realtime = coordinators["realtime"]
    await hass.async_add_executor_job(api.stop_all_stations, realtime.satellite_id)
    await realtime.async_request_refresh()


async def _handle_set_rain_delay(call: ServiceCall) -> None:
    hass = call.hass
    coordinators = _resolve_controller(hass, call.data.get("controller_entity"))
    api = coordinators["api"]
    config_coordinator = coordinators["config"]
    await hass.async_add_executor_job(
        api.set_rain_delay, config_coordinator.satellite_id, call.data["days"]
    )
    await config_coordinator.async_request_refresh()


async def _handle_enable_forecast(call: ServiceCall) -> None:
    hass = call.hass
    coordinators = _resolve_controller(hass, call.data.get("controller_entity"))
    api = coordinators["api"]
    config_coordinator = coordinators["config"]
    await hass.async_add_executor_job(
        api.set_forecast, config_coordinator.satellite_id, True,
        int(call.data["percent"]), float(call.data["rainfall"]), int(call.data["delay_days"]),
    )
    await config_coordinator.async_request_refresh()


async def _handle_disable_forecast(call: ServiceCall) -> None:
    hass = call.hass
    coordinators = _resolve_controller(hass, call.data.get("controller_entity"))
    api = coordinators["api"]
    config_coordinator = coordinators["config"]
    await hass.async_add_executor_job(
        api.set_forecast, config_coordinator.satellite_id, False
    )
    await config_coordinator.async_request_refresh()


async def _handle_weather_adjust_automatic(call: ServiceCall) -> None:
    hass = call.hass
    program_id, api, program_coordinator = _resolve_program(hass, call.data["program_entity"])
    await hass.async_add_executor_job(api.set_weather_adjust_method, program_id, 7)
    await program_coordinator.async_request_refresh()


async def _handle_weather_adjust_manual(call: ServiceCall) -> None:
    hass = call.hass
    program_id, api, program_coordinator = _resolve_program(hass, call.data["program_entity"])
    seasonal_adjust = call.data.get("seasonal_adjust", 100)
    await hass.async_add_executor_job(api.set_weather_adjust_method, program_id, 6)
    await hass.async_add_executor_job(api.set_seasonal_adjust, program_id, seasonal_adjust)
    await program_coordinator.async_request_refresh()


_SERVICE_HANDLERS = {
    "start_zone": _handle_start_zone,
    "stop_zone": _handle_stop_zone,
    "stop_all_zones": _handle_stop_all_zones,
    "set_rain_delay": _handle_set_rain_delay,
    "enable_forecast_rain_delay": _handle_enable_forecast,
    "disable_forecast_rain_delay": _handle_disable_forecast,
    "set_weather_adjust_automatic": _handle_weather_adjust_automatic,
    "set_weather_adjust_manual": _handle_weather_adjust_manual,
}


def _async_register_services(hass: HomeAssistant) -> None:
    """Register domain services once, regardless of how many entries exist."""
    for service, handler in _SERVICE_HANDLERS.items():
        if not hass.services.has_service(DOMAIN, service):
            hass.services.async_register(DOMAIN, service, handler)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Rain Bird IQ4 from a config entry."""
    username     = entry.data[CONF_USERNAME]
    password     = entry.data[CONF_PASSWORD]
    satellite_id = entry.data[CONF_SATELLITE_ID]
    company_id   = entry.data[CONF_COMPANY_ID]

    scan_realtime = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    scan_config   = entry.options.get(CONF_SCAN_INTERVAL_CONFIG, DEFAULT_SCAN_INTERVAL_CONFIG)
    scan_program  = entry.options.get(CONF_SCAN_INTERVAL_PROGRAM, DEFAULT_SCAN_INTERVAL_PROGRAM)

    # Auth channel: chosen at install (entry.data), optionally overridden
    # later via the options flow (entry.options takes precedence).
    auth_channel = entry.options.get(
        CONF_AUTH_CHANNEL,
        entry.data.get(CONF_AUTH_CHANNEL, DEFAULT_AUTH_CHANNEL),
    )

    auth = RainBirdAuth(hass, username, password, channel=auth_channel)
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

    _async_register_services(hass)

    return True


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update — reload the integration."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinators = hass.data[DOMAIN].pop(entry.entry_id)
        api = coordinators.get("api")
        if api:
            await hass.async_add_executor_job(api.close)
        # Only remove domain services when the last loaded entry goes away
        if not hass.data[DOMAIN]:
            for service in SERVICES:
                hass.services.async_remove(DOMAIN, service)
    return unload_ok
