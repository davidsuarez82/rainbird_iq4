#!/usr/bin/env python3
"""
Rain Bird IQ4 - ESP-ME3 Diagnostic Script (curl_cffi)

Same purpose as diagnose_me3.sh (issues #9 and #10), but uses curl_cffi with
impersonate="chrome" for every request -- not just login -- exactly like the
integration itself (see api.py). The plain-curl bash version was showing
stale GetRunStationStatusForSatellite / EventLog responses (HTTP 200, but
content that never reflected a manual zone start) while Home Assistant, using
curl_cffi, picked up the same change within one ~30s poll cycle. This points
to Rain Bird's WAF/CDN serving a degraded or cached response to clients that
don't fingerprint as a real browser, rather than an auth-channel or
controller-model difference. This script rules that variable out.

Usage: python3 diagnose_me3.py <email> <password> [--channel web|app]
Requires: pip install curl_cffi
Output: me3_diagnostic_<satelliteId>.json
"""
import argparse
import base64
import hashlib
import json
import re
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urljoin, urlparse, parse_qs

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("email")
    parser.add_argument("password")
    parser.add_argument(
        "--channel", choices=["web", "app"], default="web",
        help="Authentication channel to test (default: web). Use 'app' to match "
             "the integration's Mobile app channel setting.",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print extra detail about the login flow (status codes, headers, "
             "and a snippet of the response body if login is rejected).",
    )
    args = parser.parse_args()

    print("🌧️  Rain Bird IQ4 ESP-ME3 Diagnostic (curl_cffi)")
    print("=" * 55)

    print(f"\n🔐 Step 1: Authenticating (channel={args.channel}, curl_cffi impersonate=chrome)...")
    login_session = cf.Session(impersonate="chrome")
    if args.channel == "app":
        token = fetch_token_app(login_session, args.email, args.password, verbose=args.verbose)
    else:
        token = fetch_token_web(login_session, args.email, args.password)
    print("✅ Authenticated successfully")

    # Clock check: compare local time against the server's own HTTP Date
    # header. A drifting local clock makes event timestamps look "frozen" or
    # "in the future" even when the data is perfectly fresh -- this bit us
    # during development of this script, so it's worth flagging up front.
    try:
        probe = login_session.get(f"{API_BASE}/Satellite/GetSatelliteList")
        server_date_hdr = probe.headers.get("Date")
        if server_date_hdr:
            from email.utils import parsedate_to_datetime
            server_time = parsedate_to_datetime(server_date_hdr)
            local_time = datetime.now(timezone.utc)
            drift = (local_time - server_time).total_seconds()
            if abs(drift) > 30:
                print(
                    f"⚠️  Clock drift detected: your system clock is "
                    f"{drift:+.0f}s relative to the Rain Bird server. "
                    f"Event timestamps in the output may look off -- this is "
                    f"a local clock issue, not a data issue. Consider "
                    f"checking your system's time sync (NTP/chrony)."
                )
            else:
                print(f"✅ Clock check OK (drift: {drift:+.0f}s)")
    except Exception:
        pass  # non-critical, don't block the diagnostic over this

    api = API(token)

    print("\n🔍 Step 2: Discovering controllers...")
    status, satellites = api.get("Satellite/GetSatelliteList", {"includeInvisibleToCurrentUser": "false"})
    if status != 200 or not satellites:
        print(f"❌ Could not list controllers (HTTP {status})")
        sys.exit(1)

    for i, s in enumerate(satellites):
        print(f"  [{i}] {s.get('name','?')} (ID: {s.get('id','?')}, Type: {s.get('type','?')})")

    if len(satellites) > 1:
        idx = int(input("Multiple controllers found. Enter the index to test: "))
    else:
        idx = 0
    satellite_id = satellites[idx]["id"]
    print(f"\n📡 Using Satellite ID: {satellite_id}")

    print("\n🔍 Step 3: One-time snapshot (issue #10 context)...")
    s_sat_status, s_sat_body = api.get("Satellite/GetSatellite", {"satelliteId": satellite_id})
    print(f"  GetSatellite: HTTP {s_sat_status}")
    s_prog_status, s_prog_body = api.get("Program/GetProgramList", {"satelliteId": satellite_id})
    print(f"  GetProgramList: HTTP {s_prog_status}")

    print("\n🔍 Step 4: Station list...")
    _, stations = api.get("Station/GetStationListForSatellite", {"satelliteId": satellite_id})
    for s in stations or []:
        print(f"  - {s.get('name','?')} (station id: {s.get('id','?')}, terminal: {s.get('terminal','?')})")

    print("\n" + "=" * 58)
    print("  Get ready to start a SINGLE zone manually (app or Home")
    print("  Assistant), the way you normally would when reproducing")
    print("  the bug.")
    print("=" * 58)
    input("  Start the zone now, THEN press Enter here to begin polling... ")

    print("\n🔄 Polling GetRunStationStatusForSatellite and EventLog every 5s for 2 minutes...")
    print("   (keep the zone running during this time if possible)\n")

    poll_log = []
    for i in range(1, 25):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Recompute the time window fresh on every poll, anchored to "now" --
        # exactly like get_event_logs() does on every real call. Reusing a
        # fixed window (computed once outside this loop) made every request
        # byte-for-byte identical across all 24 polls, which risked a cache
        # hit (CDN/proxy/backend) serving the same stale response repeatedly
        # regardless of HTTP client. This was likely the actual cause of the
        # "frozen" results seen in earlier runs of this script, not a
        # WAF/fingerprint or auth-channel difference.
        poll_now = datetime.now(timezone.utc)
        evt_start = (poll_now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        evt_end = (poll_now + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S")

        run_status, run_body = api.get(
            "ProgramStep/GetRunStationStatusForSatellite", {"satelliteId": satellite_id}
        )
        evt_status, evt_body = api.post(
            "EventLog/GetEventLogsBySatelliteIds_V2",
            json_body=[satellite_id],
            params={
                "startTime": evt_start,
                "endTime": evt_end,
                "types": 15,
                "includeAcknowledgedAlarms": "true",
                "includeAcknowledgedWarnings": "true",
            },
        )
        print(f"  [{i}/24] {ts}  RunStationStatus=HTTP {run_status}  EventLog=HTTP {evt_status}")
        poll_log.append({
            "poll": i,
            "timestamp": ts,
            "run_station_status": {"http_status": run_status, "body": run_body},
            "event_log": {"http_status": evt_status, "body": evt_body},
        })
        time.sleep(5)

    output_file = f"me3_diagnostic_{satellite_id}_{args.channel}.json"
    results = {
        "diagnostic_timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "satellite_id": satellite_id,
        "http_client": "curl_cffi (impersonate=chrome)",
        "auth_channel": args.channel,
        "snapshot": {
            "GetSatelliteList": satellites,
            "GetSatellite": {"http_status": s_sat_status, "body": s_sat_body},
            "GetProgramList": {"http_status": s_prog_status, "body": s_prog_body},
        },
        "polling_during_manual_start": poll_log,
    }
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2, default=str)

    print(f"\n✅ Diagnostic complete!\n📄 Results saved to: {output_file}")
    print("\nPlease share this file in the GitHub issue.")
    print("⚠️  The file contains your satellite ID and station data but NOT your password or token.")


if __name__ == "__main__":
    main()
