#!/usr/bin/env python3
"""
Rain Bird IQ4 - sensor diagnostic (curl_cffi)

Dumps the raw sensor and satellite payloads the cloud API returns for your
controller, so we can see whether a physical rain sensor (e.g. a WR2
wireless rain/freeze sensor) is reported at all, and under which fields.

binary_sensor.py creates one entity per entry returned by
GetSensorListBySatelliteId with type != -1, without filtering by sensor
model. So a sensor that IQ4 reports should appear automatically. When it
doesn't, there are two possibilities this script tells apart:

  1. The sensor list is empty  -> the controller doesn't register the
     sensor as an object in the cloud, and the app's "Local rain detected"
     banner is composed from some other field. The script prints every
     satellite field whose name relates to rain/sensor/suspend state so we
     can find it.
  2. The sensor is present but filtered -> e.g. it comes back with
     type == -1, in which case the fix is in binary_sensor.py.

Standalone: does not import Home Assistant. Only needs curl_cffi.
Read-only: issues GET requests only, changes nothing on the account.

Usage:   python3 diagnose_sensors.py <email> [--channel web|app]
         (you'll be prompted for the password; passing it as an argument
          also works but leaves it in your shell history)
Requires: pip install curl_cffi
Output:  sensor_diagnostic_<satelliteId>.json

Coordinates, addresses, serial numbers and similar identifiers are redacted
by default; pass --no-redact to keep them.
"""
import argparse
import base64
import datetime
import getpass
import hashlib
import json
import re
import secrets
import sys
from urllib.parse import parse_qs, quote, urljoin, urlparse

try:
    from curl_cffi import requests as cf
except ImportError:
    print("Missing dependency. Install it with: pip install curl_cffi")
    sys.exit(1)

AUTH_BASE = "https://iq4server.rainbird.com/coreidentityserver"
API_BASE = "https://iq4server.rainbird.com/coreapi/api"

# Web/IQ channel
CLIENT_ID_WEB = "C5A6F324-3CD3-4B22-9F78-B4835BA55D25"

# Mobile app channel (Authorization Code + PKCE) -- mirrors const.py exactly
APP_CLIENT_ID = "5B0FA4CD-8248-4BEB-B89A-F0AF8A254DB5"
APP_CLIENT_SECRET = "537C58B6-DCCF-4718-BFE6-CCD0D3FCDC07"
APP_REDIRECT_URI = "com.rainbird.mobile://auth"
APP_SCOPE = "coreAPI.read coreAPI.write openid profile offline_access"
_MAX_REDIRECTS = 10

# Field names redacted unless --no-redact. Matched case-insensitively as
# substrings, so "latitude", "gpsLat" and "siteAddress1" all match.
REDACT_HINTS = (
    "latitude", "longitude", "address", "street", "zip", "postal",
    "serial", "macaddress", "imei", "iccid", "phone", "email",
    "password", "token", "apikey", "secret",
)

# Satellite fields worth surfacing when no sensor object is reported: the
# app's "Local rain detected" state has to come from one of these.
RAIN_HINTS = (
    "rain", "sensor", "suspend", "shutdown", "pause", "freeze",
    "weather", "delay", "hold",
)


def _make_pkce_pair() -> tuple[str, str]:
    """Generate a PKCE (code_verifier, code_challenge) pair using S256. Mirrors auth.py."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def fetch_token_web(session: cf.Session, username: str, password: str) -> str:
    """Web/IQ channel login -- mirrors auth.py's fetch_token()."""
    state = secrets.token_hex(8).upper()
    nonce = secrets.token_hex(8).upper()
    return_url_raw = (
        "/coreidentityserver/connect/authorize/callback"
        f"?client_id={CLIENT_ID_WEB}"
        "&redirect_uri=https%3A%2F%2Fiq4.rainbird.com%2Fauth.html"
        "&response_type=id_token%20token"
        "&scope=coreAPI.read%20coreAPI.write%20openid%20profile"
        f"&state={state}&nonce={nonce}"
    )
    return_url_encoded = quote(return_url_raw, safe="")
    login_url = f"{AUTH_BASE}/Account/Login?ReturnUrl={return_url_encoded}"

    r1 = session.get(login_url)
    if r1.status_code != 200:
        raise RuntimeError(f"Login page failed: HTTP {r1.status_code}")

    match = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', r1.text)
    if not match:
        raise RuntimeError("CSRF token not found in login page")
    csrf = match.group(1)

    r2 = session.post(
        login_url,
        data={
            "Username": username,
            "Password": password,
            "ReturnUrl": return_url_raw,
            "__RequestVerificationToken": csrf,
        },
        allow_redirects=True,
    )

    access_token = None
    for text in (r2.url, r2.text):
        m = re.search(r"access_token=([^&\"]+)", text or "")
        if m:
            access_token = m.group(1)
            break
    if not access_token:
        raise RuntimeError("Authentication failed. Check your username and password.")
    return access_token


def fetch_token_app(session: cf.Session, username: str, password: str, verbose: bool = False) -> str:
    """Mobile-app channel login (Authorization Code + PKCE) -- mirrors auth.py's
    fetch_token_isapp(). Not subject to the web-channel's IQ-Access-tier cap."""
    state = secrets.token_hex(8).upper()
    nonce = secrets.token_hex(8).upper()
    code_verifier, code_challenge = _make_pkce_pair()

    return_url_raw = (
        "/coreidentityserver/connect/authorize/callback"
        f"?client_id={APP_CLIENT_ID}"
        f"&redirect_uri={quote(APP_REDIRECT_URI, safe='')}"
        "&response_type=code"
        f"&code_challenge={code_challenge}"
        "&code_challenge_method=S256"
        f"&scope={quote(APP_SCOPE, safe='')}"
        f"&state={state}&nonce={nonce}"
    )
    login_url = f"{AUTH_BASE}/Account/Login?ReturnUrl={quote(return_url_raw, safe='')}"

    r1 = session.get(login_url)
    if verbose:
        print(f"  [verbose] GET login page: HTTP {r1.status_code}, {len(r1.text)} bytes")
    if r1.status_code != 200:
        raise RuntimeError(f"Login page failed: HTTP {r1.status_code}")

    match = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', r1.text)
    if not match:
        raise RuntimeError("CSRF token not found in login page")
    csrf = match.group(1)

    resp = session.post(
        login_url,
        data={
            "Username": username,
            "Password": password,
            "ReturnUrl": return_url_raw,
            "__RequestVerificationToken": csrf,
        },
        allow_redirects=False,
    )

    if verbose:
        loc = resp.headers.get("location") or resp.headers.get("Location")
        print(f"  [verbose] POST credentials: HTTP {resp.status_code}, location={loc!r}")
        print(f"  [verbose] response headers: {dict(resp.headers)}")

    if resp.status_code == 200 and not (
        resp.headers.get("location") or resp.headers.get("Location")
    ):
        snippet = re.sub(r"<script.*?</script>", "", resp.text, flags=re.S)
        snippet = re.sub(r"\s+", " ", snippet).strip()[:600]
        raise RuntimeError(
            "Login rejected (server returned the login page instead of "
            "redirecting) -- this may not be a credentials problem (e.g. rate "
            "limiting after repeated login attempts). Response body snippet:\n"
            f"{snippet}"
        )

    code = None
    current_url = login_url
    for _ in range(_MAX_REDIRECTS):
        location = resp.headers.get("location") or resp.headers.get("Location")
        if not location:
            raise RuntimeError(f"No redirect while logging in (HTTP {resp.status_code}).")
        absolute = urljoin(current_url, location)
        parsed = urlparse(absolute)
        found = parse_qs(parsed.query).get("code", [None])[0]
        if found:
            code = found
            break
        if parsed.scheme not in ("http", "https"):
            raise RuntimeError(f"Reached final redirect but found no authorization code: {absolute}")
        current_url = absolute
        resp = session.get(current_url, allow_redirects=False)

    if not code:
        raise RuntimeError("Too many redirects while logging in -- no code found.")

    basic = base64.b64encode(f"{APP_CLIENT_ID}:{APP_CLIENT_SECRET}".encode()).decode()
    token_resp = session.post(
        f"{AUTH_BASE}/connect/token",
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "*/*",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": APP_REDIRECT_URI,
        },
    )
    if token_resp.status_code != 200:
        raise RuntimeError(
            f"Token exchange failed: HTTP {token_resp.status_code}, body: {token_resp.text[:200]}"
        )

    token = token_resp.json().get("access_token")
    if not token:
        raise RuntimeError("Token exchange succeeded but no access_token returned.")
    return token


class API:
    def __init__(self, token: str):
        self.session = cf.Session(impersonate="chrome")
        self.token = token

    def get(self, path: str, params: dict | None = None):
        r = self.session.get(
            f"{API_BASE}/{path}",
            params=params,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            timeout=30,
        )
        body = None
        try:
            body = r.json() if r.text.strip() else None
        except Exception as e:
            body = {"_parse_error": str(e), "_raw": r.text[:500]}
        return r.status_code, body

    def post(self, path: str, json_body=None, params: dict | None = None):
        r = self.session.post(
            f"{API_BASE}/{path}",
            params=params,
            json=json_body,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            timeout=30,
        )
        body = None
        try:
            body = r.json() if r.text.strip() else None
        except Exception as e:
            body = {"_parse_error": str(e), "_raw": r.text[:500]}
        return r.status_code, body

def redact(obj, enabled: bool = True):
    """Recursively blank out identifying fields, preserving structure."""
    if not enabled:
        return obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if any(h in k.lower() for h in REDACT_HINTS) and v not in (None, "", 0):
                out[k] = "<redacted>"
            else:
                out[k] = redact(v, enabled)
        return out
    if isinstance(obj, list):
        return [redact(i, enabled) for i in obj]
    return obj


def rain_related(satellite: dict) -> dict:
    """Return the satellite fields whose names relate to rain/sensor state."""
    if not isinstance(satellite, dict):
        return {}
    return {
        k: v for k, v in satellite.items()
        if any(h in k.lower() for h in RAIN_HINTS)
    }


def main():
    parser = argparse.ArgumentParser(
        description="Dump raw Rain Bird IQ4 sensor data for troubleshooting."
    )
    parser.add_argument("email")
    parser.add_argument(
        "password", nargs="?", default=None,
        help="Optional. Leave it out and you'll be prompted instead, which "
             "keeps your password out of the shell history and out of ps.",
    )
    parser.add_argument(
        "--channel", choices=["web", "app"], default="web",
        help="Authentication channel (default: web). Use 'app' to match the "
             "integration's Mobile app channel setting.",
    )
    parser.add_argument(
        "--satellite", type=int, default=None,
        help="Satellite id to inspect. Defaults to the first one on the account.",
    )
    parser.add_argument(
        "--no-redact", action="store_true",
        help="Keep coordinates, addresses and serials in the output.",
    )
    args = parser.parse_args()
    do_redact = not args.no_redact

    password = args.password
    if not password:
        password = getpass.getpass(f"Rain Bird password for {args.email}: ")
    if not password:
        print("No password given.")
        return 1

    print(f"\nStep 1: authenticating (channel={args.channel})...")
    login_session = cf.Session(impersonate="chrome")
    try:
        if args.channel == "app":
            token = fetch_token_app(login_session, args.email, password)
        else:
            token = fetch_token_web(login_session, args.email, password)
    except RuntimeError as err:
        print(f"  FAILED: {err}")
        if "202" in str(err) or "CSRF" in str(err):
            print("  A 202 or a missing CSRF token usually means Rain Bird's bot")
            print("  protection blocked the request. Wait a few minutes, or try")
            print("  the other --channel.")
        return 1
    print("  ok")

    api = API(token)
    report = {
        "generatedAt": datetime.datetime.now().isoformat(timespec="seconds"),
        "channel": args.channel,
        "redacted": do_redact,
    }

    print("\nStep 2: listing satellites...")
    status, satellites = api.get(
        "Satellite/GetSatelliteList", {"includeInvisibleToCurrentUser": "false"}
    )
    report["GetSatelliteList"] = {"status": status, "body": redact(satellites, do_redact)}
    print(f"  GetSatelliteList: HTTP {status}")
    if status != 200 or not satellites:
        print("  No satellites returned; cannot continue.")
        _write(report, "unknown")
        return 1

    satellite_id = args.satellite or satellites[0].get("id")
    print(f"  using satelliteId {satellite_id}")

    print("\nStep 3: fetching satellite detail...")
    s_status, satellite = api.get("Satellite/GetSatellite", {"satelliteId": satellite_id})
    report["GetSatellite"] = {"status": s_status, "body": redact(satellite, do_redact)}
    print(f"  GetSatellite: HTTP {s_status}")

    print("\nStep 4: fetching sensor list...")
    sensor_status, sensors = api.get(
        "Sensor/GetSensorListBySatelliteId", {"satelliteId": satellite_id}
    )
    report["GetSensorListBySatelliteId"] = {
        "status": sensor_status, "body": redact(sensors, do_redact)
    }
    print(f"  GetSensorListBySatelliteId: HTTP {sensor_status}")
    if isinstance(sensors, list):
        print(f"  {len(sensors)} sensor(s) reported")
        for s in sensors:
            print(f"    - id={s.get('id')} type={s.get('type')} "
                  f"name={s.get('name')!r} triggered={s.get('triggered')}")
        if not sensors:
            print("    (empty list - the controller reports no sensor objects)")
    else:
        print(f"  unexpected payload: {type(sensors).__name__}")

    print("\nStep 5: connection state...")
    c_status, connected = api.get("Satellite/isConnected", {"satelliteIds": satellite_id})
    report["isConnected"] = {"status": c_status, "body": connected}
    print(f"  isConnected: HTTP {c_status} -> {connected}")

    if isinstance(satellite, dict):
        print("\nLocal sensor configuration:")
        local = satellite.get("localSensor")
        types = satellite.get("localSensorTypes")
        print(f"    localSensor      = {local!r}"
              f"{'   (-1 means no local sensor configured)' if local == -1 else ''}")
        print(f"    localSensorTypes = {types!r}")
        print("    (localSensorTypes is the list of sensor types this controller")
        print("     accepts; a configured sensor shows up as one of those values)")

    fields = rain_related(satellite)
    report["rainRelatedSatelliteFields"] = redact(fields, do_redact)
    print("\nRain/sensor-related fields on the satellite:")
    if fields:
        for k, v in sorted(fields.items()):
            print(f"    {k} = {v!r}")
    else:
        print("    (none found)")

    path = _write(report, satellite_id)
    print(f"\nWrote {path}")
    print("Please skim the file before sharing it, even with redaction on.")
    return 0


def _write(report: dict, satellite_id) -> str:
    path = f"sensor_diagnostic_{satellite_id}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    return path


if __name__ == "__main__":
    sys.exit(main())
