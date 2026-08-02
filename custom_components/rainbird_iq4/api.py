"""Rain Bird IQ4 API client."""
from __future__ import annotations

import datetime
import logging
import threading
import time
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

    Uses one persistent curl_cffi session per executor thread (sessions are
    not thread-safe, and the three coordinators may poll concurrently).
    Reusing sessions avoids a full TLS handshake on every request.
    """

    # Station lists (id/name/terminal) change essentially never in normal
    # operation, but were being re-fetched on every realtime poll (every
    # 30s by default) and every program poll. Cache them for a while to
    # cut needless cloud calls.
    _STATION_LIST_CACHE_TTL = 3600  # seconds

    def __init__(self, auth: RainBirdAuth) -> None:
        self._auth = auth
        self._local = threading.local()
        self._sessions: list[cf_requests.Session] = []
        self._sessions_lock = threading.Lock()
        self._station_list_cache: dict[int, tuple[float, list]] = {}
        self._station_list_cache_lock = threading.Lock()

    def _session(self) -> cf_requests.Session:
        """Return the persistent session for the current thread."""
        session = getattr(self._local, "session", None)
        if session is None:
            session = cf_requests.Session(impersonate="chrome")
            self._local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return session

    def close(self) -> None:
        """Close all sessions created by this client."""
        with self._sessions_lock:
            for session in self._sessions:
                try:
                    session.close()
                except Exception:
                    pass
            self._sessions.clear()

    # HTTP statuses that indicate a transient server-side failure and are
    # safe to retry. The app-channel StartStations occasionally returns 500
    # "transient failure" that succeeds on a retry.
    _TRANSIENT_STATUSES = (500, 502, 503, 504)
    _MAX_TRANSIENT_RETRIES = 2

    def _request(self, method: str, path: str, json: Any = None, params: dict | None = None) -> Any:
        """Perform a request on the thread-local session.

        Retries once on 401 (after refreshing the token) and up to a few
        times on transient 5xx errors with a short backoff.
        """
        url = f"{API_BASE}/{path}"
        session = self._session()

        r = session.request(method, url, json=json, params=params,
                            headers=self._auth.get_headers(), timeout=30)

        if r.status_code == 401:
            _LOGGER.debug("Token rejected, refreshing and retrying")
            self._auth.invalidate()
            r = session.request(method, url, json=json, params=params,
                                headers=self._auth.get_headers(), timeout=30)

        # Retry transient server errors (e.g. app-channel StartStations 500).
        attempt = 0
        while r.status_code in self._TRANSIENT_STATUSES and attempt < self._MAX_TRANSIENT_RETRIES:
            attempt += 1
            delay = 0.5 * attempt
            _LOGGER.debug(
                "Transient HTTP %s on %s %s, retry %d/%d after %.1fs",
                r.status_code, method, path, attempt, self._MAX_TRANSIENT_RETRIES, delay,
            )
            time.sleep(delay)
            r = session.request(method, url, json=json, params=params,
                                headers=self._auth.get_headers(), timeout=30)

        r.raise_for_status()
        return r.json() if r.text.strip() else None

    def _get(self, path: str, params: dict | None = None) -> Any:
        """Perform a GET request, retrying once on 401."""
        return self._request("GET", path, params=params)

    def _post(self, path: str, json: Any = None, params: dict | None = None) -> Any:
        """Perform a POST request, retrying once on 401."""
        return self._request("POST", path, json=json, params=params)

    def _patch(self, path: str, json: Any = None) -> Any:
        """Perform a PATCH request, retrying once on 401."""
        return self._request("PATCH", path, json=json)

    # ── Satellite ─────────────────────────────────────────────────────────────

    def get_satellite(self, satellite_id: int) -> dict | None:
        """Get full satellite (controller) details.
        Returns None if the endpoint is not available (e.g. ESP-ME3 returns 403)."""
        try:
            return self._get("Satellite/GetSatellite", {"satelliteId": satellite_id})
        except cf_requests.RequestsError as e:
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

    def get_station_list(self, satellite_id: int, force_refresh: bool = False) -> list:
        """Get all stations (zones) for a satellite.

        Station id/name/terminal data essentially never changes, so the
        result is cached for _STATION_LIST_CACHE_TTL seconds. Pass
        force_refresh=True to bypass the cache (e.g. after the user presses
        the Reload button and entities are being rebuilt).
        """
        with self._station_list_cache_lock:
            cached = self._station_list_cache.get(satellite_id)
            if not force_refresh and cached and (time.time() - cached[0]) < self._STATION_LIST_CACHE_TTL:
                return cached[1]

        stations = self._get("Station/GetStationListForSatellite", {"satelliteId": satellite_id}) or []

        with self._station_list_cache_lock:
            self._station_list_cache[satellite_id] = (time.time(), stations)

        return stations

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

    def stop_all_stations(self, satellite_id: int, station_ids: list[int] | None = None) -> None:
        """Stop running stations on a satellite in a single batch call.

        station_ids lets the caller target only the zones it already knows
        to be running (typically read straight from the realtime
        coordinator's cached data, at zero extra API cost). If None, falls
        back to targeting every station on the controller — the safe
        default for when no live status is available yet (e.g. right after
        startup before the first realtime refresh completes).
        """
        if station_ids is None:
            stations = self.get_station_list(satellite_id)
            station_ids = [s["id"] for s in stations]
        if not station_ids:
            return
        self._post(
            "ManualOps/AdvanceStations",
            json=[{"programId": -1, "stationId": station_id} for station_id in station_ids],
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
        except cf_requests.RequestsError as e:
            if e.response is not None and e.response.status_code == 403:
                _LOGGER.debug(
                    "EventLog returned 403 for satellite %s, running zone detection unavailable",
                    satellite_id,
                )
                return []
            raise
