"""Rain Bird IQ4 data update coordinators."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import RainBirdAPI
from .const import DOMAIN, WEEKDAY_NAMES, get_controller_model

_LOGGER = logging.getLogger(__name__)

# Event numbers from the Rain Bird event log
EVENT_STATION_ON      = 97
EVENT_STATION_OFF     = 98
EVENT_IRRIGATION_DONE = 15000

# Number of consecutive errors before marking unavailable
MAX_CONSECUTIVE_ERRORS = 3


def _parse_weekdays(weekdays_str: str) -> list[str]:
    """Convert '0010101' bitstring to list of day names."""
    if not weekdays_str or len(weekdays_str) != 7:
        return []
    return [WEEKDAY_NAMES[i] for i, bit in enumerate(weekdays_str) if bit == "1"]


def _parse_start_time(start_time_str: str | None) -> str | None:
    """Extract HH:MM from a datetime string, return None for unset values."""
    if not start_time_str or start_time_str.startswith("0001"):
        return None
    try:
        return start_time_str.split("T")[1][:5]
    except Exception:
        return None


# Program schedule type IDs
PROGRAM_TYPE_WEEKLY  = 0  # Fixed weekdays
PROGRAM_TYPE_ODD     = 2  # Odd calendar days
PROGRAM_TYPE_EVEN    = 4  # Even calendar days
PROGRAM_TYPE_CYCLIC  = 5  # Every N days


def _parse_excluded_weekdays(hybrid_str: str) -> list[str]:
    """Parse hybridWeekDays — '0' means excluded, '1' means allowed."""
    if not hybrid_str or len(hybrid_str) != 7:
        return []
    return [WEEKDAY_NAMES[i] for i, bit in enumerate(hybrid_str) if bit == "0"]


# Map day name to Python weekday (Monday=0)
_WEEKDAY_MAP = {
    "Sun": 6, "Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5,
}


def _calculate_next_run(
    program_type: int,
    start_time_str: str | None,
    week_days: list[str],
    excluded_week_days: list[str],
    skip_days: int,
    next_cyclical_start: str | None,
) -> str | None:
    """Calculate the next run date for a program, accounting for start time vs now."""
    now = datetime.now()
    today = now.date()

    # Parse start time to know if today's run has already passed
    start_hour, start_min = 0, 0
    if start_time_str:
        try:
            start_hour, start_min = map(int, start_time_str.split(":"))
        except Exception:
            pass
    run_time_passed_today = now.hour > start_hour or (
        now.hour == start_hour and now.minute >= start_min
    )
    # First candidate: today if run hasn't passed yet, else tomorrow
    first_candidate = today + timedelta(days=1) if run_time_passed_today else today

    if program_type == PROGRAM_TYPE_WEEKLY:
        if not week_days:
            return None
        target_weekdays = {_WEEKDAY_MAP[d] for d in week_days if d in _WEEKDAY_MAP}
        for i in range(14):
            candidate = first_candidate + timedelta(days=i)
            if candidate.weekday() in target_weekdays:
                return candidate.isoformat()
        return None

    elif program_type == PROGRAM_TYPE_ODD:
        excluded = {_WEEKDAY_MAP[d] for d in excluded_week_days if d in _WEEKDAY_MAP}
        for i in range(14):
            candidate = first_candidate + timedelta(days=i)
            if candidate.day % 2 == 1 and candidate.weekday() not in excluded:
                return candidate.isoformat()
        return None

    elif program_type == PROGRAM_TYPE_EVEN:
        excluded = {_WEEKDAY_MAP[d] for d in excluded_week_days if d in _WEEKDAY_MAP}
        for i in range(14):
            candidate = first_candidate + timedelta(days=i)
            if candidate.day % 2 == 0 and candidate.weekday() not in excluded:
                return candidate.isoformat()
        return None

    elif program_type == PROGRAM_TYPE_CYCLIC:
        # Use API-provided nextCyclicalStartDate but adjust if today's run already passed
        if not next_cyclical_start:
            return None
        try:
            api_date = date.fromisoformat(next_cyclical_start.split("T")[0])
        except Exception:
            return None
        if api_date == today and run_time_passed_today:
            return (today + timedelta(days=skip_days)).isoformat()
        return api_date.isoformat()

    return None


def _process_event_logs(event_logs: list, stations: list) -> dict:
    """Process event logs to determine station status and last run times."""
    terminal_to_id = {s.get("terminal"): s.get("id") for s in stations}
    sorted_events = sorted(event_logs, key=lambda e: e.get("timestamp", ""))

    station_events: dict[int, dict] = {}
    for terminal in terminal_to_id:
        station_events[terminal] = {
            "isRunning":        False,
            "lastRun":          None,
            "lastRunCompleted": None,
        }

    for event in sorted_events:
        terminal = event.get("eventParameter1")
        if terminal not in station_events:
            continue
        num = event.get("eventNumber")
        ts  = event.get("timestamp")
        if num == EVENT_STATION_ON:
            station_events[terminal]["isRunning"] = True
            station_events[terminal]["lastRun"]   = ts
        elif num == EVENT_STATION_OFF:
            station_events[terminal]["isRunning"] = False
        elif num == EVENT_IRRIGATION_DONE:
            station_events[terminal]["isRunning"]        = False
            station_events[terminal]["lastRunCompleted"] = ts

    return station_events


class RainBirdCoordinator(DataUpdateCoordinator):
    """
    Real-time coordinator — polls every 30s (configurable).

    Fetches: station run status via event log, connection state, alerts.
    Tolerates up to 3 consecutive errors before marking unavailable.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api: RainBirdAPI,
        satellite_id: int,
        company_id: int,
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass, _LOGGER, name=f"{DOMAIN}_realtime",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.api = api
        self.satellite_id = satellite_id
        self.company_id = company_id
        self._consecutive_errors = 0

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data = await self.hass.async_add_executor_job(self._fetch_data)
            self._consecutive_errors = 0
            return data
        except Exception as err:
            self._consecutive_errors += 1
            # Never mask a failure of the very first refresh: with no previous
            # data, returning None would crash platform setup later on.
            if self.data is None or self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                self._consecutive_errors = 0
                raise UpdateFailed(f"Error fetching Rain Bird data: {err}") from err
            _LOGGER.warning(
                "Transient error (%d/%d), keeping last known data: %s",
                self._consecutive_errors, MAX_CONSECUTIVE_ERRORS, err
            )
            return self.data

    def _fetch_data(self) -> dict[str, Any]:
        sid = self.satellite_id
        cid = self.company_id

        connected  = self.api.is_connected(sid)
        alerts     = self.api.get_company_status(cid)
        event_logs = self.api.get_event_logs(sid, hours=24)
        stations   = self.api.get_station_list(sid)
        run_status = self.api.get_run_station_status(sid)

        # Map stationId → live status
        station_live: dict[int, dict] = {}
        for prog in run_status:
            for rs in prog.get("runStationStatuses", []):
                station_live[rs["stationId"]] = {
                    "status":    rs.get("status", "-"),
                    "remaining": rs.get("remainingRunTime"),
                }

        # Process event logs
        station_event_data = _process_event_logs(event_logs, stations)

        stations_data = []
        for s in stations:
            sid_key  = s["id"]
            terminal = s.get("terminal")
            live     = station_live.get(sid_key, {})
            events   = station_event_data.get(terminal, {})
            live_status = live.get("status", "-")
            is_running  = live_status == "R" or events.get("isRunning", False)
            stations_data.append({
                "id":               sid_key,
                "name":             s.get("name"),
                "terminal":         terminal,
                "status":           "R" if is_running else live_status,
                "remaining":        live.get("remaining"),
                "isRunning":        is_running,
                "lastRun":          events.get("lastRun"),
                "lastRunCompleted": events.get("lastRunCompleted"),
            })

        return {
            "connection": {
                "isConnected": connected,
            },
            "alerts": {
                "alarms":   alerts.get("unackedAlarmCount", 0),
                "warnings": alerts.get("unackedWarningCount", 0),
            },
            "stations":  stations_data,
            "eventLogs": event_logs,
        }


class RainBirdConfigCoordinator(DataUpdateCoordinator):
    """
    Config coordinator — polls every 5 min (configurable).

    Fetches: satellite info, rain delay, forecast settings, physical sensors.
    Tolerates up to 3 consecutive errors before marking unavailable.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api: RainBirdAPI,
        satellite_id: int,
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass, _LOGGER, name=f"{DOMAIN}_config",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.api = api
        self.satellite_id = satellite_id
        self._consecutive_errors = 0

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data = await self.hass.async_add_executor_job(self._fetch_data)
            self._consecutive_errors = 0
            return data
        except Exception as err:
            self._consecutive_errors += 1
            # Never mask a failure of the very first refresh (see realtime note).
            if self.data is None or self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                self._consecutive_errors = 0
                raise UpdateFailed(f"Error fetching Rain Bird config data: {err}") from err
            _LOGGER.warning(
                "Transient config error (%d/%d), keeping last known data: %s",
                self._consecutive_errors, MAX_CONSECUTIVE_ERRORS, err
            )
            return self.data

    def _fetch_data(self) -> dict[str, Any]:
        sid = self.satellite_id

        # GetSatellite returns 403 on some controller types (e.g. ESP-ME3).
        # Fall back to GetSatelliteList in that case.
        raw = self.api.get_satellite(sid)
        if raw is None:
            _LOGGER.info(
                "GetSatellite unavailable for satellite %s, using GetSatelliteList fallback",
                sid,
            )
            sat_list = self.api.get_satellite_list()
            match = next((s for s in sat_list if s.get("id") == sid), None)
            if match:
                raw = {
                    "id":                   match.get("id"),
                    "name":                 match.get("name"),
                    "versionString":        match.get("version"),
                    "satelliteEnabled":     match.get("satelliteEnabled", True),
                    "logicalDialPos":       match.get("frontPanelState"),
                    "rainDelay":            match.get("rainDelay", 0),
                    "rainDelayDaysRemaining": 0,
                    "syncState":            match.get("syncState"),
                    "useForecast":          False,
                    "forecastPercentLimit": None,
                    "forecastInchesLimit":  None,
                    "forecastDelayDays":    None,
                }
            else:
                raw = {}

        sensors = self.api.get_sensor_list(sid)

        return {
            "satellite": {
                "id":         raw.get("id"),
                "name":       raw.get("name"),
                "version":    raw.get("versionString"),
                "enabled":    raw.get("satelliteEnabled"),
                "systemMode": raw.get("logicalDialPos"),
                "model":      get_controller_model(raw.get("type")),
            },
            "connection": {
                "rainDelay":              raw.get("rainDelay"),
                "rainDelayDaysRemaining": raw.get("rainDelayDaysRemaining", 0),
                "syncState":              raw.get("syncState"),
            },
            "forecast": {
                "enabled":   raw.get("useForecast", False),
                "percent":   raw.get("forecastPercentLimit"),
                "inches":    raw.get("forecastInchesLimit"),
                "delayDays": raw.get("forecastDelayDays"),
            },
            "sensors": [
                {
                    "id":        s.get("id"),
                    "name":      s.get("name"),
                    "type":      s.get("type"),
                    "typeName":  s.get("typeName"),
                    "model":     s.get("model"),
                    "active":    s.get("active"),
                    "triggered": s.get("triggered"),
                }
                for s in sensors
            ],
        }


class RainBirdProgramCoordinator(DataUpdateCoordinator):
    """
    Program coordinator — polls every 1 hour (configurable).

    Fetches: programs with schedule, adjust settings and assigned runtimes.
    Tolerates up to 3 consecutive errors before marking unavailable.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api: RainBirdAPI,
        satellite_id: int,
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass, _LOGGER, name=f"{DOMAIN}_programs",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.api = api
        self.satellite_id = satellite_id
        self._consecutive_errors = 0

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data = await self.hass.async_add_executor_job(self._fetch_data)
            self._consecutive_errors = 0
            return data
        except Exception as err:
            self._consecutive_errors += 1
            # Never mask a failure of the very first refresh (see realtime note).
            if self.data is None or self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                self._consecutive_errors = 0
                raise UpdateFailed(f"Error fetching Rain Bird program data: {err}") from err
            _LOGGER.warning(
                "Transient program error (%d/%d), keeping last known data: %s",
                self._consecutive_errors, MAX_CONSECUTIVE_ERRORS, err
            )
            return self.data

    def _fetch_data(self) -> dict[str, Any]:
        sid = self.satellite_id

        programs   = self.api.get_program_list(sid)
        stations   = self.api.get_station_list(sid)
        assigned   = self.api.get_programs_assigned_runtime(sid)
        flow_zones = self.api.get_flow_elements(sid)
        flow_mon   = self.api.get_flow_monitoring(sid)

        # Map stationId → assigned runtimes
        station_runtime: dict[int, list] = {}
        for item in assigned:
            sid_key = item["stationId"]
            for prog in item.get("runtimeProgramAssignedList", []):
                station_runtime.setdefault(sid_key, []).append({
                    "programId":       prog.get("programId"),
                    "programName":     prog.get("programShortName"),
                    "baseRunTime":     prog.get("baseRunTime"),
                    "adjustedRunTime": prog.get("adjustedRunTime"),
                })

        # Enrich stations with assigned programs
        stations_data = []
        for s in stations:
            stations_data.append({
                "id":       s["id"],
                "name":     s.get("name"),
                "terminal": s.get("terminal"),
                "programs": station_runtime.get(s["id"], []),
            })

        # Enrich programs
        programs_data = []
        for p in programs:
            et_type          = p.get("etAdjustType", 6)
            program_type     = p.get("type", PROGRAM_TYPE_WEEKLY)
            start_time       = _parse_start_time(p.get("startTime"))
            week_days        = _parse_weekdays(p.get("weekDays", ""))
            excluded_days    = _parse_excluded_weekdays(p.get("hybridWeekDays", ""))
            skip_days        = p.get("skipDays", 1)
            next_run = _calculate_next_run(
                program_type=program_type,
                start_time_str=start_time,
                week_days=week_days,
                excluded_week_days=excluded_days,
                skip_days=skip_days,
                next_cyclical_start=p.get("nextCyclicalStartDate"),
            )
            programs_data.append({
                "id":               p.get("id"),
                "name":             p.get("name"),
                "shortName":        p.get("shortName"),
                "isEnabled":        p.get("isEnabled"),
                "startTime":        start_time,
                "programType":      program_type,
                "weekDays":         week_days,
                "excludedWeekDays": excluded_days,
                "skipDays":         skip_days,
                "nextRun":          next_run,
                "adjust":           p.get("programAdjust"),
                "adjustedValue":    p.get("tempProgramAdjust") if et_type == 7 else p.get("programAdjust"),
                "steps":            p.get("numberOfProgramSteps"),
                "etAdjustType":     et_type,
            })

        return {
            "programs":  programs_data,
            "stations":  stations_data,
            "flowZones": [
                {
                    "id":              fz.get("id"),
                    "name":            fz.get("name"),
                    "flowRate":        fz.get("flowRate"),
                    "flowRateLearned": fz.get("flowRateLearned"),
                }
                for fz in flow_zones
            ],
            "flowMonitoring": {
                "enabled":           flow_mon.get("floWatchEnabled"),
                "maxFlowRate":       flow_mon.get("maxFlowRate"),
                "highFlowThreshold": flow_mon.get("highFlowThreshold"),
                "lowFlowThreshold":  flow_mon.get("lowFlowThreshold"),
            },
        }
