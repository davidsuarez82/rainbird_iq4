"""Rain Bird IQ4 number platform — Rain Delay, Seasonal Adjust and Station Duration."""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import RainBirdCoordinator

_LOGGER = logging.getLogger(__name__)

# Default manual run duration in minutes
DEFAULT_STATION_DURATION = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Rain Bird IQ4 number entities from a config entry."""
    coordinator: RainBirdCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [RainBirdRainDelay(coordinator)]

    for station in coordinator.data.get("stations", []):
        entities.append(RainBirdStationDuration(coordinator, station))

    for program in coordinator.data.get("programs", []):
        entities.append(RainBirdSeasonalAdjust(coordinator, program))

    async_add_entities(entities)


class RainBirdBaseNumber(CoordinatorEntity, NumberEntity):
    """Base class for Rain Bird IQ4 number entities."""

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


class RainBirdRainDelay(RainBirdBaseNumber):
    """Number entity representing the Rain Delay setting in days."""

    def __init__(self, coordinator: RainBirdCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._satellite_id}_rain_delay"
        self._attr_name = f"{self._satellite_name} Rain Delay"
        self._attr_icon = "mdi:weather-rainy"
        self._attr_native_min_value = 0
        self._attr_native_max_value = 14
        self._attr_native_step = 1
        self._attr_native_unit_of_measurement = "days"
        self._attr_mode = NumberMode.SLIDER
        self._attr_native_precision = 0

    @property
    def native_value(self) -> int:
        """Return current rain delay in days."""
        return self.coordinator.data.get("connection", {}).get("rainDelayDaysRemaining", 0)

    async def async_set_native_value(self, value: float) -> None:
        """Set rain delay in days."""
        await self.hass.async_add_executor_job(
            self.coordinator.api.set_rain_delay,
            self.coordinator.satellite_id,
            int(value),
        )
        await self.coordinator.async_request_refresh()


class RainBirdStationDuration(RainBirdBaseNumber):
    """
    Number entity for manual irrigation duration per station.

    Sets how many minutes the station runs when activated via switch.
    Range: 1-30 minutes in steps of 1 minute.
    Default: 1 minute (safety default to prevent accidental long runs).
    Value is stored locally — not sent to the Rain Bird API.
    """

    def __init__(self, coordinator: RainBirdCoordinator, station: dict) -> None:
        super().__init__(coordinator)
        self._station_id = station["id"]
        self._attr_unique_id = f"{self._satellite_id}_duration_{self._station_id}"
        self._attr_name = f"{self._satellite_name} {station['name']} Duration"
        self._attr_icon = "mdi:timer-outline"
        self._attr_native_min_value = 1
        self._attr_native_max_value = 30
        self._attr_native_step = 1
        self._attr_native_unit_of_measurement = "min"
        self._attr_mode = NumberMode.BOX
        self._attr_native_precision = 0
        self._duration = DEFAULT_STATION_DURATION

    @property
    def native_value(self) -> int:
        """Return current duration in minutes."""
        return self._duration

    async def async_set_native_value(self, value: float) -> None:
        """Store duration locally — used by the station switch on activation."""
        self._duration = int(value)
        self.async_write_ha_state()


class RainBirdSeasonalAdjust(RainBirdBaseNumber):
    """
    Number entity for seasonal adjust percentage per program.

    Shows as slider when weather adjust method is manual (etAdjustType=6).
    Shows as box (read-only behaviour) when method is automatic (etAdjustType=7).
    Only available when the program is scheduled.
    """

    def __init__(self, coordinator: RainBirdCoordinator, program: dict) -> None:
        super().__init__(coordinator)
        self._program_id = program["id"]
        self._attr_unique_id = f"{self._satellite_id}_seasonal_adjust_{self._program_id}"
        self._attr_name = f"{self._satellite_name} Program {program['shortName']} Seasonal Adjust"
        self._attr_icon = "mdi:chart-line"
        self._attr_native_min_value = 5
        self._attr_native_max_value = 200
        self._attr_native_step = 5
        self._attr_native_unit_of_measurement = "%"

    def _get_program(self) -> dict:
        for p in self.coordinator.data.get("programs", []):
            if p["id"] == self._program_id:
                return p
        return {}

    def _is_program_scheduled(self) -> bool:
        """Return True if the program has scheduled days and steps."""
        program = self._get_program()
        return bool(program.get("weekDays")) and program.get("steps", 0) > 0

    @property
    def mode(self) -> NumberMode:
        """Use slider when manual, box when automatic."""
        program = self._get_program()
        if program.get("etAdjustType", 6) != 6:
            return NumberMode.BOX
        return NumberMode.SLIDER

    @property
    def available(self) -> bool:
        """Available when program is scheduled, regardless of adjust method."""
        return self._is_program_scheduled()

    @property
    def native_value(self) -> int:
        return self._get_program().get("adjust", 100)

    async def async_set_native_value(self, value: float) -> None:
        """Set seasonal adjust percentage — only when method is manual."""
        program = self._get_program()
        if program.get("etAdjustType", 6) != 6:
            _LOGGER.warning("Cannot set seasonal adjust when method is automatic")
            await self.coordinator.async_request_refresh()
            return
        await self.hass.async_add_executor_job(
            self.coordinator.api.set_seasonal_adjust,
            self._program_id,
            int(value),
        )
        await self.coordinator.async_request_refresh()
