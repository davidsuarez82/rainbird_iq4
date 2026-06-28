"""Rain Bird IQ4 API client."""
from __future__ import annotations

import datetime
import logging
from typing import Any

from curl_cffi import requests as cf_requests

from .auth import RainBirdAuth
from .const import API_BASE

_LOGGER = logging.getLogger(__name__)


class RainBirdAPI:
    """
    Client for the Rain Bird IQ4 REST API.

    All methods return parsed JSON or None on empty responses.
    Automatically retries with a fresh token on 401 responses.
    """

    def __init__(self, auth: RainBirdAuth) -> None:
        self._auth = auth

    def _get(self, path: str, params: dict | None = None) -> Any:
        """Perform a GET request, retrying once on 401."""
        url = f"{API_BASE}/{path}"
        r = cf_requests.get(url, params=params, headers=self._auth.get_headers(), timeout=30, impersonate="chrome")
        if r.status_code == 401:
            _LOGGER.debug("Token rejected, refreshing and retrying")
            self._auth.invalidate()
            r = cf_requests.get(url, params=params, headers=self._auth.get_headers(), timeout=30, impersonate="chrome")
        r.raise_for_status()
        return r.json() if r.text.strip() else None

    def _post(self, path: str, json: Any = None, params: dict | None = None) -> Any:
        """Perform a POST request, retrying once on 401."""
        url = f"{API_BASE}/{path}"
        r = cf_requests.post(url, json=json, params=params, headers=self._auth.get_headers(), timeout=30, impersonate="chrome")
        if r.status_code == 401:
            self._auth.invalidate()
            r = cf_requests.post(url, json=json, params=params, headers=self._auth.get_headers(), timeout=30, impersonate="chrome")
        r.raise_for_status()
        return r.json() if r.text.strip() else None

    def _patch(self, path: str, json: Any = None) -> Any:
        """Perform a PATCH request, retrying once on 401."""
        url = f"{API_BASE}/{path}"
        r = cf_requests.patch(url, json=json, headers=self._auth.get_headers(), timeout=30, impersonate="chrome")
        if r.status_code == 401:
            self._auth.invalidate()
            r = cf_requests.patch(url, json=json, headers=self._auth.get_headers(), timeout=30, impersonate="chrome")
        r.raise_for_status()
        return r.json() if r.text.strip() else None

    # ── Satellite ─────────────────────────────────────────────────────────────

    def get_satellite(self, satellite_id: int) -> dict | None:
        """Get full satellite (controller) details.
        Returns None if the endpoint is not available (e.g. ESP-ME3 returns 403)."""
        try:
            return self._get("Satellite/GetSatellite", {"satelliteId": satellite_id})
        except cf_requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 403:
                _LOGGER.debug(
                    "GetSatellite returned 403 for satellite %s, will use fallback",
                    satellite_id,
                )
                return None
            raise

    def get_satellite_list(self) -> list:
        """Get list of all satellites for the account."""
        return self._get(
            "Satellite/GetSatelliteList",
            {"includeInvisibleToCurrentUser": False},
        ) or []

    def is_connected(self, satellite_id: int) -> bool:
        """Return True if the controller is currently connected to the cloud."""
        result = self._get("Satellite/isConnected", {"satelliteIds": satellite_id}) or {}
        for s in result.get("satellites", []):
            if s.get("id") == satellite_id:
                return bool(s.get("isConnected", False))
        return False

    # ── Programs ──────────────────────────────────────────────────────────────

    def get_program_list(self, satellite_id: int) -> list:
        """Get all irrigation programs for a satellite."""
        return self._get("Program/GetProgramList", {"satelliteId": satellite_id}) or []

    # ── Stations ──────────────────────────────────────────────────────────────

    def get_station_list(self, satellite_id: int) -> list:
        """Get all stations (zones) for a satellite."""
        return self._get("Station/GetStationListForSatellite", {"satelliteId": satellite_id}) or []

    def get_run_station_status(self, satellite_id: int) -> list:
        """Get real-time run status for all stations."""
        return self._get("ProgramStep/GetRunStationStatusForSatellite", {"satelliteId": satellite_id}) or []

    def get_programs_assigned_runtime(self, satellite_id: int) -> list:
        """Get assigned run times per station per program."""
        return self._get(
            "ProgramStep/GetProgramsAssignedAndRunTimeBySatelliteId",
            {"satelliteId": satellite_id}
        ) or []

    # ── Manual control ────────────────────────────────────────────────────────

    def start_station(self, station_id: int, seconds: int = 60) -> None:
        """Start a station manually for the given number of seconds."""
        self._post("ManualOps/StartStations", json={
            "stationIds": [station_id],
            "seconds": [seconds],
            "isGroupStart": False,
        })

    def stop_station(self, station_id: int) -> None:
        """Stop a station that is currently running."""
        self._post(
            "ManualOps/AdvanceStations",
            json=[{"programId": -1, "stationId": station_id}],
            params={"isProgramIndex": "true"},
        )

    def stop_all_stations(self, satellite_id: int) -> None:
        """Stop all running stations on a satellite."""
        stations = self.get_station_list(satellite_id)
        for station in stations:
            self._post(
                "ManualOps/AdvanceStations",
                json=[{"programId": -1, "stationId": station["id"]}],
                params={"isProgramIndex": "true"},
            )

    # ── Rain delay ────────────────────────────────────────────────────────────

    def set_rain_delay(self, satellite_id: int, days: int) -> None:
        """Set rain delay in days. Use 0 to clear the delay."""
        ticks = days * 24 * 3600 * 10_000_000  # .NET ticks (100ns units)
        start = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._patch("Satellite/v2/UpdateBatches", {
            "ids": [satellite_id],
            "patch": [
                {"op": "replace", "path": "/rainDelayLong", "value": ticks},
                {"op": "replace", "path": "/rainDelayStart", "value": start},
            ]
        })

    # ── Forecast rain delay ───────────────────────────────────────────────────

    def set_forecast(
        self,
        satellite_id: int,
        enabled: bool,
        percent: int | None = None,
        inches: float | None = None,
        delay_days: int | None = None,
    ) -> None:
        """Enable or disable forecast rain delay with optional parameters."""
        if enabled:
            patch = [
                {"op": "replace", "path": "/useForecast", "value": True},
                {"op": "replace", "path": "/forecastPercentLimit", "value": percent},
                {"op": "replace", "path": "/forecastInchesLimit", "value": inches},
                {"op": "replace", "path": "/forecastDelayDays", "value": delay_days},
            ]
        else:
            patch = [
                {"op": "replace", "path": "/useForecast", "value": False},
                {"op": "replace", "path": "/forecastPercentLimit"},
                {"op": "replace", "path": "/forecastInchesLimit"},
                {"op": "replace", "path": "/forecastDelayDays"},
            ]
        self._patch("Satellite/v2/UpdateBatches", {
            "ids": [satellite_id],
            "patch": patch,
        })

    # ── Seasonal adjust ───────────────────────────────────────────────────────

    def set_weather_adjust_method(self, program_id: int, method: int) -> None:
        """Set weather adjust method. 6=manual, 7=automatic seasonal adjust."""
        self._patch("Program/UpdateBatches", {
            "ids": [program_id],
            "patch": [
                {"op": "replace", "path": "/etAdjustType", "value": method},
            ]
        })

    def set_seasonal_adjust(self, program_id: int, percent: int) -> None:
        """Set manual seasonal adjust percentage (5-200)."""
        self._patch("Program/UpdateBatches", {
            "ids": [program_id],
            "patch": [
                {"op": "replace", "path": "/programAdjust", "value": percent},
            ]
        })

    # ── Sensors ───────────────────────────────────────────────────────────────

    def get_sensor_list(self, satellite_id: int) -> list:
        """Get all sensors attached to a satellite."""
        return self._get("Sensor/GetSensorListBySatelliteId", {"satelliteId": satellite_id}) or []

    # ── Flow ──────────────────────────────────────────────────────────────────

    def get_flow_elements(self, satellite_id: int) -> list:
        """Get flow zones for a satellite."""
        return self._get("FlowElement/GetFlowElements", {
            "parentId": "",
            "satelliteId": satellite_id,
            "includeHiddenFlowZones": False,
        }) or []

    def get_flow_monitoring(self, satellite_id: int) -> dict:
        """Get flow monitoring configuration."""
        return self._get("FlowMonitoring/GetFlowMonitoringBySatelliteId",
                         {"satelliteId": satellite_id}) or {}

    # ── Alerts ────────────────────────────────────────────────────────────────

    def get_company_status(self, company_id: int) -> dict:
        """Get company-level alarm and warning counts."""
        return self._get("Company/GetCompanyStatusCore", {"companyId": company_id}) or {}

    # ── Event log ─────────────────────────────────────────────────────────────

    def get_event_logs(self, satellite_id: int, hours: int = 24) -> list:
        """
        Get event logs for the last N hours.
        Returns empty list if the endpoint is not available (e.g. ESP-ME3 returns 403).

        Event numbers:
          97    — station turning on (eventParameter1 = terminal number)
          98    — station turning off (eventParameter1 = terminal number)
          15000 — irrigation completed (eventParameter1 = terminal number)
          15001 — seasonal adjust auto-changed
          15002 — rain delay enabled
          15011 — rain delay expired/disabled
        """
        now = datetime.datetime.now()
        start = (now - datetime.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
        end = (now + datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
        try:
            return self._post(
                "EventLog/GetEventLogsBySatelliteIds_V2",
                json=[satellite_id],
                params={
                    "startTime": start,
                    "endTime": end,
                    "types": 15,
                    "includeAcknowledgedAlarms": "true",
                    "includeAcknowledgedWarnings": "true",
                },
            ) or []
        except cf_requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 403:
                _LOGGER.debug(
                    "EventLog returned 403 for satellite %s, running zone detection unavailable",
                    satellite_id,
                )
                return []
            raise
