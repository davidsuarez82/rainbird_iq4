"""Rain Bird IQ4 API client."""
from __future__ import annotations

import datetime
import json as json_lib
import logging
import threading
import time
from typing import Any

from curl_cffi import requests as cf_requests

from .auth import RainBirdAuth
from .const import API_BASE, APPSYNC_URL

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

    # deviceUUID and isMQTT come from GetSatelliteList and are properties of
    # the hardware, so they only change if the controller is replaced.
    _DEVICE_INFO_CACHE_TTL = 3600  # seconds

    def __init__(self, auth: RainBirdAuth) -> None:
        self._auth = auth
        self._local = threading.local()
        self._sessions: list[cf_requests.Session] = []
        self._sessions_lock = threading.Lock()
        self._station_list_cache: dict[int, tuple[float, list]] = {}
        self._station_list_cache_lock = threading.Lock()
        self._device_info_cache: dict[int, tuple[float, dict]] = {}
        self._device_info_cache_lock = threading.Lock()
        # Whether the current run of AppSync failures has already been
        # reported at WARNING level. Reset once a call succeeds.
        self._appsync_warned = False

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

        # Manual control commands are fire-and-forget: start_station and its
        # siblings discard the response body, so a backend that accepts the
        # request without acting on it leaves no trace at all. DukeMini (#13)
        # reports zone starts that silently do nothing and only take effect on
        # a second attempt, with nothing in the log. Record what the backend
        # actually replied so the next report carries the evidence.
        if path.startswith("ManualOps/"):
            _LOGGER.debug(
                "ManualOps reply: %s %s -> HTTP %s, body: %s",
                method, path, r.status_code, (r.text or "")[:300] or "<empty>",
            )

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

    # ── AppSync: state the REST API does not expose ──────────────────────────

    _DEVICE_STATE_QUERY = (
        "query getDeviceStateTable($PK: String, $SK: String) {"
        "  getDeviceStateTable(PK: $PK, SK: $SK) { SK Data TimeStamp }"
        "}"
    )

    # Key of the local sensor (SEN terminals) record in the device state table.
    SK_RAIN_SENSOR_STATE = "Event#RainSensorState"

    def _graphql(self, query: str, variables: dict | None = None) -> dict | None:
        """POST a GraphQL document to AppSync.

        Returns the `data` object, or None if the call failed for any reason.
        Callers must treat None as "no information" rather than as a negative
        answer.

        The token is sent without a "Bearer" prefix. Both forms were accepted
        on the accounts tested here, over the web and app channels alike, but
        one user reported the prefixed form being rejected; the bare token has
        worked everywhere it has been tried.

        A rejection here deliberately does NOT invalidate the shared token.
        AppSync refusing a call says nothing about whether the token is still
        good for the REST API, and invalidating it would force a full login on
        the next request — every 30 seconds from the realtime coordinator,
        which is exactly the kind of traffic that trips the AWS WAF challenge.
        Expiry is already handled by get_token(), and the REST client still
        invalidates on its own 401s.
        """
        session = self._session()
        payload = {"query": query, "variables": variables or {}}
        headers = {
            "Authorization": self._auth.get_token(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        try:
            r = session.post(APPSYNC_URL, json=payload, headers=headers, timeout=30)
            if r.status_code != 200:
                self._appsync_failed(
                    "AppSync returned HTTP %s: %s",
                    r.status_code, (r.text or "")[:200],
                )
                return None
            body = r.json()
        except Exception as err:
            self._appsync_failed("AppSync request failed: %s", err)
            return None

        if body.get("errors"):
            self._appsync_failed("AppSync GraphQL errors: %s", body["errors"])
            return None

        if self._appsync_warned:
            _LOGGER.info("AppSync reachable again")
            self._appsync_warned = False
        return body.get("data")

    def _appsync_failed(self, message: str, *args) -> None:
        """Log an AppSync failure: WARNING the first time, DEBUG after that.

        Failures used to be DEBUG-only, which left a user whose local sensor
        entity never appeared with nothing in the log to explain why. Warning
        on every poll would flood the log instead, so only the first failure
        of a run is raised, and recovery is announced once it clears.
        """
        if self._appsync_warned:
            _LOGGER.debug(message, *args)
            return
        _LOGGER.warning(
            message + " (further AppSync failures are logged at debug level)",
            *args,
        )
        self._appsync_warned = True

    @staticmethod
    def _parse_state_payload(raw) -> dict | None:
        """AppSync wraps state payloads as a JSON string inside `Data`."""
        if raw is None:
            return None
        if isinstance(raw, dict):
            return raw
        try:
            return json_lib.loads(raw)
        except (ValueError, TypeError):
            _LOGGER.debug("Could not parse AppSync state payload: %s", raw)
            return None

    def get_device_info(self, satellite_id: int) -> dict:
        """Return {"deviceUUID": str|None, "isMQTT": bool} for a satellite.

        Cached — these are hardware properties, not state.
        """
        now = time.monotonic()
        with self._device_info_cache_lock:
            cached = self._device_info_cache.get(satellite_id)
            if cached and now - cached[0] < self._DEVICE_INFO_CACHE_TTL:
                return cached[1]

        info = {"deviceUUID": None, "isMQTT": False}
        try:
            match = next(
                (s for s in self.get_satellite_list() if s.get("id") == satellite_id),
                None,
            )
            if match:
                info = {
                    "deviceUUID": match.get("deviceUUID"),
                    "isMQTT": bool(match.get("isMQTT")),
                }
        except Exception as err:
            _LOGGER.debug("Could not resolve device info for %s: %s", satellite_id, err)
            return info

        with self._device_info_cache_lock:
            self._device_info_cache[satellite_id] = (now, info)
        return info

    def get_local_sensor_state(self, satellite_id: int) -> dict | None:
        """State of the controller's local sensor (SEN) terminals.

        Returns {"state": int, "timestamp": int|None}, where state 1 means the
        circuit is open (what a rain sensor does when it trips) and 0 means
        closed. Controllers shipped without a sensor have a factory jumper
        across those terminals, which reads as 0 — electrically identical to a
        sensor reporting dry, and correct either way.

        None means we could not ask. The REST sensor list does NOT carry this:
        its `onOffState` was observed to stay at 0 with the terminals both
        bridged and open, so AppSync is the only source.
        """
        info = self.get_device_info(satellite_id)
        device_uuid = info.get("deviceUUID")
        if not device_uuid or not info.get("isMQTT"):
            return None

        data = self._graphql(
            self._DEVICE_STATE_QUERY,
            {"PK": device_uuid, "SK": self.SK_RAIN_SENSOR_STATE},
        )
        if data is None:
            return None

        record = data.get("getDeviceStateTable")
        if not record:
            return None

        parsed = self._parse_state_payload(record.get("Data"))
        if parsed is None or parsed.get("state") is None:
            return None

        return {"state": parsed["state"], "timestamp": record.get("TimeStamp")}

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

    def start_program(self, program_id: int) -> None:
        """Start a saved program manually ("Program Run" in the official app).

        Uses the program's own per-station run times — confirmed via
        traffic capture (2026-08-06): the app posts a bare list of program
        ids to this endpoint, with no satellite id or duration needed.
        """
        self._post("ManualOps/StartPrograms", json=[program_id])

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
