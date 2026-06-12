"""Rain Bird IQ4 sensor platform."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import RainBirdCoordinator, RainBirdConfigCoordinator, RainBirdProgramCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Rain Bird IQ4 sensors from a config entry."""
    coordinators        = hass.data[DOMAIN][entry.entry_id]
    coordinator         = coordinators["realtime"]
    config_coordinator  = coordinators["config"]
    program_coordinator = coordinators["program"]

    entities: list[SensorEntity] = []

    # Config-based sensors (5 min polling)
    entities.append(RainBirdControllerModeSensor(config_coordinator))
    entities.append(RainBirdAlarmSensor(coordinator))
    entities.append(RainBirdWarningSensor(coordinator))
    entities.append(RainBirdRainDelaySensor(config_coordinator))

    # Real-time sensors (30s polling)
    for station in coordinator.data.get("stations", []):
        entities.append(RainBirdStationSensor(coordinator, config_coordinator, station))

    # Program sensors (1h polling)
    for program in program_coordinator.data.get("programs", []):
        entities.append(RainBirdProgramSensor(program_coordinator, config_coordinator, program))

    async_add_entities(entities)


class RainBirdBaseSensor(CoordinatorEntity, SensorEntity):
    """Base class for Rain Bird IQ4 sensors."""

    def __init__(self, coordinator, config_coordinator: RainBirdConfigCoordinator) -> None:
        super().__init__(coordinator)
        self._config_coordinator = config_coordinator
        satellite = config_coordinator.data.get("satellite", {}) if config_coordinator.data else {}
        self._satellite_id = coordinator.satellite_id if hasattr(coordinator, 'satellite_id') else config_coordinator.satellite_id
        self._satellite_name = satellite.get("name", "Rain Bird IQ4")

    @property
    def device_info(self) -> DeviceInfo:
        satellite = self._config_coordinator.data.get("satellite", {}) if self._config_coordinator.data else {}
        return DeviceInfo(
            identifiers={(DOMAIN, str(self._satellite_id))},
            name=self._satellite_name,
            manufacturer="Rain Bird",
            model="ESP-TM2",
            sw_version=satellite.get("version"),
        )


class RainBirdAlarmSensor(CoordinatorEntity, SensorEntity):
    """Sensor reporting number of unacknowledged alarms — real-time."""

    def __init__(self, coordinator: RainBirdCoordinator) -> None:
        super().__init__(coordinator)
        self._satellite_id = coordinator.satellite_id
        self._attr_unique_id = f"{self._satellite_id}_alarms"
        self._attr_icon = "mdi:alarm-light"
        self._attr_native_unit_of_measurement = "alarms"

    @property
    def name(self) -> str:
        return f"{self.coordinator.data.get('satellite', {}).get('name', 'Rain Bird IQ4') if hasattr(self.coordinator, 'data') and self.coordinator.data else 'Rain Bird IQ4'} Alarms"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, str(self._satellite_id))})

    @property
    def native_value(self) -> int:
        return self.coordinator.data.get("alerts", {}).get("alarms", 0)


class RainBirdWarningSensor(CoordinatorEntity, SensorEntity):
    """Sensor reporting number of unacknowledged warnings — real-time."""

    def __init__(self, coordinator: RainBirdCoordinator) -> None:
        super().__init__(coordinator)
        self._satellite_id = coordinator.satellite_id
        self._attr_unique_id = f"{self._satellite_id}_warnings"
        self._attr_icon = "mdi:alert"
        self._attr_native_unit_of_measurement = "warnings"

    @property
    def name(self) -> str:
        return f"Rain Bird IQ4 Warnings"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, str(self._satellite_id))})

    @property
    def native_value(self) -> int:
        return self.coordinator.data.get("alerts", {}).get("warnings", 0)


class RainBirdRainDelaySensor(CoordinatorEntity, SensorEntity):
    """Sensor reporting current rain delay in days — config polling."""

    def __init__(self, coordinator: RainBirdConfigCoordinator) -> None:
        super().__init__(coordinator)
        self._satellite_id = coordinator.satellite_id
        satellite = coordinator.data.get("satellite", {}) if coordinator.data else {}
        self._satellite_name = satellite.get("name", "Rain Bird IQ4")
        self._attr_unique_id = f"{self._satellite_id}_rain_delay_sensor"
        self._attr_name = f"{self._satellite_name} Rain Delay"
        self._attr_icon = "mdi:weather-rainy"
        self._attr_native_unit_of_measurement = "days"

    @property
    def device_info(self) -> DeviceInfo:
        satellite = self.coordinator.data.get("satellite", {}) if self.coordinator.data else {}
        return DeviceInfo(
            identifiers={(DOMAIN, str(self._satellite_id))},
            name=satellite.get("name", "Rain Bird IQ4"),
            manufacturer="Rain Bird",
            model="ESP-TM2",
            sw_version=satellite.get("version"),
        )

    @property
    def native_value(self) -> int:
        return self.coordinator.data.get("connection", {}).get("rainDelayDaysRemaining", 0)


class RainBirdStationSensor(SensorEntity):
    """Sensor reporting the current status of a single irrigation zone — real-time."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["idle", "running", "paused"]

    def __init__(
        self,
        coordinator: RainBirdCoordinator,
        config_coordinator: RainBirdConfigCoordinator,
        station: dict,
    ) -> None:
        self._coordinator = coordinator
        self._config_coordinator = config_coordinator
        self._station_id = station["id"]
        satellite = config_coordinator.data.get("satellite", {}) if config_coordinator.data else {}
        self._satellite_id = coordinator.satellite_id
        self._satellite_name = satellite.get("name", "Rain Bird IQ4")
        self._attr_unique_id = f"{self._satellite_id}_station_{self._station_id}"
        self._attr_name = f"{self._satellite_name} {station['name']}"
        self._attr_icon = "mdi:sprinkler"

    @property
    def device_info(self) -> DeviceInfo:
        satellite = self._config_coordinator.data.get("satellite", {}) if self._config_coordinator.data else {}
        return DeviceInfo(
            identifiers={(DOMAIN, str(self._satellite_id))},
            name=self._satellite_name,
            manufacturer="Rain Bird",
            model="ESP-TM2",
            sw_version=satellite.get("version"),
        )

    def _get_station(self) -> dict:
        if not self._coordinator.data:
            return {}
        for s in self._coordinator.data.get("stations", []):
            if s["id"] == self._station_id:
                return s
        return {}

    @property
    def state(self) -> str:
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
    def native_value(self) -> str:
        return self.state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        station = self._get_station()
        return {
            "terminal":           station.get("terminal"),
            "remaining":          station.get("remaining"),
            "last_run":           station.get("lastRun"),
            "last_run_completed": station.get("lastRunCompleted"),
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to coordinator updates."""
        self.async_on_remove(
            self._coordinator.async_add_listener(self.async_write_ha_state)
        )
        self.async_on_remove(
            self._config_coordinator.async_add_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        return self._coordinator.last_update_success


class RainBirdProgramSensor(SensorEntity):
    """Sensor reporting the configuration of an irrigation program — program polling."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["scheduled", "not scheduled", "disabled"]

    def __init__(
        self,
        coordinator: RainBirdProgramCoordinator,
        config_coordinator: RainBirdConfigCoordinator,
        program: dict,
    ) -> None:
        self._coordinator = coordinator
        self._config_coordinator = config_coordinator
        self._program_id = program["id"]
        satellite = config_coordinator.data.get("satellite", {}) if config_coordinator.data else {}
        self._satellite_id = coordinator.satellite_id
        self._satellite_name = satellite.get("name", "Rain Bird IQ4")
        self._attr_unique_id = f"{self._satellite_id}_program_{self._program_id}"
        self._attr_name = f"{self._satellite_name} Program {program['shortName']} Status"
        self._attr_icon = "mdi:calendar-clock"

    @property
    def device_info(self) -> DeviceInfo:
        satellite = self._config_coordinator.data.get("satellite", {}) if self._config_coordinator.data else {}
        return DeviceInfo(
            identifiers={(DOMAIN, str(self._satellite_id))},
            name=self._satellite_name,
            manufacturer="Rain Bird",
            model="ESP-TM2",
            sw_version=satellite.get("version"),
        )

    def _get_program(self) -> dict:
        if not self._coordinator.data:
            return {}
        for p in self._coordinator.data.get("programs", []):
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
        et_type = program.get("etAdjustType", 6)
        return {
            "start_time":      program.get("startTime"),
            "week_days":       program.get("weekDays", []),
            "steps":           program.get("steps"),
            "weather_adjust":  "automatic" if et_type == 7 else "none",
            "seasonal_adjust": program.get("adjustedValue"),
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to coordinator updates."""
        self.async_on_remove(
            self._coordinator.async_add_listener(self.async_write_ha_state)
        )
        self.async_on_remove(
            self._config_coordinator.async_add_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        return self._coordinator.last_update_success


class RainBirdControllerModeSensor(CoordinatorEntity, SensorEntity):
    """Sensor reporting the controller operating mode — config polling."""

    MODES = {1: "off", 2: "auto"}

    def __init__(self, coordinator: RainBirdConfigCoordinator) -> None:
        super().__init__(coordinator)
        self._satellite_id = coordinator.satellite_id
        satellite = coordinator.data.get("satellite", {}) if coordinator.data else {}
        self._satellite_name = satellite.get("name", "Rain Bird IQ4")
        self._attr_unique_id = f"{self._satellite_id}_controller_mode"
        self._attr_name = f"{self._satellite_name} Controller Mode"
        self._attr_icon = "mdi:controller"

    @property
    def device_info(self) -> DeviceInfo:
        satellite = self.coordinator.data.get("satellite", {}) if self.coordinator.data else {}
        return DeviceInfo(
            identifiers={(DOMAIN, str(self._satellite_id))},
            name=self._satellite_name,
            manufacturer="Rain Bird",
            model="ESP-TM2",
            sw_version=satellite.get("version"),
        )

    @property
    def native_value(self) -> str:
        mode = self.coordinator.data.get("satellite", {}).get("systemMode") if self.coordinator.data else None
        return self.MODES.get(mode, "unknown")
