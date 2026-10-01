"""Live controller state over the AppSync WebSocket.

The IQ4 website keeps a WebSocket open to AppSync and subscribes to
onUpdateDeviceStateTable, which is the table the controller itself writes to.
Everything in it arrives about a second after it happens, against the ten to
thirty seconds polling takes, and a run's end arrives explicitly as state -1
rather than having to be inferred.

Measured on an ESP-TM2 (2026-09-24), and the numbers below come from it:

* Keep-alives arrive every 60.0 s over hours at a time, but not always: on
  the night of 2026-09-29 four gaps of about 96 s were measured within half
  an hour, each followed by data as if nothing had happened. One missed beat
  therefore means nothing, which is why the connection is only doubted after
  two.
* The token is only checked when the connection is opened. Data kept coming
  an hour and 43 minutes after the token the socket was opened with had
  expired, so there is no reason to reconnect ahead of expiry.
* Cutting the route to AppSync produces no error at all: the socket stays
  open and simply goes quiet, which is why silence is the only usable
  symptom.
* A three-minute cut healed itself. The queued keep-alives arrived in one
  burst, three of them in the same second, and the subscription carried on
  delivering. Hence silence degrades the connection but does not reconnect
  it until it passes the server's own connectionTimeoutMs of five minutes.

This module only reports what it receives. Deciding what a record means is
the coordinator's job, and polling continues regardless, both as the source
for everything this does not carry (alarms, programs, rain delay, the event
log) and as the safety net for when the socket goes quiet.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import aiohttp

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import APPSYNC_URL

_LOGGER = logging.getLogger(__name__)

# No frame at all for this long means the data is no longer trustworthy. The
# keep-alive cadence is 60 s, so this is two missed beats plus a margin: at
# 90 s a single late beat was enough to declare the connection bad and drop
# back to polling, six seconds before the next frame arrived, four times in
# one night. Detecting a real outage a minute later costs nothing, because
# polling covers the gap either way.
SILENCE_DEGRADED = 150  # seconds

# The server announces connectionTimeoutMs = 300000. Past that a connection
# that has said nothing is dead however healthy the socket looks.
SILENCE_DEAD = 300  # seconds

# How often the read loop wakes up to measure the silence.
WATCHDOG_TICK = 10  # seconds

# Reconnection backoff, in seconds. Capped at the last value.
RECONNECT_BACKOFF = (5, 15, 30, 60, 120, 300)

STATE_TABLE_SUBSCRIPTION = (
    "subscription onUpdateDeviceStateTable($PK: String!) {\n"
    "  onUpdateDeviceStateTable(PK: $PK) {\n"
    "    PK\n    SK\n    Data\n    TimeStamp\n    __typename\n"
    "  }\n"
    "}\n"
)


def realtime_url(appsync_url: str, token: str) -> tuple[str, str]:
    """Build the AppSync real-time URL. Returns (url, api_host).

    Subscriptions are served from a sibling host — appsync-realtime-api
    instead of appsync-api — and the authorisation header travels
    base64-encoded in the query string, which is what the website's
    graphql?header=... URL carries.
    """
    api_host = urlparse(appsync_url).netloc
    ws_host = api_host.replace("appsync-api", "appsync-realtime-api")
    header = base64.b64encode(
        json.dumps({"Authorization": token, "host": api_host}).encode()
    ).decode()
    return f"wss://{ws_host}/graphql?header={header}&payload=e30=", api_host


def start_frame(sub_id: str, pk: str, token: str, api_host: str) -> dict:
    """Build a subscription `start` frame, shaped as the website sends it."""
    return {
        "id": sub_id,
        "type": "start",
        "payload": {
            "data": json.dumps(
                {"query": STATE_TABLE_SUBSCRIPTION, "variables": {"PK": pk}}
            ),
            "extensions": {
                "authorization": {
                    "Authorization": token,
                    "host": api_host,
                    "x-amz-user-agent": "aws-amplify/2.0.8",
                }
            },
        },
    }


class RainBirdSubscription:
    """Keeps one WebSocket open and hands every record to a callback.

    on_record(sk, data, timestamp, source) is called in the event loop for
    each record, where sk is the record key ("Station3",
    "Event#RainSensorState", ...), data is the decoded Data payload and
    source is "device" or "gateway".

    on_health(healthy) is called whenever the connection's usefulness
    changes, so the caller can fall back to polling while it is quiet.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api: Any,
        satellite_id: int,
        on_record: Callable[[str, Any, int | None, str], None],
        on_health: Callable[[bool], None],
    ) -> None:
        self.hass = hass
        self._api = api
        self._satellite_id = satellite_id
        self._on_record = on_record
        self._on_health = on_health
        self._subscriptions: dict[str, str] = {}
        self._task: asyncio.Task | None = None
        self._healthy = False

    @property
    def healthy(self) -> bool:
        return self._healthy

    async def async_start(self) -> bool:
        """Resolve the controller and start listening. False if it cannot."""
        info = await self.hass.async_add_executor_job(
            self._api.get_device_info, self._satellite_id
        )
        if not info.get("isMQTT") or not info.get("deviceUUID"):
            _LOGGER.info(
                "Controller %s does not report over Rain Bird's MQTT channel "
                "(isMQTT=%s), so live updates are not available and its state "
                "keeps coming from polling. No controller reporting false has "
                "been seen so far — please report yours on GitHub",
                self._satellite_id, info.get("isMQTT"),
            )
            return False

        self._subscriptions = {"device": info["deviceUUID"]}
        if info.get("ioTGatewayUUID"):
            # The website also subscribes with the LNK module's own id, which
            # is where DevicePresence (the controller's online state) arrives.
            self._subscriptions["gateway"] = info["ioTGatewayUUID"]

        self._task = self.hass.async_create_background_task(
            self._async_run(), f"rainbird_iq4 subscription {self._satellite_id}"
        )
        return True

    async def async_stop(self) -> None:
        """Stop listening and drop the connection."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            self._async_set_healthy(False)

    @callback
    def _async_set_healthy(self, healthy: bool) -> None:
        if healthy == self._healthy:
            return
        self._healthy = healthy
        self._on_health(healthy)

    async def _async_run(self) -> None:
        """Hold the connection open, reconnecting when it dies."""
        attempt = 0
        while True:
            try:
                await self._async_session()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - never let the task die
                _LOGGER.debug("Live connection failed: %s", err)
            self._async_set_healthy(False)
            delay = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
            attempt += 1
            _LOGGER.debug("Reconnecting to AppSync in %ss", delay)
            await asyncio.sleep(delay)

    async def _async_session(self) -> None:
        """One connection, from open to death. Returns when it should be redone."""
        # get_token() does disk I/O and may log in again, so it stays off the
        # event loop. A fresh token every time is deliberate: the expired one
        # would be accepted on an open socket, but not to open a new one.
        token = await self.hass.async_add_executor_job(self._api.get_access_token)
        url, api_host = realtime_url(APPSYNC_URL, token)
        session = async_get_clientsession(self.hass)

        async with session.ws_connect(
            url, protocols=("graphql-ws",), heartbeat=None
        ) as ws:
            await ws.send_json({"type": "connection_init"})
            labels: dict[str, str] = {}
            for index, (label, pk) in enumerate(self._subscriptions.items()):
                sub_id = f"{self._satellite_id}-{label}-{index}"
                labels[sub_id] = label
                await ws.send_json(start_frame(sub_id, pk, token, api_host))

            last_frame = self.hass.loop.time()
            degraded = False

            while True:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=WATCHDOG_TICK)
                except asyncio.TimeoutError:
                    silence = self.hass.loop.time() - last_frame
                    if silence >= SILENCE_DEAD:
                        _LOGGER.info(
                            "No data from Rain Bird for %ss; reconnecting", int(silence)
                        )
                        return
                    if silence >= SILENCE_DEGRADED and not degraded:
                        degraded = True
                        _LOGGER.debug(
                            "No frame for %ss; falling back to polling until it resumes",
                            int(silence),
                        )
                        # The silence is logged before the health change so a
                        # noisy connection can be told apart from a dead one
                        # without turning debug logging on.
                        _LOGGER.info(
                            "No data from Rain Bird for %ss", int(silence)
                        )
                        self._async_set_healthy(False)
                    continue

                if msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    _LOGGER.debug("AppSync closed the connection (%s)", ws.close_code)
                    return
                if msg.type == aiohttp.WSMsgType.ERROR:
                    _LOGGER.debug("WebSocket error: %s", ws.exception())
                    return
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue

                last_frame = self.hass.loop.time()
                degraded = False
                if not self._handle_frame(msg.data, labels):
                    return

    def _handle_frame(self, raw: str, labels: dict[str, str]) -> bool:
        """Process one text frame. False means the connection must be redone."""
        try:
            frame = json.loads(raw)
        except ValueError:
            _LOGGER.debug("Unparsable frame: %s", raw[:200])
            return True

        kind = frame.get("type")
        if kind in ("ka", "start_ack", "connection_ack"):
            # Any of the three proves the connection is alive and authorised.
            # Keep-alives can arrive several at once after a network cut, and
            # that carries no meaning beyond the connection being back.
            self._async_set_healthy(True)
            return True
        if kind == "data":
            self._async_set_healthy(True)
            self._dispatch(frame, labels)
            return True
        if kind in ("error", "connection_error"):
            _LOGGER.warning(
                "Rain Bird rejected the live connection: %s", json.dumps(frame)[:300]
            )
            return False
        if kind == "complete":
            _LOGGER.debug("Subscription ended by the server")
            return False
        _LOGGER.debug("Unknown frame type %s", kind)
        return True

    def _dispatch(self, frame: dict, labels: dict[str, str]) -> None:
        item = ((frame.get("payload") or {}).get("data") or {}).get(
            "onUpdateDeviceStateTable"
        ) or {}
        sk = item.get("SK")
        if not sk:
            return
        # Data is a JSON string, though not always an object: station records
        # hold {"state": .., "remainSec": ..} while RSSI holds a bare number.
        try:
            data = json.loads(item.get("Data") or "null")
        except ValueError:
            data = item.get("Data")
        source = labels.get(frame.get("id"), "device")
        _LOGGER.debug("Live record from %s: %s = %s", source, sk, data)
        try:
            self._on_record(sk, data, item.get("TimeStamp"), source)
        except Exception:  # noqa: BLE001 - a bad record must not kill the socket
            _LOGGER.exception("Could not apply live record %s", sk)
