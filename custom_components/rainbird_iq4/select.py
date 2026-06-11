"""Rain Bird IQ4 select platform."""
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import RainBirdCoordinator

_LOGGER = logging.getLogger(__name__)

# Forecast parameter options
FORECAST_PERCENT_OPTIONS = ["70", "80", "90"]
FORECAST_RAINFALL_OPTIONS = ["0.125", "0.25", "0.50", "0.75"]  # inches
FORECAST_DELAY_OPTIONS = ["1", "2"]

# Weather adjust method options
WEATHER_ADJUST_OPTIONS = ["none", "automatic"]
ET_ADJUST_TYPE_MAP = {"none": 6, "automatic": 7}
ET_ADJUST_TYPE_REVERSE = {6: "none", 7: "automatic"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Rain Bird IQ4 select entities from a config entry."""
    coordinator: RainBirdCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        RainBirdForecastPercent(coordinator),
        RainBirdForecastRainfall(coordinator),
        RainBirdForecastDelayDays(coordinator),
    ]

    for program in coordinator.data.get("programs", []):
        entities.append(RainBirdWeatherAdjustMethod(coordinator, program))

    async_add_entities(entities)


class RainBirdBaseSelect(CoordinatorEntity, SelectEntity):
    """Base class for Rain Bird IQ4 select entities."""

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

    @property
    def _forecast_enabled(self) -> bool:
        return self.coordinator.data.get("forecast", {}).get("enabled", False)


class RainBirdForecastPercent(RainBirdBaseSelect):
    """Select entity for forecast rain percent threshold."""

    def __init__(self, coordinator: RainBirdCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._satellite_id}_forecast_percent"
        self._attr_name = f"{self._satellite_name} Forecast Percent"
        self._attr_icon = "mdi:percent"
        self._attr_options = FORECAST_PERCENT_OPTIONS

    @property
    def available(self) -> bool:
        return self._forecast_enabled

    @property
    def current_option(self) -> str | None:
        val = self.coordinator.data.get("forecast", {}).get("percent")
        if val is None:
            return None
        return str(int(val))

    async def async_select_option(self, option: str) -> None:
        forecast = self.coordinator.data.get("forecast", {})
        await self.hass.async_add_executor_job(
            self.coordinator.api.set_forecast,
            self.coordinator.satellite_id,
            True,
            int(option),
            forecast.get("inches"),
            forecast.get("delayDays"),
        )
        await self.coordinator.async_request_refresh()


class RainBirdForecastRainfall(RainBirdBaseSelect):
    """Select entity for forecast rainfall threshold in inches."""

    def __init__(self, coordinator: RainBirdCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._satellite_id}_forecast_rainfall"
        self._attr_name = f"{self._satellite_name} Forecast Rainfall"
        self._attr_icon = "mdi:weather-rainy"
        self._attr_options = FORECAST_RAINFALL_OPTIONS

    @property
    def available(self) -> bool:
        return self._forecast_enabled

    @property
    def current_option(self) -> str | None:
        val = self.coordinator.data.get("forecast", {}).get("inches")
        if val is None:
            return None
        # Match to closest option
        closest = min(FORECAST_RAINFALL_OPTIONS, key=lambda x: abs(float(x) - float(val)))
        return closest

    async def async_select_option(self, option: str) -> None:
        forecast = self.coordinator.data.get("forecast", {})
        await self.hass.async_add_executor_job(
            self.coordinator.api.set_forecast,
            self.coordinator.satellite_id,
            True,
            forecast.get("percent"),
            float(option),
            forecast.get("delayDays"),
        )
        await self.coordinator.async_request_refresh()


class RainBirdForecastDelayDays(RainBirdBaseSelect):
    """Select entity for forecast delay days."""

    def __init__(self, coordinator: RainBirdCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._satellite_id}_forecast_delay_days"
        self._attr_name = f"{self._satellite_name} Forecast Delay Days"
        self._attr_icon = "mdi:calendar-clock"
        self._attr_options = FORECAST_DELAY_OPTIONS

    @property
    def available(self) -> bool:
        return self._forecast_enabled

    @property
    def current_option(self) -> str | None:
        val = self.coordinator.data.get("forecast", {}).get("delayDays")
        if val is None:
            return None
        return str(int(val))

    async def async_select_option(self, option: str) -> None:
        forecast = self.coordinator.data.get("forecast", {})
        await self.hass.async_add_executor_job(
            self.coordinator.api.set_forecast,
            self.coordinator.satellite_id,
            True,
            forecast.get("percent"),
            forecast.get("inches"),
            int(option),
        )
        await self.coordinator.async_request_refresh()


class RainBirdWeatherAdjustMethod(RainBirdBaseSelect):
    """Select entity for weather adjust method per program."""

    def __init__(self, coordinator: RainBirdCoordinator, program: dict) -> None:
        super().__init__(coordinator)
        self._program_id = program["id"]
        self._attr_unique_id = f"{self._satellite_id}_weather_adjust_{self._program_id}"
        self._attr_name = f"{self._satellite_name} Program {program['shortName']} Weather Adjust"
        self._attr_icon = "mdi:weather-sunny"
        self._attr_options = WEATHER_ADJUST_OPTIONS

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
    def available(self) -> bool:
        return self._is_program_scheduled()

    @property
    def current_option(self) -> str:
        program = self._get_program()
        et_type = program.get("etAdjustType", 6)
        return ET_ADJUST_TYPE_REVERSE.get(et_type, "none")

    async def async_select_option(self, option: str) -> None:
        method = ET_ADJUST_TYPE_MAP.get(option, 6)
        await self.hass.async_add_executor_job(
            self.coordinator.api.set_weather_adjust_method,
            self._program_id,
            method,
        )
        await self.coordinator.async_request_refresh()
