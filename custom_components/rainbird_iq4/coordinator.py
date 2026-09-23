"""Rain Bird IQ4 data update coordinators."""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
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

# How long we keep trusting our own "I asked Rain Bird to stop this zone"
# over the backend's reported status before giving up and falling back to
# whatever it says. This is a safety cap against a leaking dict — the
# override is normally cleared as soon as a fresh ON event proves a real
# restart happened, not by this timeout. See #12: after a manual stop
# (ManualOps/AdvanceStations), neither GetRunStationStatusForSatellite nor
# the EVENT_STATION_OFF (98) event log entry reliably reflects the stop —
# GetRunStationStatusForSatellite keeps reporting the original run window
# until it naturally expires, and event 98 can lag several minutes or
# never appear at all. So we override both with our own command.
MANUAL_STOP_MAX_AGE = timedelta(minutes=20)

# ── AppSync station-state tuning (MQTT controllers only) ─────────────────────
# All four numbers come from captures taken on 2026-09-08 and 2026-09-22; see
# RainBirdAPI.get_station_states for the raw behaviour they are derived from.

# When to take the first look after a command. The cloud accepts it long
# before anything happens: on 2026-09-23 a start was accepted at 12:19:22 and
# the controller only opened the valve at 12:19:30, writing its record at
# 12:19:33. A stop takes about as long to show up. Neither delay is trusted to
# be enough, which is why an unconfirmed command keeps booking another look
# (APPSYNC_RETRY_PROBE_DELAY) until it is confirmed or gives up.
APPSYNC_START_PROBE_DELAY = 8   # seconds
APPSYNC_STOP_PROBE_DELAY  = 15  # seconds
APPSYNC_RETRY_PROBE_DELAY = 8   # seconds

# How long our own command keeps overriding AppSync while it has not caught
# up. Rewrites were never slower than 22 s, so reaching this cap means the
# controller never carried the command out.
APPSYNC_CONFIRM_TIMEOUT = 60  # seconds

# A finished station keeps its record, still flagged as running, until the
# next rewrite. Past endsAt plus this margin the run is treated as over. The
# margin covers the jitter seen in endsAt within a single run (up to 5 s).
APPSYNC_END_GRACE = 10  # seconds

# How far endsAt has to move for the record to be a *different* run rather
# than the same one re-reported, which is how a stop override tells a real
# restart from the stale record it is waiting to see disappear.
APPSYNC_NEW_RUN_TOLERANCE = 10  # seconds

# Extra refresh scheduled for the moment a run is expected to end, so the
# zone does not sit on "running" until the next poll.
APPSYNC_END_PROBE_DELAY = APPSYNC_END_GRACE + 1  # seconds


def _parse_event_timestamp(ts: str | None) -> datetime | None:
    """Parse a Rain Bird event-log timestamp into a naive datetime.

    Timestamps come back in the same local/naive frame used to build the
    GetEventLog request (see RainBirdAPI.get_event_logs), so no timezone
    conversion is needed here — just enough parsing to compare ordering.
    """
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.split(".")[0])
    except Exception:
        return None


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
        self._manual_stops: dict[int, datetime] = {}

        # station_id -> monotonic expiry timestamp, set by a successful
        # start_zone call (see __init__.py._handle_start_zone). Neither
        # GetRunStationStatusForSatellite nor Rain Bird's AppSync push
        # channel reflect manually-started zones — the official app has
        # the same blind spot and works around it with a local countdown
        # instead of a confirmed hardware status. Program-triggered runs
        # do NOT need this; the live status endpoint reports them fine.
        self._optimistic_until: dict[int, float] = {}

        # ── AppSync path state (MQTT controllers) ────────────────────────
        # True while getStationStateList is answering; it decides whether a
        # command schedules a confirmation refresh at all, since on the REST
        # path there would be nothing new to read.
        self._appsync_active = False
        # station_id -> monotonic deadline: our command keeps overriding
        # AppSync until it catches up or the deadline passes.
        self._pending_starts: dict[int, float] = {}
        # station_id -> (monotonic deadline, endsAt of the run we stopped)
        self._pending_stops: dict[int, tuple[float, int | None]] = {}
        # station_id -> endsAt last seen, so a stop knows which run it ended.
        self._last_ends_at: dict[int, int | None] = {}
        # Single pending extra refresh, with its monotonic deadline.
        self._probe_unsub = None
        self._probe_at: float | None = None

    def set_optimistic_running(self, station_id: int, duration_seconds: int) -> None:
        """Mark a station as running locally for duration_seconds.

        Call this ONLY after a start_station API call has succeeded (i.e.
        did not raise) — an optimistic state must never be set for a
        command the backend didn't accept.
        """
        # Small safety margin so we don't flip back to idle a beat before
        # the physical valve actually closes.
        self._optimistic_until[station_id] = time.monotonic() + duration_seconds + 5
        # A fresh start supersedes any pending manual-stop override.
        self._manual_stops.pop(station_id, None)
        self._pending_stops.pop(station_id, None)
        # On the AppSync path the override is shorter-lived: it only bridges
        # the gap until the controller's record shows up, and a start that
        # never shows up is worth a warning.
        self._pending_starts[station_id] = time.monotonic() + APPSYNC_CONFIRM_TIMEOUT
        self._async_schedule_probe(APPSYNC_START_PROBE_DELAY)

    def mark_stopped(self, station_id: int) -> None:
        """Record that we just asked Rain Bird to stop this station.

        See MANUAL_STOP_MAX_AGE above for why this exists: the backend
        does not reliably reflect a manual stop right away (or at all), so
        we trust our own command until we see proof of a *new* start (a
        fresh ON event timestamped after this call).
        """
        self._manual_stops[station_id] = datetime.now()
        # A stop supersedes any pending optimistic-run countdown.
        self._optimistic_until.pop(station_id, None)
        self._pending_starts.pop(station_id, None)
        # AppSync path: hold the zone at idle until the record for the run we
        # just stopped is gone. endsAt identifies that run, so a station that
        # comes back with a different one is a genuine restart rather than the
        # stale record we are waiting on.
        self._pending_stops[station_id] = (
            time.monotonic() + APPSYNC_CONFIRM_TIMEOUT,
            self._last_ends_at.get(station_id),
        )
        self._async_schedule_probe(APPSYNC_STOP_PROBE_DELAY)

    def _resolve_appsync_station(
        self, station_id: int, terminal: int, record: dict | None, live_status: str
    ) -> tuple[bool, str, int | None]:
        """Decide a station's state from AppSync, honouring our own commands.

        Returns (is_running, status, run_ends_at).

        Only state 1 counts as irrigating: a queued station of a running
        program is listed too, with state 0. Pause still comes from the REST
        status, since AppSync has never been seen reporting one and neither
        the IQ4 app nor the website offer a pause control that could produce
        it — they only advance to the next station or cancel everything.
        """
        record = record or {}
        ends_at = record.get("endsAt")
        self._last_ends_at[station_id] = ends_at

        running = record.get("state") == 1
        if running and ends_at is not None and time.time() > ends_at + APPSYNC_END_GRACE:
            # The run is over and the controller simply has not rewritten its
            # record yet, which takes up to another 22 s.
            running = False

        # A stop we asked for wins until the record of the run we stopped is
        # gone. If the station comes back with a clearly different endsAt it
        # is a new run, so the override steps aside.
        pending_stop = self._pending_stops.get(station_id)
        if pending_stop is not None:
            deadline, stopped_ends_at = pending_stop
            restarted = (
                running
                and ends_at is not None
                and stopped_ends_at is not None
                and abs(ends_at - stopped_ends_at) > APPSYNC_NEW_RUN_TOLERANCE
            )
            if not running or restarted or time.monotonic() > deadline:
                del self._pending_stops[station_id]
            else:
                running = False

        # A start we asked for bridges the gap until its record shows up.
        pending_start = self._pending_starts.get(station_id)
        if pending_start is not None:
            if running:
                del self._pending_starts[station_id]
            elif time.monotonic() <= pending_start:
                running = True
            else:
                del self._pending_starts[station_id]
                _LOGGER.warning(
                    "Station %s (terminal %s): Rain Bird accepted the start but "
                    "the controller never reported it running within %ss",
                    station_id, terminal, APPSYNC_CONFIRM_TIMEOUT,
                )

        if live_status == "P":
            return False, "P", None
        return running, ("R" if running else "-"), (ends_at if running else None)

    @callback
    def _async_schedule_probe(self, delay: float) -> None:
        """Ask for one extra refresh in `delay` seconds.

        Only one probe is ever pending and an earlier one always wins, so a
        burst of commands cannot stack refreshes up. async_refresh is used
        rather than async_request_refresh because the coordinator's debouncer
        would hold the call back for its cooldown, which is longer than the
        delays this exists to hit.
        """
        if delay <= 0 or not self._appsync_active:
            return
        deadline = time.monotonic() + delay
        if (
            self._probe_unsub is not None
            and self._probe_at is not None
            and self._probe_at <= deadline
        ):
            return
        self._async_cancel_probe()
        self._probe_at = deadline
        self._probe_unsub = async_call_later(self.hass, delay, self._async_probe_fired)

    @callback
    def _async_probe_fired(self, _now) -> None:
        self._probe_unsub = None
        self._probe_at = None
        self.hass.async_create_task(self.async_refresh())

    @callback
    def async_cancel_probe(self) -> None:
        """Drop any pending probe, so no timer outlives the config entry."""
        self._async_cancel_probe()

    @callback
    def _async_cancel_probe(self) -> None:
        if self._probe_unsub is not None:
            self._probe_unsub()
            self._probe_unsub = None
        self._probe_at = None

    @callback
    def _async_schedule_followup(self, data: dict[str, Any]) -> None:
        """Book the next extra refresh after an update.

        Two things are worth coming back for: a command of ours that AppSync
        has not confirmed yet, and a run that is due to finish — without the
        latter a finished zone would report as running until the next poll,
        since the controller leaves its record in place for another rewrite
        cycle. Whichever falls first is the one that gets scheduled.
        """
        if self._pending_starts or self._pending_stops:
            self._async_schedule_probe(APPSYNC_RETRY_PROBE_DELAY)

        ends = [
            station["runEndsAt"]
            for station in data.get("stations", [])
            if station.get("isRunning") and station.get("runEndsAt")
        ]
        if ends:
            self._async_schedule_probe(min(ends) + APPSYNC_END_PROBE_DELAY - time.time())

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data = await self.hass.async_add_executor_job(self._fetch_data)
            self._consecutive_errors = 0
            self._async_schedule_followup(data)
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

        # State of the SEN terminals. Only AppSync reports this — the REST
        # sensor list's onOffState does not change when the terminals do.
        # None when the controller is not MQTT-based or the call failed.
        local_sensor = self.api.get_local_sensor_state(sid)

        # Live zone state from AppSync. None means we could not ask (non-MQTT
        # controller, or the call failed) and every zone falls back to the
        # REST status plus event log, exactly as before 1.5.0.
        station_states = self.api.get_station_states(sid)
        self._appsync_active = station_states is not None

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

        # Purge stale manual-stop markers so the dict doesn't grow forever
        # if a station never sees a confirming event again.
        now = datetime.now()
        self._manual_stops = {
            sid: ts for sid, ts in self._manual_stops.items()
            if now - ts < MANUAL_STOP_MAX_AGE
        }

        stations_data = []
        for s in stations:
            sid_key  = s["id"]
            terminal = s.get("terminal")
            live     = station_live.get(sid_key, {})
            events   = station_event_data.get(terminal, {})
            remaining = live.get("remaining")
            live_status = live.get("status", "-")
            run_ends_at = None

            if station_states is not None and terminal is not None:
                # AppSync knows about manual starts, program runs and queued
                # stations alike, so it replaces both the REST status and the
                # event-log inference for this controller.
                is_running, final_status, run_ends_at = self._resolve_appsync_station(
                    sid_key, terminal, station_states.get(terminal), live_status
                )
                if not is_running:
                    remaining = None
            elif live_status in ("R", "P"):
                # The real-time API gave an explicit status (running/paused) —
                # trust it. The event log must never override an explicit P,
                # since pausing a station doesn't emit a "station off" event
                # and would otherwise make a paused zone look like it's running.
                is_running  = live_status == "R"
                final_status = live_status
            else:
                # No explicit live status — fall back to the event log's
                # on/off tracking to infer whether the zone is running.
                is_running   = events.get("isRunning", False)
                final_status = "R" if is_running else live_status

            # See #12: after a manual stop, GetRunStationStatusForSatellite
            # keeps reporting the original run window (it doesn't cancel on
            # AdvanceStations) and EVENT_STATION_OFF can lag or never show
            # up. If we asked to stop this station, don't believe a "still
            # running" result from either source unless a fresher ON event
            # proves a genuine new start happened since then.
            # Optimistic override for manually-started zones. Manual
            # starts never appear in GetRunStationStatusForSatellite, so
            # live_status is always "-" for them here — this only ever
            # fires in the no-explicit-status branch above and never
            # overrides a real R/P from the API. Evaluated before the
            # manual-stop override below so that a stop always wins.
            optimistic_expiry = (
                None if station_states is not None and terminal is not None
                else self._optimistic_until.get(sid_key)
            )
            if optimistic_expiry is not None:
                if time.monotonic() < optimistic_expiry:
                    if live_status not in ("R", "P"):
                        is_running   = True
                        final_status = "R"
                else:
                    # Expired — stop overriding, let real data speak.
                    self._optimistic_until.pop(sid_key, None)

            stop_requested_at = (
                None if station_states is not None and terminal is not None
                else self._manual_stops.get(sid_key)
            )
            if stop_requested_at is not None:
                last_on = _parse_event_timestamp(events.get("lastRun"))
                if last_on is not None and last_on > stop_requested_at:
                    del self._manual_stops[sid_key]
                else:
                    is_running = False
                    final_status = "-" if final_status == "R" else final_status
                    remaining = None

            station_data = {
                "id":               sid_key,
                "name":             s.get("name"),
                "terminal":         terminal,
                "status":           final_status,
                "remaining":        remaining,
                "isRunning":        is_running,
                "lastRun":          events.get("lastRun"),
                "lastRunCompleted": events.get("lastRunCompleted"),
            }
            if station_states is not None and terminal is not None:
                # Only present on controllers that report their own end time.
                # Its absence is what tells the card that nothing better than
                # a local estimate will ever arrive for this zone.
                station_data["runEndsAt"] = run_ends_at
            stations_data.append(station_data)

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
            "localSensor": local_sensor,
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
