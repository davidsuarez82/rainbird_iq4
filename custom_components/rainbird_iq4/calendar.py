"""Rain Bird IQ4 calendar platform."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
import zoneinfo

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import (
    RainBirdConfigCoordinator,
    RainBirdProgramCoordinator,
    PROGRAM_TYPE_WEEKLY,
    PROGRAM_TYPE_ODD,
    PROGRAM_TYPE_EVEN,
    PROGRAM_TYPE_CYCLIC,
    WEEKDAY_NAMES,
)

_LOGGER = logging.getLogger(__name__)

# Map day name to weekday number (Monday=0)
WEEKDAY_MAP = {
    "Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3,
    "Fri": 4, "Sat": 5, "Sun": 6,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Rain Bird IQ4 calendar from a config entry."""
    coordinators        = hass.data[DOMAIN][entry.entry_id]
    config_coordinator  = coordinators["config"]
    program_coordinator = coordinators["program"]

    tz = zoneinfo.ZoneInfo(hass.config.time_zone)

    entities = []
    for program in program_coordinator.data.get("programs", []):
        if program.get("startTime"):
            entities.append(RainBirdCalendar(program_coordinator, config_coordinator, program, tz))

    async_add_entities(entities)


def _get_next_occurrences(
    program: dict,
    total_minutes: int,
    from_date: date,
    tz: zoneinfo.ZoneInfo,
    days_ahead: int = 30,
) -> list[CalendarEvent]:
    """Generate CalendarEvent objects for the next occurrences of a program."""
    events = []
    start_time_str = program.get("startTime")
    if not start_time_str:
        return events

    try:
        h, m = map(int, start_time_str.split(":"))
    except Exception:
        return events

    duration     = timedelta(minutes=total_minutes or 30)
    program_type = program.get("programType", PROGRAM_TYPE_WEEKLY)
    summary      = f"Irrigation: Program {program.get('shortName', '')}"

    if program_type == PROGRAM_TYPE_WEEKLY:
        target_weekdays = {WEEKDAY_MAP[d] for d in program.get("weekDays", []) if d in WEEKDAY_MAP}
        for i in range(days_ahead):
            check_date = from_date + timedelta(days=i)
            if check_date.weekday() in target_weekdays:
                events.append(_make_event(check_date, h, m, duration, tz, summary))

    elif program_type in (PROGRAM_TYPE_ODD, PROGRAM_TYPE_EVEN):
        excluded_days = {WEEKDAY_MAP[d] for d in program.get("excludedWeekDays", []) if d in WEEKDAY_MAP}
        for i in range(days_ahead):
            check_date = from_date + timedelta(days=i)
            day_num = check_date.day
            is_odd  = day_num % 2 == 1
            if program_type == PROGRAM_TYPE_ODD and not is_odd:
                continue
            if program_type == PROGRAM_TYPE_EVEN and is_odd:
                continue
            if check_date.weekday() in excluded_days:
                continue
            events.append(_make_event(check_date, h, m, duration, tz, summary))

    elif program_type == PROGRAM_TYPE_CYCLIC:
        skip_days = max(1, program.get("skipDays", 1))
        # Use nextCyclicalStartDate as anchor if available
        next_start_str = program.get("nextCyclicalStartDate")
        if next_start_str:
            try:
                anchor = datetime.fromisoformat(next_start_str.split("T")[0]).date()
            except Exception:
                anchor = from_date
        else:
            anchor = from_date

        # Find first occurrence >= from_date
        if anchor < from_date:
            delta = (from_date - anchor).days
            steps = (delta + skip_days - 1) // skip_days
            anchor = anchor + timedelta(days=steps * skip_days)

        check_date = anchor
        while (check_date - from_date).days < days_ahead:
            events.append(_make_event(check_date, h, m, duration, tz, summary))
            check_date += timedelta(days=skip_days)

    return events


def _make_event(
    day: date,
    h: int,
    m: int,
    duration: timedelta,
    tz: zoneinfo.ZoneInfo,
    summary: str,
) -> CalendarEvent:
    """Create a CalendarEvent for a given day and time."""
    start_dt = datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
    return CalendarEvent(start=start_dt, end=start_dt + duration, summary=summary)


class RainBirdCalendar(CalendarEntity):
    """Calendar entity showing scheduled irrigation events for one program."""

    def __init__(
        self,
        coordinator: RainBirdProgramCoordinator,
        config_coordinator: RainBirdConfigCoordinator,
        program: dict,
        tz: zoneinfo.ZoneInfo,
    ) -> None:
        self._coordinator = coordinator
        self._config_coordinator = config_coordinator
        self._program_id = program["id"]
        self._tz = tz
        satellite = config_coordinator.data.get("satellite", {}) if config_coordinator.data else {}
        self._satellite_id = coordinator.satellite_id
        self._satellite_name = satellite.get("name", "Rain Bird IQ4")
        self._attr_unique_id = f"{self._satellite_id}_calendar_{self._program_id}"
        self._attr_name = f"{self._satellite_name} Program {program['shortName']} Schedule"
        self._attr_icon = "mdi:calendar-clock"

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

    def _get_program(self) -> dict:
        if not self._coordinator.data:
            return {}
        for p in self._coordinator.data.get("programs", []):
            if p["id"] == self._program_id:
                return p
        return {}

    def _total_minutes(self, program: dict) -> int:
        """Calculate total irrigation time in minutes for this program."""
        total = 0
        if not self._coordinator.data:
            return 30
        for station in self._coordinator.data.get("stations", []):
            for prog in station.get("programs", []):
                if prog.get("programId") == self._program_id:
                    runtime = prog.get("adjustedRunTime", "00:00:00")
                    try:
                        parts = runtime.split(":")
                        total += int(parts[0]) * 60 + int(parts[1])
                    except Exception:
                        pass
        return total or 30

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

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming irrigation event."""
        program = self._get_program()
        if not program.get("startTime"):
            return None
        events = _get_next_occurrences(
            program,
            self._total_minutes(program),
            date.today(),
            self._tz,
            days_ahead=7,
        )
        return events[0] if events else None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return all irrigation events between start_date and end_date."""
        program = self._get_program()
        if not program.get("startTime"):
            return []

        days_ahead = (end_date.date() - start_date.date()).days + 1
        all_events = _get_next_occurrences(
            program,
            self._total_minutes(program),
            start_date.date(),
            self._tz,
            days_ahead=days_ahead,
        )

        return [
            e for e in all_events
            if start_date <= e.start_datetime_local <= end_date
        ]
