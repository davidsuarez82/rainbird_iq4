"""Rain Bird IQ4 switch platform — manual zone control and forecast rain delay."""
from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import RainBirdCoordinator

_LOGGER = logging.getLogger(__name__)

# Fallback duration if number entity is not found
DEFAULT_RUN_SECONDS = 60  # 1 minute safety default

# Default forecast parameters when enabling for the first time
DEFAULT_FORECAST_PERCENT = 70
DEFAULT_FORECAST_INCHES = 0.5
DEFAULT_FORECAST_DELAY_DAYS = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Rain Bird IQ4 switches from a config entry."""
    coordinator: RainBirdCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        RainBirdForecastSwitch(coordinator),
    ]

    for station in coordinator.data.get("stations", []):
        entities.append(RainBirdStationSwitch(coordinator, station))

    async_add_entities(entities)


class RainBirdBaseSwitch(CoordinatorEntity, SwitchEntity):
    """Base class for Rain Bird IQ4 switches."""

    def __init__(self, coordinator: RainBirdCoordinator) -> None:
        super().__init__(coordinator)
        satellite = coordinator.data.get("satellite", {})
        self._satellite_id = coordinator.satellite_id
        self._satellite_name = satellite.get("name", "Rain Bird IQ4")

    @property
    def device_info(self) -> DeviceInfo:
        satellite = self.coordinator.data.get("satellite", {})
        return DeviceInfo(
            identifiers={(DOMAIN, str(self._satellite_id))},
            name=self._satellite_name,
            manufacturer="Rain Bird",
            model="ESP-TM2",
            sw_version=satellite.get("version"),
        )


class RainBirdForecastSwitch(RainBirdBaseSwitch):
    """Switch entity to enable/disable forecast rain delay."""

    def __init__(self, coordinator: RainBirdCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._satellite_id}_forecast"
        self._attr_name = f"{self._satellite_name} Forecast Rain Delay"
        self._attr_icon = "mdi:weather-lightning-rainy"
        self._optimistic_state: bool | None = None

    @property
    def is_on(self) -> bool:
        """Return True if forecast is enabled, using optimistic state while pending."""
        if self._optimistic_state is not None:
            return self._optimistic_state
        return self.coordinator.data.get("forecast", {}).get("enabled", False)

    def _handle_coordinator_update(self) -> None:
        """Clear optimistic state when coordinator updates."""
        self._optimistic_state = None
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs) -> None:
        """Enable forecast rain delay with current or default parameters."""
        self._optimistic_state = True
        self.async_write_ha_state()
        forecast = self.coordinator.data.get("forecast", {})
        await self.hass.async_add_executor_job(
            self.coordinator.api.set_forecast,
            self.coordinator.satellite_id,
            True,
            forecast.get("percent") or DEFAULT_FORECAST_PERCENT,
            forecast.get("inches") or DEFAULT_FORECAST_INCHES,
            forecast.get("delayDays") or DEFAULT_FORECAST_DELAY_DAYS,
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        """Disable forecast rain delay."""
        self._optimistic_state = False
        self.async_write_ha_state()
        await self.hass.async_add_executor_job(
            self.coordinator.api.set_forecast,
            self.coordinator.satellite_id,
            False,
        )
        await self.coordinator.async_request_refresh()


class RainBirdStationSwitch(RainBirdBaseSwitch):
    """
    Switch entity for manual control of a single irrigation zone.

    Uses optimistic state — assumes the command was accepted and reflects
    the new state immediately without waiting for API confirmation.
    Turning on starts the zone for the duration set in the companion number entity.
    Turning off stops the zone immediately.
    """

    def __init__(
        self, coordinator: RainBirdCoordinator, station: dict
    ) -> None:
        super().__init__(coordinator)
        self._station_id = station["id"]
        self._attr_unique_id = f"{self._satellite_id}_switch_{self._station_id}"
        self._attr_name = f"{self._satellite_name} {station['name']}"
        self._attr_icon = "mdi:sprinkler"
        self._is_on = False

    def _get_station(self) -> dict:
        for s in self.coordinator.data.get("stations", []):
            if s["id"] == self._station_id:
                return s
        return {}

    def _get_duration_seconds(self) -> int:
        """Get run duration in seconds from companion number entity."""
        entity_id = (
            f"number.{self._satellite_name.lower().replace(' ', '_')}"
            f"_{self._get_station().get('name', '').lower().replace(' ', '_')}_duration"
        )
        state = self.hass.states.get(entity_id)
        if state and state.state not in ("unknown", "unavailable"):
            try:
                return int(float(state.state)) * 60
            except ValueError:
                pass
        return DEFAULT_RUN_SECONDS

    @property
    def is_on(self) -> bool:
        """
        Return True if the station is on.

        Prefers the live API state when available (isRunning),
        otherwise falls back to the optimistic local state.
        """
        station = self._get_station()
        if station.get("isRunning"):
            self._is_on = True
        return self._is_on

    @property
    def extra_state_attributes(self) -> dict:
        station = self._get_station()
        return {
            "terminal":  station.get("terminal"),
            "remaining": station.get("remaining"),
            "programs":  station.get("programs", []),
        }

    async def async_turn_on(self, **kwargs) -> None:
        """Start the irrigation zone — update state optimistically."""
        seconds = self._get_duration_seconds()
        _LOGGER.debug(
            "Starting station %s for %s seconds",
            self._station_id, seconds
        )
        self._is_on = True
        self.async_write_ha_state()
        await self.hass.async_add_executor_job(
            self.coordinator.api.start_station,
            self._station_id,
            seconds,
        )

    async def async_turn_off(self, **kwargs) -> None:
        """Stop the irrigation zone — update state optimistically."""
        _LOGGER.debug("Stopping station %s", self._station_id)
        self._is_on = False
        self.async_write_ha_state()
        await self.hass.async_add_executor_job(
            self.coordinator.api.stop_station,
            self._station_id,
        )
        await self.coordinator.async_request_refresh()
