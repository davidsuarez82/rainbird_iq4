"""Rain Bird IQ4 sensor platform."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import RainBirdCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Rain Bird IQ4 sensors from a config entry."""
    coordinator: RainBirdCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = []

    # Controller sensors
    entities.append(RainBirdControllerModeSensor(coordinator))
    entities.append(RainBirdAlarmSensor(coordinator))
    entities.append(RainBirdWarningSensor(coordinator))

    # One sensor per station
    for station in coordinator.data.get("stations", []):
        entities.append(RainBirdStationSensor(coordinator, station))

    # One sensor per program
    for program in coordinator.data.get("programs", []):
        entities.append(RainBirdProgramSensor(coordinator, program))

    async_add_entities(entities)


class RainBirdBaseSensor(CoordinatorEntity, SensorEntity):
    """Base class for Rain Bird IQ4 sensors."""

    def __init__(self, coordinator: RainBirdCoordinator) -> None:
        super().__init__(coordinator)
        satellite = coordinator.data.get("satellite", {})
        self._satellite_id = coordinator.satellite_id
        self._satellite_name = satellite.get("name", "Rain Bird IQ4")

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info — all entities share one device."""
        satellite = self.coordinator.data.get("satellite", {})
        return DeviceInfo(
            identifiers={(DOMAIN, str(self._satellite_id))},
            name=self._satellite_name,
            manufacturer="Rain Bird",
            model="ESP-TM2",
            sw_version=satellite.get("version"),
        )


class RainBirdAlarmSensor(RainBirdBaseSensor):
    """Sensor reporting number of unacknowledged alarms."""

    def __init__(self, coordinator: RainBirdCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._satellite_id}_alarms"
        self._attr_name = f"{self._satellite_name} Alarms"
        self._attr_icon = "mdi:alarm-light"
        self._attr_native_unit_of_measurement = "alarms"

    @property
    def native_value(self) -> int:
        return self.coordinator.data.get("alerts", {}).get("alarms", 0)


class RainBirdWarningSensor(RainBirdBaseSensor):
    """Sensor reporting number of unacknowledged warnings."""

    def __init__(self, coordinator: RainBirdCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._satellite_id}_warnings"
        self._attr_name = f"{self._satellite_name} Warnings"
        self._attr_icon = "mdi:alert"
        self._attr_native_unit_of_measurement = "warnings"

    @property
    def native_value(self) -> int:
        return self.coordinator.data.get("alerts", {}).get("warnings", 0)


class RainBirdStationSensor(RainBirdBaseSensor):
    """Sensor reporting the current status of a single irrigation zone."""

    def __init__(self, coordinator: RainBirdCoordinator, station: dict) -> None:
        super().__init__(coordinator)
        self._station_id = station["id"]
        self._attr_unique_id = f"{self._satellite_id}_station_{self._station_id}"
        self._attr_name = f"{self._satellite_name} {station['name']}"
        self._attr_icon = "mdi:sprinkler"

    def _get_station(self) -> dict:
        for s in self.coordinator.data.get("stations", []):
            if s["id"] == self._station_id:
                return s
        return {}

    @property
    def native_value(self) -> str:
        station = self._get_station()
        if station.get("isRunning"):
            return "running"
        status = station.get("status", "-")
        if status == "R":
            return "running"
        if status == "P":
            return "paused"
        return "idle"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        station = self._get_station()
        return {
            "terminal":           station.get("terminal"),
            "remaining":          station.get("remaining"),
            "programs":           station.get("programs", []),
            "last_run":           station.get("lastRun"),
            "last_run_completed": station.get("lastRunCompleted"),
        }


class RainBirdProgramSensor(RainBirdBaseSensor):
    """Sensor reporting the configuration of an irrigation program."""

    def __init__(self, coordinator: RainBirdCoordinator, program: dict) -> None:
        super().__init__(coordinator)
        self._program_id = program["id"]
        self._attr_unique_id = f"{self._satellite_id}_program_{self._program_id}"
        self._attr_name = f"{self._satellite_name} Program {program['shortName']} Status"
        self._attr_icon = "mdi:calendar-clock"

    def _get_program(self) -> dict:
        for p in self.coordinator.data.get("programs", []):
            if p["id"] == self._program_id:
                return p
        return {}

    @property
    def native_value(self) -> str:
        program = self._get_program()
        if not program.get("isEnabled"):
            return "disabled"
        if not program.get("weekDays"):
            return "not scheduled"
        return "scheduled"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        program = self._get_program()
        return {
            "start_time": program.get("startTime"),
            "week_days":  program.get("weekDays", []),
            "adjust":     program.get("adjust"),
            "steps":      program.get("steps"),
        }


class RainBirdControllerModeSensor(RainBirdBaseSensor):
    """Sensor reporting the controller operating mode."""

    MODES = {1: "off", 2: "auto"}

    def __init__(self, coordinator: RainBirdCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._satellite_id}_controller_mode"
        self._attr_name = f"{self._satellite_name} Controller Mode"
        self._attr_icon = "mdi:controller"

    @property
    def native_value(self) -> str:
        mode = self.coordinator.data.get("satellite", {}).get("systemMode")
        return self.MODES.get(mode, "unknown")
