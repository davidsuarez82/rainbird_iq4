#!/usr/bin/env python3
"""
Rain Bird IQ4 - station list dump (READ-ONLY)
=============================================

Logs in the same way the mobile app does and prints the raw list of
stations/zones for each controller, showing every field. This is purely
diagnostic - it does NOT start or stop anything.

Purpose: confirm which field in the station list is the real stationId
used for control (e.g. a large internal id like 13867561), versus the
terminal number (1-4).

Run:
    pip install curl_cffi
    python3 rainbird_dump_stations.py
"""

import base64
import getpass
import hashlib
import json
import re
import secrets
import sys
from urllib.parse import quote, urlparse, parse_qs, urljoin

try:
    from curl_cffi import requests as cf
except ImportError:
    print("\nERROR: 'curl_cffi' not installed. Run:  pip install curl_cffi\n")
    sys.exit(1)

AUTH_BASE = "https://iq4server.rainbird.com/coreidentityserver"
API_BASE = "https://iq4server.rainbird.com/coreapi"
MOBILE_CLIENT_ID = "5B0FA4CD-8248-4BEB-B89A-F0AF8A254DB5"
MOBILE_CLIENT_SECRET = "537C58B6-DCCF-4718-BFE6-CCD0D3FCDC07"
MOBILE_REDIRECT_URI = "com.rainbird.mobile://auth"
MOBILE_SCOPE = "coreAPI.read coreAPI.write openid profile offline_access"
MAX_REDIRECTS = 10


def make_pkce_pair():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def login(session, username, password):
    state = secrets.token_hex(8).upper()
    nonce = secrets.token_hex(8).upper()
    code_verifier, code_challenge = make_pkce_pair()
    return_url_raw = (
        f"/coreidentityserver/connect/authorize/callback"
        f"?client_id={MOBILE_CLIENT_ID}"
        f"&redirect_uri={quote(MOBILE_REDIRECT_URI, safe='')}"
        f"&response_type=code&code_challenge={code_challenge}"
        f"&code_challenge_method=S256&scope={quote(MOBILE_SCOPE, safe='')}"
        f"&state={state}&nonce={nonce}"
    )
    login_url = f"{AUTH_BASE}/Account/Login?ReturnUrl={quote(return_url_raw, safe='')}"

    r1 = session.get(login_url)
    if r1.status_code != 200:
        raise RuntimeError(f"Login page HTTP {r1.status_code} (possible WAF challenge - wait and retry)")
    m = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', r1.text)
    if not m:
        raise RuntimeError("CSRF token not found")
    csrf = m.group(1)

    resp = session.post(
        login_url,
        data={"Username": username, "Password": password,
              "ReturnUrl": return_url_raw, "__RequestVerificationToken": csrf},
        allow_redirects=False,
    )
    if resp.status_code == 200 and not (resp.headers.get("location") or resp.headers.get("Location")):
        raise RuntimeError("Login rejected - check username/password")

    current = login_url
    for _ in range(MAX_REDIRECTS):
        loc = resp.headers.get("location") or resp.headers.get("Location")
        if not loc:
            raise RuntimeError(f"No redirect (HTTP {resp.status_code})")
        absolute = urljoin(current, loc)
        parsed = urlparse(absolute)
        code = parse_qs(parsed.query).get("code", [None])[0]
        if code:
            basic = base64.b64encode(f"{MOBILE_CLIENT_ID}:{MOBILE_CLIENT_SECRET}".encode()).decode()
            tok = session.post(
                f"{AUTH_BASE}/connect/token",
                headers={"Authorization": f"Basic {basic}",
                         "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                data={"grant_type": "authorization_code", "code": code,
                      "code_verifier": code_verifier, "redirect_uri": MOBILE_REDIRECT_URI},
            )
            tok.raise_for_status()
            return tok.json()["access_token"]
        if parsed.scheme not in ("http", "https"):
            raise RuntimeError(f"No code in final redirect: {absolute}")
        current = absolute
        resp = session.get(current, allow_redirects=False)
    raise RuntimeError("Too many redirects")


def main():
    username = input("Rain Bird username (email): ").strip()
    password = getpass.getpass("Rain Bird password (hidden): ")
    session = cf.Session(impersonate="chrome")

    print("\nLogging in...")
    token = login(session, username, password)
    print("Login OK.\n")

    sats = session.get(
        f"{API_BASE}/api/Satellite/GetSatelliteList",
        headers={"Authorization": f"Bearer {token}", "Accept": "*/*"},
    ).json()

    for sat in sats:
        sat_id = sat.get("id")
        sat_name = sat.get("name", "?")
        print("=" * 70)
        print(f"Controller: {sat_name} (satellite id={sat_id})")
        print("=" * 70)
        stations = session.get(
            f"{API_BASE}/api/Station/GetStationListForSatellite",
            params={"satelliteId": sat_id},
            headers={"Authorization": f"Bearer {token}", "Accept": "*/*"},
        ).json()

        print(f"Raw station list ({len(stations)} stations):\n")
        print(json.dumps(stations, indent=2))
        print()
        # Highlight the key fields side by side
        print("Summary of key fields per station:")
        for s in stations:
            print(
                f"  name={s.get('name'):<16} "
                f"id={s.get('id')!r:<12} "
                f"terminal={s.get('terminal')!r:<5} "
                f"stationNumber={s.get('stationNumber')!r}"
            )
        print()

    print("Done. This was read-only - nothing was started or stopped.")


if __name__ == "__main__":
    main()
