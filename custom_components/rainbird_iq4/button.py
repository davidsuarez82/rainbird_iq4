"""Rain Bird IQ4 button platform — manual refresh."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import RainBirdCoordinator, RainBirdConfigCoordinator, RainBirdProgramCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Rain Bird IQ4 button from a config entry."""
    coordinators = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([RainBirdRefreshButton(coordinators)])


class RainBirdRefreshButton(ButtonEntity):
    """Button to force a refresh of all Rain Bird coordinators."""

    def __init__(self, coordinators: dict) -> None:
        self._coordinators = coordinators
        coordinator = coordinators["realtime"]
        satellite = coordinator.data.get("satellite", {}) if coordinator.data else {}
        satellite_id = coordinator.satellite_id
        satellite_name = satellite.get("name", "Rain Bird IQ4")
        self._attr_unique_id = f"{satellite_id}_refresh"
        self._attr_name = f"{satellite_name} Refresh"
        self._attr_icon = "mdi:refresh"

    @property
    def device_info(self) -> DeviceInfo:
        coordinator = self._coordinators["realtime"]
        config_coordinator = self._coordinators["config"]
        satellite = config_coordinator.data.get("satellite", {}) if config_coordinator.data else {}
        return DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.satellite_id))},
            name=satellite.get("name", "Rain Bird IQ4"),
            manufacturer="Rain Bird",
            model=satellite.get("model", "Rain Bird IQ4"),
            sw_version=satellite.get("version"),
        )

    async def async_press(self) -> None:
        """Force refresh all coordinators."""
        _LOGGER.debug("Manual refresh triggered")
        for key, coordinator in self._coordinators.items():
            if key in ("realtime", "config", "program"):
                await coordinator.async_request_refresh()
