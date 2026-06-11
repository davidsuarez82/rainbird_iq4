"""Rain Bird IQ4 data update coordinator."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import RainBirdAPI
from .const import DOMAIN, WEEKDAY_NAMES

_LOGGER = logging.getLogger(__name__)

# Event numbers from the Rain Bird event log
EVENT_STATION_ON       = 97    # station turning on
EVENT_STATION_OFF      = 98    # station turning off
EVENT_IRRIGATION_DONE  = 15000 # irrigation completed


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


def _process_event_logs(event_logs: list, stations: list) -> dict:
    """
    Process event logs to determine station status and last run times.

    Returns a dict keyed by terminal number with:
      - isRunning: bool
      - lastRun: timestamp string or None
      - lastRunCompleted: timestamp string or None
    """
    # Map terminal → station id
    terminal_to_id = {s.get("terminal"): s.get("id") for s in stations}

    # Sort events oldest first
    sorted_events = sorted(event_logs, key=lambda e: e.get("timestamp", ""))

    # Track state per terminal
    station_events: dict[int, dict] = {}
    for terminal in terminal_to_id:
        station_events[terminal] = {
            "isRunning":       False,
            "lastRun":         None,
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
    Coordinator that fetches and caches Rain Bird IQ4 data.

    Polls the Rain Bird API at a configurable interval and
    provides structured data to all platform entities.
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
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.api = api
        self.satellite_id = satellite_id
        self.company_id = company_id

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch all data from Rain Bird API."""
        try:
            return await self.hass.async_add_executor_job(self._fetch_data)
        except Exception as err:
            raise UpdateFailed(f"Error fetching Rain Bird data: {err}") from err

    def _fetch_data(self) -> dict[str, Any]:
        """Synchronous data fetch — runs in executor thread."""
        sid = self.satellite_id
        cid = self.company_id

        raw        = self.api.get_satellite(sid)
        programs   = self.api.get_program_list(sid)
        stations   = self.api.get_station_list(sid)
        run_status = self.api.get_run_station_status(sid)
        assigned   = self.api.get_programs_assigned_runtime(sid)
        connected  = self.api.is_connected(sid)
        alerts     = self.api.get_company_status(cid)
        sensors    = self.api.get_sensor_list(sid)
        flow_zones = self.api.get_flow_elements(sid)
        flow_mon   = self.api.get_flow_monitoring(sid)
        event_logs = self.api.get_event_logs(sid, hours=24)

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

        # Map stationId → live status from run_status endpoint
        station_live: dict[int, dict] = {}
        for prog in run_status:
            for rs in prog.get("runStationStatuses", []):
                station_live[rs["stationId"]] = {
                    "status":    rs.get("status", "-"),
                    "remaining": rs.get("remainingRunTime"),
                }

        # Process event logs — more reliable than run_status for scheduled runs
        station_event_data = _process_event_logs(event_logs, stations)

        # Map terminal → station id for event lookup
        terminal_to_id = {s.get("terminal"): s.get("id") for s in stations}

        # Enrich stations
        stations_data = []
        for s in stations:
            sid_key  = s["id"]
            terminal = s.get("terminal")
            live     = station_live.get(sid_key, {})
            events   = station_event_data.get(terminal, {})

            # Prefer live API status, fall back to event log
            live_status = live.get("status", "-")
            is_running  = live_status == "R" or events.get("isRunning", False)

            stations_data.append({
                "id":               sid_key,
                "name":             s.get("name"),
                "terminal":         terminal,
                "status":           "R" if is_running else live_status,
                "remaining":        live.get("remaining"),
                "programs":         station_runtime.get(sid_key, []),
                "isRunning":        is_running,
                "lastRun":          events.get("lastRun"),
                "lastRunCompleted": events.get("lastRunCompleted"),
            })

        # Enrich programs
        programs_data = []
        for p in programs:
            programs_data.append({
                "id":           p.get("id"),
                "name":         p.get("name"),
                "shortName":    p.get("shortName"),
                "isEnabled":    p.get("isEnabled"),
                "startTime":    _parse_start_time(p.get("startTime")),
                "weekDays":     _parse_weekdays(p.get("weekDays", "")),
                "adjust":       p.get("programAdjust"),
                "steps":        p.get("numberOfProgramSteps"),
                "etAdjustType": p.get("etAdjustType"),
            })

        return {
            "satellite": {
                "id":         raw.get("id"),
                "name":       raw.get("name"),
                "version":    raw.get("versionString"),
                "enabled":    raw.get("satelliteEnabled"),
                "systemMode": raw.get("logicalDialPos"),
            },
            "connection": {
                "isConnected":            connected,
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
            "alerts": {
                "alarms":   alerts.get("unackedAlarmCount", 0),
                "warnings": alerts.get("unackedWarningCount", 0),
            },
            "programs":  programs_data,
            "stations":  stations_data,
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
            "eventLogs": event_logs,
        }
