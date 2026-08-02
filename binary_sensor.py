"""Rain Bird IQ4 binary sensor platform."""
from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import RainBirdCoordinator, RainBirdConfigCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Rain Bird IQ4 binary sensors from a config entry."""
    coordinators       = hass.data[DOMAIN][entry.entry_id]
    config_coordinator = coordinators["config"]

    entities = [
        RainBirdConnectionBinarySensor(coordinators["realtime"], config_coordinator),
        RainBirdForecastBinarySensor(config_coordinator),
        RainBirdAnyZoneRunningBinarySensor(coordinators["realtime"], config_coordinator),
    ]

    for sensor in config_coordinator.data.get("sensors", []):
        if sensor.get("type", -1) != -1:
            entities.append(RainBirdRainSensor(config_coordinator, sensor))

    async_add_entities(entities)


class RainBirdConnectionBinarySensor(BinarySensorEntity):
    """Binary sensor reporting whether the controller is connected — real-time."""

    def __init__(
        self,
        coordinator: RainBirdCoordinator,
        config_coordinator: RainBirdConfigCoordinator,
    ) -> None:
        self._coordinator = coordinator
        self._config_coordinator = config_coordinator
        satellite = config_coordinator.data.get("satellite", {}) if config_coordinator.data else {}
        self._satellite_id = coordinator.satellite_id
        self._satellite_name = satellite.get("name", "Rain Bird IQ4")
        self._attr_unique_id = f"{self._satellite_id}_connected"
        self._attr_name = f"{self._satellite_name} Connected"
        self._attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
        self._attr_icon = "mdi:cloud-check"

    @property
    def device_info(self) -> DeviceInfo:
        satellite = self._config_coordinator.data.get("satellite", {}) if self._config_coordinator.data else {}
        return DeviceInfo(
            identifiers={(DOMAIN, str(self._satellite_id))},
            name=self._satellite_name,
            manufacturer="Rain Bird",
            model=satellite.get("model", "Rain Bird IQ4"),
            sw_version=satellite.get("version"),
        )

    @property
    def is_on(self) -> bool:
        return self._coordinator.data.get("connection", {}).get("isConnected", False) if self._coordinator.data else False

    @property
    def available(self) -> bool:
        return self._coordinator.last_update_success

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._coordinator.async_add_listener(self.async_write_ha_state)
        )
        self.async_on_remove(
            self._config_coordinator.async_add_listener(self.async_write_ha_state)
        )


class RainBirdAnyZoneRunningBinarySensor(BinarySensorEntity):
    """Binary sensor reporting whether any zone is currently running — real-time.

    Saves comparing every Station sensor individually or building a template
    helper — a single entity for automations like "pause music while
    irrigation is running" or "notify when irrigation starts".
    """

    def __init__(
        self,
        coordinator: RainBirdCoordinator,
        config_coordinator: RainBirdConfigCoordinator,
    ) -> None:
        self._coordinator = coordinator
        self._config_coordinator = config_coordinator
        satellite = config_coordinator.data.get("satellite", {}) if config_coordinator.data else {}
        self._satellite_id = coordinator.satellite_id
        self._satellite_name = satellite.get("name", "Rain Bird IQ4")
        self._attr_unique_id = f"{self._satellite_id}_any_zone_running"
        self._attr_name = f"{self._satellite_name} Any Zone Running"
        self._attr_device_class = BinarySensorDeviceClass.RUNNING
        self._attr_icon = "mdi:sprinkler-variant"

    @property
    def device_info(self) -> DeviceInfo:
        satellite = self._config_coordinator.data.get("satellite", {}) if self._config_coordinator.data else {}
        return DeviceInfo(
            identifiers={(DOMAIN, str(self._satellite_id))},
            name=self._satellite_name,
            manufacturer="Rain Bird",
            model=satellite.get("model", "Rain Bird IQ4"),
            sw_version=satellite.get("version"),
        )

    def _running_stations(self) -> list[dict]:
        if not self._coordinator.data:
            return []
        return [s for s in self._coordinator.data.get("stations", []) if s.get("isRunning")]

    @property
    def is_on(self) -> bool:
        return bool(self._running_stations())

    @property
    def extra_state_attributes(self) -> dict:
        running = self._running_stations()
        return {"running_zones": [s.get("name") for s in running]} if running else {}

    @property
    def available(self) -> bool:
        return self._coordinator.last_update_success

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._coordinator.async_add_listener(self.async_write_ha_state)
        )
        self.async_on_remove(
            self._config_coordinator.async_add_listener(self.async_write_ha_state)
        )


class RainBirdForecastBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor reporting whether forecast rain delay is enabled — config polling."""

    def __init__(self, coordinator: RainBirdConfigCoordinator) -> None:
        super().__init__(coordinator)
        self._satellite_id = coordinator.satellite_id
        satellite = coordinator.data.get("satellite", {}) if coordinator.data else {}
        self._satellite_name = satellite.get("name", "Rain Bird IQ4")
        self._attr_unique_id = f"{self._satellite_id}_forecast_enabled"
        self._attr_name = f"{self._satellite_name} Forecast Rain Delay"
        self._attr_device_class = BinarySensorDeviceClass.RUNNING
        self._attr_icon = "mdi:weather-lightning-rainy"

    @property
    def device_info(self) -> DeviceInfo:
        satellite = self.coordinator.data.get("satellite", {}) if self.coordinator.data else {}
        return DeviceInfo(
            identifiers={(DOMAIN, str(self._satellite_id))},
            name=self._satellite_name,
            manufacturer="Rain Bird",
            model=satellite.get("model", "Rain Bird IQ4"),
            sw_version=satellite.get("version"),
        )

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.get("forecast", {}).get("enabled", False) if self.coordinator.data else False

    @property
    def extra_state_attributes(self) -> dict:
        if not self.coordinator.data:
            return {}
        forecast = self.coordinator.data.get("forecast", {})
        if not forecast.get("enabled"):
            return {}
        return {
            "percent":    forecast.get("percent"),
            "rainfall":   forecast.get("inches"),
            "delay_days": forecast.get("delayDays"),
        }


class RainBirdRainSensor(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor reporting whether the rain sensor is triggered — config polling."""

    def __init__(self, coordinator: RainBirdConfigCoordinator, sensor: dict) -> None:
        super().__init__(coordinator)
        self._sensor_id = sensor["id"]
        self._satellite_id = coordinator.satellite_id
        satellite = coordinator.data.get("satellite", {}) if coordinator.data else {}
        self._satellite_name = satellite.get("name", "Rain Bird IQ4")
        self._attr_unique_id = f"{self._satellite_id}_sensor_{self._sensor_id}"
        self._attr_name = f"{self._satellite_name} {sensor.get('name', 'Rain Sensor')}"
        self._attr_device_class = BinarySensorDeviceClass.MOISTURE
        self._attr_icon = "mdi:weather-rainy"

    @property
    def device_info(self) -> DeviceInfo:
        satellite = self.coordinator.data.get("satellite", {}) if self.coordinator.data else {}
        return DeviceInfo(
            identifiers={(DOMAIN, str(self._satellite_id))},
            name=self._satellite_name,
            manufacturer="Rain Bird",
            model=satellite.get("model", "Rain Bird IQ4"),
            sw_version=satellite.get("version"),
        )

    def _get_sensor(self) -> dict:
        if not self.coordinator.data:
            return {}
        for s in self.coordinator.data.get("sensors", []):
            if s["id"] == self._sensor_id:
                return s
        return {}

    @property
    def is_on(self) -> bool:
        return bool(self._get_sensor().get("triggered", False))

    @property
    def extra_state_attributes(self) -> dict:
        sensor = self._get_sensor()
        return {
            "model":  sensor.get("model"),
            "type":   sensor.get("typeName"),
            "active": sensor.get("active"),
        }
