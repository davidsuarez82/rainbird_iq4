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
from .coordinator import RainBirdCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Rain Bird IQ4 binary sensors from a config entry."""
    coordinator: RainBirdCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        RainBirdConnectionBinarySensor(coordinator),
        RainBirdForecastBinarySensor(coordinator),
    ]

    # Add a binary sensor for each physical sensor (e.g. rain sensor)
    for sensor in coordinator.data.get("sensors", []):
        if sensor.get("type", -1) != -1:  # -1 means no sensor installed
            entities.append(RainBirdRainSensor(coordinator, sensor))

    async_add_entities(entities)


class RainBirdBaseBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Base class for Rain Bird IQ4 binary sensors."""

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


class RainBirdConnectionBinarySensor(RainBirdBaseBinarySensor):
    """Binary sensor reporting whether the controller is connected to the cloud."""

    def __init__(self, coordinator: RainBirdCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._satellite_id}_connected"
        self._attr_name = f"{self._satellite_name} Connected"
        self._attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
        self._attr_icon = "mdi:cloud-check"

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.get("connection", {}).get("isConnected", False)


class RainBirdForecastBinarySensor(RainBirdBaseBinarySensor):
    """Binary sensor reporting whether forecast rain delay is enabled."""

    def __init__(self, coordinator: RainBirdCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._satellite_id}_forecast_enabled"
        self._attr_name = f"{self._satellite_name} Forecast Rain Delay"
        self._attr_device_class = BinarySensorDeviceClass.RUNNING
        self._attr_icon = "mdi:weather-lightning-rainy"

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.get("forecast", {}).get("enabled", False)

    @property
    def extra_state_attributes(self) -> dict:
        forecast = self.coordinator.data.get("forecast", {})
        if not forecast.get("enabled"):
            return {}
        return {
            "percent":    forecast.get("percent"),
            "rainfall":   forecast.get("inches"),
            "delay_days": forecast.get("delayDays"),
        }


class RainBirdRainSensor(RainBirdBaseBinarySensor):
    """Binary sensor reporting whether the rain sensor is triggered."""

    def __init__(self, coordinator: RainBirdCoordinator, sensor: dict) -> None:
        super().__init__(coordinator)
        self._sensor_id = sensor["id"]
        self._attr_unique_id = f"{self._satellite_id}_sensor_{self._sensor_id}"
        self._attr_name = f"{self._satellite_name} {sensor.get('name', 'Rain Sensor')}"
        self._attr_device_class = BinarySensorDeviceClass.MOISTURE
        self._attr_icon = "mdi:weather-rainy"

    def _get_sensor(self) -> dict:
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
