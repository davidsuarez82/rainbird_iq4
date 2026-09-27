#!/usr/bin/env python3
"""
Rain Bird IQ4 -- AppSync subscription probe.

Opens the same real-time WebSocket the iq4.rainbird.com website uses,
subscribes to onUpdateDeviceStateTable for one controller and records every
frame the server sends, with a timestamp, until the time runs out or the
server closes the connection.

It exists to answer, outside a browser and with the same token the
integration uses, the questions a push-based zone state depends on:

  * whether AppSync accepts that token over WebSocket at all
  * how zone changes arrive (manual starts, programs, stops, natural ends)
  * how regular the keep-alives are
  * what the server does once the token the connection was opened with
    expires: keep the connection, close it, or send an error

It never reconnects: a dropped connection is the result, not a problem to
work around. It never sends a command either; start and stop zones from the
Rain Bird app, the website or Home Assistant while it runs.

Needs curl_cffi (login, same as probe_appsync.py) and aiohttp (WebSocket):

    pip install curl_cffi aiohttp
    python3 probe_subscription.py you@example.com

Every frame goes to subscription_probe_<timestamp>.jsonl, one JSON object per
line. The access token is never printed or written to that file.
"""
import argparse
import asyncio
import base64
import datetime
import getpass
import json
import sys
import time
import uuid
from urllib.parse import urlparse

try:
    import aiohttp
except ImportError:
    print("Missing dependency. Install it with: pip install aiohttp")
    sys.exit(1)

# Login and controller lookup are shared with probe_appsync.py, which has to
# sit in the same directory. That also brings in the curl_cffi check.
from probe_appsync import (
    APPSYNC_URL_DEFAULT,
    cf,
    fetch_token_app,
    fetch_token_web,
    rest_get,
)

SUBSCRIPTION = (
    "subscription onUpdateDeviceStateTable($PK: String!) {\n"
    "  onUpdateDeviceStateTable(PK: $PK) {\n"
    "    PK\n    SK\n    Data\n    TimeStamp\n    __typename\n"
    "  }\n"
    "}\n"
)

# The website sends keep-alives every 60 s exactly. Twice that without any
# frame is worth a line in the log, but the probe keeps listening: a quiet
# connection that later resumes is an answer too.
SILENCE_WARNING = 70  # seconds


def token_expiry(token: str) -> datetime.datetime | None:
    """Read `exp` from the JWT without verifying it. Only used for display."""
    try:
        body = token.split(".")[1]
        body += "=" * (-len(body) % 4)
        claims = json.loads(base64.urlsafe_b64decode(body))
        return datetime.datetime.fromtimestamp(int(claims["exp"]))
    except (IndexError, KeyError, ValueError, TypeError):
        return None


def realtime_url(appsync_url: str, token: str) -> tuple[str, str]:
    """Build the AppSync real-time URL. Returns (url, api_host).

    AppSync serves subscriptions from a sibling host, appsync-realtime-api
    instead of appsync-api, and takes the auth header base64-encoded in the
    query string: that is what the website's graphql?header=... URL carries.
    """
    api_host = urlparse(appsync_url).netloc
    ws_host = api_host.replace("appsync-api", "appsync-realtime-api")
    header = {"Authorization": token, "host": api_host}
    encoded = base64.b64encode(json.dumps(header).encode()).decode()
    return f"wss://{ws_host}/graphql?header={encoded}&payload=e30=", api_host


def start_frame(sub_id: str, pk: str, token: str, api_host: str) -> dict:
    """The `start` frame, shaped exactly like the website's."""
    return {
        "id": sub_id,
        "type": "start",
        "payload": {
            "data": json.dumps({"query": SUBSCRIPTION, "variables": {"PK": pk}}),
            "extensions": {
                "authorization": {
                    "Authorization": token,
                    "host": api_host,
                    "x-amz-user-agent": "aws-amplify/2.0.8",
                }
            },
        },
    }


class Recorder:
    """Writes every event to the JSONL file and a short line to the console."""

    def __init__(self, path: str, expires_at: datetime.datetime | None):
        self._file = open(path, "a", encoding="utf-8")
        self._started = time.monotonic()
        self._expires_at = expires_at
        self.last_frame = time.monotonic()
        self.last_ka: float | None = None
        self.ka_count = 0
        self.max_ka_gap = 0.0
        self.data_count = 0
        self.errors: list = []

    def _suffix(self) -> str:
        if not self._expires_at:
            return ""
        delta = (datetime.datetime.now() - self._expires_at).total_seconds()
        if delta < 0:
            return ""
        return f"   [token expired {int(delta)} s ago]"

    def event(self, kind: str, console: str, **fields) -> None:
        now = datetime.datetime.now()
        record = {
            "at": now.isoformat(timespec="milliseconds"),
            "elapsed": round(time.monotonic() - self._started, 1),
            "event": kind,
            **fields,
        }
        self._file.write(json.dumps(record) + "\n")
        self._file.flush()
        print(f"{now:%H:%M:%S}  {console}{self._suffix()}")

    def close(self) -> None:
        self._file.close()


def describe_data(frame: dict, labels: dict) -> tuple[str, dict]:
    """Turn a data frame into a console line and structured fields."""
    item = ((frame.get("payload") or {}).get("data") or {}).get(
        "onUpdateDeviceStateTable"
    ) or {}
    # Data is a JSON string, but not always an object: station records hold
    # {"state": .., "remainSec": ..}, while RSSI holds a bare number.
    try:
        data = json.loads(item.get("Data") or "null")
    except ValueError:
        data = item.get("Data")
    stamp = item.get("TimeStamp")
    lag = round(time.time() - stamp, 1) if isinstance(stamp, (int, float)) else None
    stamp_text = (
        f"{datetime.datetime.fromtimestamp(stamp):%H:%M:%S}"
        if isinstance(stamp, (int, float)) else "?"
    )
    source = labels.get(frame.get("id"), "?")
    fields = {
        "subscription": source,
        "pk": item.get("PK"),
        "sk": item.get("SK"),
        "data": data,
        "timestamp": stamp,
        "lagSeconds": lag,
    }
    rendered = (
        " ".join(f"{k}={v}" for k, v in data.items())
        if isinstance(data, dict) else f"value={data}"
    )
    line = (
        f"DATA [{source}] {item.get('SK')}  {rendered}"
        + f"  TimeStamp={stamp_text}"
        + (f" (lag {lag} s)" if lag is not None else "")
    )
    return line, fields


async def run_probe(
    url: str,
    token: str,
    api_host: str,
    subscriptions: dict[str, str],
    duration: float,
    recorder: Recorder,
) -> str:
    """Hold the subscription open and record frames. Returns how it ended.

    subscriptions maps a label ("device", "gateway") to the PK to subscribe
    with. Kept free of any login code so it can be exercised against a local
    stand-in server.
    """
    deadline = time.monotonic() + duration
    labels: dict[str, str] = {}
    outcome = "time limit reached"

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(
            url, protocols=("graphql-ws",), heartbeat=None, autoping=True
        ) as ws:
            recorder.event("connected", "connected")
            await ws.send_json({"type": "connection_init"})

            for label, pk in subscriptions.items():
                sub_id = str(uuid.uuid4())
                labels[sub_id] = label
                await ws.send_json(start_frame(sub_id, pk, token, api_host))
                recorder.event("start_sent", f"start sent: {label} ({pk})",
                               subscription=label, id=sub_id, pk=pk)

            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        msg = await asyncio.wait_for(
                            ws.receive(), timeout=min(remaining, SILENCE_WARNING)
                        )
                    except asyncio.TimeoutError:
                        if time.monotonic() >= deadline:
                            break
                        silent = int(time.monotonic() - recorder.last_frame)
                        recorder.event("silence", f"no frame for {silent} s",
                                       silentSeconds=silent)
                        continue

                    if msg.type in (aiohttp.WSMsgType.CLOSE,
                                    aiohttp.WSMsgType.CLOSING,
                                    aiohttp.WSMsgType.CLOSED):
                        outcome = f"server closed the connection (code {ws.close_code})"
                        recorder.event("closed", outcome, code=ws.close_code,
                                       extra=str(msg.extra) if msg.extra else None)
                        break
                    if msg.type == aiohttp.WSMsgType.ERROR:
                        outcome = f"connection error: {ws.exception()}"
                        recorder.event("ws_error", outcome)
                        break
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue

                    recorder.last_frame = time.monotonic()
                    try:
                        frame = json.loads(msg.data)
                    except ValueError:
                        recorder.event("unparsed", f"unparsed frame: {msg.data[:200]}",
                                       raw=msg.data)
                        continue
                    kind = frame.get("type")

                    if kind == "ka":
                        now = time.monotonic()
                        gap = None if recorder.last_ka is None else round(now - recorder.last_ka, 1)
                        recorder.last_ka = now
                        recorder.ka_count += 1
                        if gap is not None:
                            recorder.max_ka_gap = max(recorder.max_ka_gap, gap)
                        recorder.event("ka", f"ka{f' (+{gap} s)' if gap else ''}", gapSeconds=gap)
                    elif kind == "connection_ack":
                        timeout = (frame.get("payload") or {}).get("connectionTimeoutMs")
                        recorder.event("connection_ack",
                                       f"connection_ack (connectionTimeoutMs={timeout})",
                                       connectionTimeoutMs=timeout)
                    elif kind == "start_ack":
                        label = labels.get(frame.get("id"), "?")
                        recorder.event("start_ack", f"start_ack: {label}", subscription=label)
                    elif kind == "data":
                        recorder.data_count += 1
                        line, fields = describe_data(frame, labels)
                        recorder.event("data", line, **fields)
                    else:
                        # error, connection_error, complete, or anything new.
                        # Recorded whole: these are exactly the frames the
                        # probe is here to catch.
                        recorder.errors.append(frame)
                        recorder.event(kind or "unknown", f"{(kind or 'unknown').upper()}: "
                                       f"{json.dumps(frame)[:300]}", frame=frame)
            finally:
                if not ws.closed:
                    for sub_id, label in labels.items():
                        await ws.send_json({"id": sub_id, "type": "stop"})
                    await ws.close()
    return outcome


def main() -> int:
    parser = argparse.ArgumentParser(description="Rain Bird IQ4 AppSync subscription probe")
    parser.add_argument("email")
    parser.add_argument(
        "password", nargs="?", default=None,
        help="Optional. Leave it out and you'll be prompted instead, which "
             "keeps your password out of the shell history and out of ps.",
    )
    parser.add_argument("--channel", choices=["web", "app"], default="web",
                        help="Authentication channel (default: web), as in the integration.")
    parser.add_argument("--satellite", type=int, default=None,
                        help="Satellite id to watch. Defaults to the first one on the account.")
    parser.add_argument("--gateway", action="store_true",
                        help="Also subscribe with the controller's ioTGatewayUUID, "
                             "as the website does.")
    parser.add_argument("--hours", type=float, default=3.0,
                        help="How long to listen (default: 3). Pick more than the "
                             "token's remaining lifetime to see what happens at expiry.")
    parser.add_argument("--appsync-url", default=APPSYNC_URL_DEFAULT,
                        help="Override the AppSync endpoint.")
    args = parser.parse_args()

    password = args.password or getpass.getpass(f"Rain Bird password for {args.email}: ")
    if not password:
        print("No password given.")
        return 1

    with cf.Session(impersonate="chrome") as session:
        print(f"Authenticating ({args.channel} channel)...")
        try:
            fetch = fetch_token_web if args.channel == "web" else fetch_token_app
            token = fetch(session, args.email, password)
        except Exception as err:  # noqa: BLE001 - report and stop
            print(f"\nAuthentication failed: {err}")
            return 1

        sats, err = rest_get(session, token, "Satellite/GetSatelliteList",
                             {"includeInvisibleToCurrentUser": False})
        if err:
            print(f"GetSatelliteList failed: {err}")
            return 1
        targets = [s for s in (sats or [])
                   if args.satellite is None or s.get("id") == args.satellite]
        if not targets:
            print("No matching controller found.")
            return 1
        sat = targets[0]

    if not sat.get("deviceUUID"):
        print(f"{sat.get('name')} has no deviceUUID, so there is nothing to subscribe to.")
        return 1

    subscriptions = {"device": sat["deviceUUID"]}
    if args.gateway and sat.get("ioTGatewayUUID"):
        subscriptions["gateway"] = sat["ioTGatewayUUID"]

    expires_at = token_expiry(token)
    stamp = datetime.datetime.now()
    out_path = f"subscription_probe_{stamp:%Y%m%d_%H%M%S}.jsonl"
    url, api_host = realtime_url(args.appsync_url, token)

    print(f"Controller: {sat.get('name')} (id {sat.get('id')}, isMQTT={sat.get('isMQTT')})")
    if expires_at:
        minutes = int((expires_at - stamp).total_seconds() // 60)
        print(f"Token expires at {expires_at:%H:%M:%S} (in {minutes} min)")
    print(f"Listening for {args.hours} h. Frames go to {out_path}. Ctrl+C stops it.\n")

    recorder = Recorder(out_path, expires_at)
    recorder.event("probe_started", "probe started",
                   controller=sat.get("name"), satelliteId=sat.get("id"),
                   isMQTT=sat.get("isMQTT"), subscriptions=subscriptions,
                   tokenExpiresAt=expires_at.isoformat() if expires_at else None,
                   channel=args.channel)

    try:
        outcome = asyncio.run(run_probe(url, token, api_host, subscriptions,
                                        args.hours * 3600, recorder))
    except KeyboardInterrupt:
        outcome = "stopped by user"
    except aiohttp.ClientError as err:
        outcome = f"could not connect: {err}"

    ended = datetime.datetime.now()
    summary = {
        "outcome": outcome,
        "endedAt": ended.isoformat(timespec="seconds"),
        "tokenExpiresAt": expires_at.isoformat(timespec="seconds") if expires_at else None,
        "secondsPastTokenExpiry": (
            int((ended - expires_at).total_seconds()) if expires_at else None
        ),
        "keepAlives": recorder.ka_count,
        "maxKeepAliveGapSeconds": recorder.max_ka_gap,
        "dataFrames": recorder.data_count,
        "otherFrames": len(recorder.errors),
    }
    recorder.event("probe_finished", f"finished: {outcome}", **summary)
    recorder.close()

    print("\nSummary")
    for key, value in summary.items():
        print(f"  {key}: {value}")
    print(f"\nFull record: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
