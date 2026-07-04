#!/usr/bin/env python3
"""
Rain Bird IQ4 - "isApp" auth test
=================================

WHAT THIS SCRIPT DOES
----------------------
Some Rain Bird IQ4 accounts get a "403 Permission denied" error when the
rainbird_iq4 Home Assistant integration tries to start/stop zones, even
though the exact same account can control zones fine from the official
"Rain Bird 2.0" mobile app.

We captured mobile app traffic and found that it authenticates using a
DIFFERENT OAuth client than the one the integration uses to log in via
the website (Authorization Code + PKCE flow with its own client_id).
The token issued to the mobile app contains a claim "isApp": "True",
which the integration's website-based login does not get. This script
logs in exactly the way the mobile app does (same OAuth client, same
flow) so we can compare the two tokens and see whether "isApp" is the
reason some accounts get blocked.

This script:
  1. Logs in with your Rain Bird username/password, replicating the
     exact same login flow the mobile app uses (Authorization Code +
     PKCE, same OAuth client as the app).
  2. Prints out whether the resulting token has "isApp": True.
  3. Makes ONE harmless, READ-ONLY API call (GetSatelliteList) to confirm
     the token works at all.

After that, the MAIN test is done and safe to stop there - just send
back everything printed so far.

There is also an OPTIONAL extra step at the very end, which is OFF by
default: briefly starting one irrigation zone using this same login, to
see whether it succeeds where the Home Assistant integration gets a
403 error. This step:
  - Only runs if you explicitly type "yes" when asked.
  - Asks you to pick which controller and zone to test.
  - Asks for a SECOND confirmation (typing "CONFIRM") before doing
    anything.
  - Runs the zone for a short time you choose (5 seconds by default,
    15 seconds maximum).
  - Automatically sends a stop command afterwards as a safety net.
  - WILL physically turn on a sprinkler zone for a few seconds. Only
    say "yes" to this step if that's okay right now.

If you just want to check the "isApp" value, you can stop as soon as
you see "Main test DONE" and skip the optional part entirely (press
Enter instead of typing "yes").

Your username and password are sent only to Rain Bird's own servers
(iq4server.rainbird.com), exactly like the mobile app would. Nothing is
sent anywhere else, and nothing is saved to disk.

HOW TO RUN
----------
1. Make sure you have Python 3 installed.
2. Install one dependency:
       pip install curl_cffi
   (or, if that command doesn't work on your system:
       python3 -m pip install curl_cffi
   )

   If you're on Linux and see an error mentioning
   "externally-managed-environment", your system is blocking
   system-wide installs (this is normal on recent Ubuntu/Debian).
   Use a virtual environment instead - copy/paste these lines:
       python3 -m venv rainbird_test_env
       source rainbird_test_env/bin/activate
       pip install curl_cffi
   Then run the script (step 3 below) from that same terminal window.
   When you're done you can close the terminal, or type "deactivate".

3. Run the script:
       python3 rainbird_isapp_test.py
4. Enter your Rain Bird IQ4 username and password when asked.
5. Copy the full output and send it back - no need to understand it,
   just paste everything that gets printed.
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
    print(
        "\nERROR: the 'curl_cffi' package is not installed.\n"
        "Please run:  pip install curl_cffi\n"
        "and then run this script again.\n"
    )
    sys.exit(1)


AUTH_BASE = "https://iq4server.rainbird.com/coreidentityserver"
API_BASE = "https://iq4server.rainbird.com/coreapi"

# Same OAuth client the official "Rain Bird 2.0" mobile app uses.
MOBILE_CLIENT_ID = "5B0FA4CD-8248-4BEB-B89A-F0AF8A254DB5"
MOBILE_CLIENT_SECRET = "537C58B6-DCCF-4718-BFE6-CCD0D3FCDC07"
MOBILE_REDIRECT_URI = "com.rainbird.mobile://auth"
MOBILE_SCOPE = "coreAPI.read coreAPI.write openid profile offline_access"

# The OAuth client the iq4.rainbird.com WEB PORTAL uses - this is what the
# Home Assistant integration currently logs in as. Its token carries
# isApp: false / isIQ: true. Used for the A/B comparison.
WEB_CLIENT_ID = "C5A6F324-3CD3-4B22-9F78-B4835BA55D25"
WEB_REDIRECT_URI = "https://iq4.rainbird.com/auth.html"

MAX_REDIRECTS = 10


def decode_jwt_claims(token: str) -> dict:
    """Decode a JWT payload without verifying the signature (for display only)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception as exc:
        return {"_decode_error": str(exc)}


def make_pkce_pair() -> tuple[str, str]:
    """Generate a PKCE code_verifier and its S256 code_challenge."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def login_and_get_authorization_code(
    session: "cf.Session", username: str, password: str
) -> tuple[str, str]:
    """
    Replicate the mobile app's login: submit credentials to the same web
    login form the website uses, but requesting an Authorization Code
    (with PKCE) for the mobile app's OAuth client instead of the
    website's implicit-flow token.

    Returns a (authorization_code, code_verifier) tuple.
    """
    state = secrets.token_hex(8).upper()
    nonce = secrets.token_hex(8).upper()
    code_verifier, code_challenge = make_pkce_pair()

    return_url_raw = (
        f"/coreidentityserver/connect/authorize/callback"
        f"?client_id={MOBILE_CLIENT_ID}"
        f"&redirect_uri={quote(MOBILE_REDIRECT_URI, safe='')}"
        f"&response_type=code"
        f"&code_challenge={code_challenge}"
        f"&code_challenge_method=S256"
        f"&scope={quote(MOBILE_SCOPE, safe='')}"
        f"&state={state}&nonce={nonce}"
    )
    return_url_encoded = quote(return_url_raw, safe="")
    login_url = f"{AUTH_BASE}/Account/Login?ReturnUrl={return_url_encoded}"

    # Step A: load login page and extract CSRF token
    r1 = session.get(login_url)
    if r1.status_code != 200:
        snippet = r1.text[:300].replace("\n", " ") if r1.text else "(empty body)"
        raise RuntimeError(
            f"Login page failed: HTTP {r1.status_code}\n"
            f"Response snippet: {snippet}\n"
            f"(HTTP 202 or similar non-200 codes here are often a temporary "
            f"AWS WAF challenge triggered by repeated attempts in a short "
            f"time - if you see that, wait 5-10 minutes and try again.)"
        )

    match = re.search(
        r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', r1.text
    )
    if not match:
        raise RuntimeError("CSRF token not found in login page")
    csrf = match.group(1)

    # Step B: submit credentials, WITHOUT following redirects - we need
    # to intercept the redirect that carries the authorization code,
    # including a final hop to a non-http redirect URI
    # (com.rainbird.mobile://auth) that the library can't follow itself.
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

    # A successful login responds with a redirect (3xx). If it comes back
    # 200, the login form was almost certainly re-rendered with an error
    # (usually wrong username/password).
    if resp.status_code == 200 and not (
        resp.headers.get("location") or resp.headers.get("Location")
    ):
        raise RuntimeError(
            "Login was rejected (the server returned the login page again "
            "instead of redirecting). This usually means the username or "
            "password is incorrect."
        )

    current_url = login_url
    for _ in range(MAX_REDIRECTS):
        location = resp.headers.get("location") or resp.headers.get("Location")
        if not location:
            raise RuntimeError(
                f"No redirect found (HTTP {resp.status_code}). "
                f"Login may have failed - check your username/password."
            )

        # Redirects can be relative (e.g. "/coreidentityserver/...") - resolve
        # them against the current URL before inspecting.
        absolute_location = urljoin(current_url, location)
        parsed = urlparse(absolute_location)

        # The authorization code may appear on any hop (either the custom
        # com.rainbird.mobile:// redirect, or occasionally an https hop).
        # Grab it as soon as we see it.
        code = parse_qs(parsed.query).get("code", [None])[0]
        if code:
            return code, code_verifier

        if parsed.scheme not in ("http", "https"):
            # Reached the app's custom redirect URI but there was no code
            # in it - something went wrong.
            raise RuntimeError(
                f"Reached final redirect but found no 'code' parameter: {absolute_location}"
            )

        # Still a normal http(s) hop with no code yet - follow it manually.
        current_url = absolute_location
        resp = session.get(current_url, allow_redirects=False)

    raise RuntimeError("Too many redirects while logging in - could not find the code.")


def login_web_portal_isiq(session: "cf.Session", username: str, password: str) -> str:
    """
    Log in exactly the way the Home Assistant integration does: the
    iq4.rainbird.com web portal's implicit flow (response_type=id_token
    token), which returns the access_token directly in the redirect URL
    fragment. The resulting token carries isApp: false / isIQ: true.

    Returns the access_token string.
    """
    state = secrets.token_hex(8).upper()
    nonce = secrets.token_hex(8).upper()

    return_url_raw = (
        f"/coreidentityserver/connect/authorize/callback"
        f"?client_id={WEB_CLIENT_ID}"
        f"&redirect_uri={quote(WEB_REDIRECT_URI, safe='')}"
        f"&response_type=id_token%20token"
        f"&scope=coreAPI.read%20coreAPI.write%20openid%20profile"
        f"&state={state}&nonce={nonce}"
    )
    login_url = f"{AUTH_BASE}/Account/Login?ReturnUrl={quote(return_url_raw, safe='')}"

    r1 = session.get(login_url)
    if r1.status_code != 200:
        raise RuntimeError(
            f"Web login page failed: HTTP {r1.status_code} "
            f"(possible WAF challenge - wait a few minutes and retry)"
        )
    m = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', r1.text)
    if not m:
        raise RuntimeError("CSRF token not found in web login page")
    csrf = m.group(1)

    # Implicit flow: let the library follow redirects; the token ends up in
    # the final URL fragment (#access_token=...).
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

    url = r2.url
    if "access_token=" in url:
        fragment = url.split("#")[1] if "#" in url else url.split("?", 1)[1]
        params = dict(p.split("=", 1) for p in fragment.split("&") if "=" in p)
        token = params.get("access_token")
        if token:
            return token

    raise RuntimeError(
        f"access_token not found in web-login redirect "
        f"(HTTP {r2.status_code}). The login may have been rejected "
        f"(wrong username/password) or blocked by the WAF."
    )


def choose_from_list(items: list, describe, prompt: str) -> dict:
    """Show a simple numbered menu and return the chosen item."""
    if len(items) == 1:
        print(f"\nOnly one option found: {describe(items[0])}")
        return items[0]

    print()
    for i, item in enumerate(items, start=1):
        print(f"  {i}. {describe(item)}")

    while True:
        choice = input(prompt).strip()
        try:
            idx = int(choice)
            if 1 <= idx <= len(items):
                return items[idx - 1]
        except ValueError:
            pass
        print(f"Please enter a number between 1 and {len(items)}.")


def attempt_start_zone(
    session, token, channel_label, satellite_id, station_id_int,
    station_name, duration=60,
):
    """
    Try to start one zone using the given token, then stop it.
    Returns a dict with the HTTP status codes and physical-observation
    result, so the caller can compare channels.
    """
    print("\n" + "-" * 70)
    print(f"CHANNEL: {channel_label}")
    print("-" * 70)

    start_payload = {
        "stationIds": [station_id_int],
        "seconds": [duration],
        "isGroupStart": False,
    }
    start_resp = session.post(
        f"{API_BASE}/api/ManualOps/StartStations",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
        },
        json=start_payload,
    )
    print(f"StartStations response: HTTP {start_resp.status_code}")
    if start_resp.text:
        print(f"  body: {start_resp.text[:300]}")

    saw_water = None
    if start_resp.status_code in (200, 204):
        print(
            f"\nThe API ACCEPTED the start command on the {channel_label} "
            f"channel (HTTP {start_resp.status_code})."
        )
        print(
            "Go check the actual sprinkler zone now (look or listen for "
            "water). Give it a few seconds - the controller may take a "
            "moment to react. It was started for 60s but we'll stop it as "
            "soon as you answer."
        )
        input("\nPress Enter once you're done checking: ")
        ans = input(
            f"Did the zone physically run on the {channel_label} channel? "
            f"(yes/no): "
        ).strip().lower()
        saw_water = ans in ("y", "yes")
        print(
            f">>> {channel_label}: physical run = "
            f"{'YES' if saw_water else 'NO'} <<<"
        )
    elif start_resp.status_code == 403:
        print(
            f"\n>>> {channel_label}: HTTP 403 Permission denied - this "
            f"channel is BLOCKED for zone control on this account. <<<"
        )
    else:
        print(
            f"\n>>> {channel_label}: unexpected HTTP "
            f"{start_resp.status_code}. <<<"
        )

    # Always send a stop for this channel's token, just in case.
    stop_resp = session.post(
        f"{API_BASE}/api/Satellite/StopAllIrrigation",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
        },
        json=[satellite_id],
    )
    print(f"Safety stop ({channel_label}): HTTP {stop_resp.status_code}")

    return {
        "channel": channel_label,
        "start_status": start_resp.status_code,
        "saw_water": saw_water,
    }


def main() -> None:
    print("=" * 70)
    print("Rain Bird IQ4 - isApp auth test")
    print("=" * 70)
    print(
        "\nThis will log in using the same method the Rain Bird 2.0 mobile\n"
        "app uses and check one field in the resulting token. The main test\n"
        "is read-only. (There is an optional zone test at the very end that\n"
        "is off by default and clearly asks before doing anything.)\n"
    )

    username = input("Rain Bird username (email): ").strip()
    password = getpass.getpass("Rain Bird password (hidden while typing): ")

    if not username or not password:
        print("\nUsername and password are required. Exiting.")
        sys.exit(1)

    session = cf.Session(impersonate="chrome")

    print("\nStep 1/3: Logging in the same way the mobile app does...")
    try:
        auth_code, code_verifier = login_and_get_authorization_code(
            session, username, password
        )
    except Exception as exc:
        print(f"\nLogin FAILED: {exc}")
        print("Please send this full output back.")
        sys.exit(1)

    basic_auth = base64.b64encode(
        f"{MOBILE_CLIENT_ID}:{MOBILE_CLIENT_SECRET}".encode()
    ).decode()

    token_resp = session.post(
        f"{AUTH_BASE}/connect/token",
        headers={
            "Authorization": f"Basic {basic_auth}",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "*/*",
        },
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "code_verifier": code_verifier,
            "redirect_uri": MOBILE_REDIRECT_URI,
        },
    )

    if token_resp.status_code != 200:
        print(f"\nToken exchange FAILED - HTTP {token_resp.status_code}")
        print("Response body:")
        print(token_resp.text[:1000])
        print("\nPlease send this full output back.")
        sys.exit(1)

    try:
        token_json = token_resp.json()
    except Exception:
        print("\nToken exchange returned HTTP 200 but the response wasn't valid JSON:")
        print(token_resp.text[:1000])
        sys.exit(1)

    access_token = token_json.get("access_token")
    if not access_token:
        print("\nToken exchange succeeded but no access_token was found in the response:")
        print(json.dumps(token_json, indent=2))
        sys.exit(1)

    print("Login succeeded.\n")

    print("Step 2/3: Checking token claims...")
    claims = decode_jwt_claims(access_token)

    interesting_keys = [
        "isApp",
        "isIQ",
        "site_type",
        "group_level",
        "company_id",
        "sub",
        "name",
    ]
    print("-" * 70)
    for key in interesting_keys:
        if key in claims:
            print(f"  {key:15s}: {claims[key]}")
    print("-" * 70)

    is_app = claims.get("isApp")
    print(f"\n>>> isApp = {is_app!r} <<<\n")

    print("Step 3/3: Confirming the token works with a read-only API call...")
    sat_resp = session.get(
        f"{API_BASE}/api/Satellite/GetSatelliteList",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "*/*",
        },
    )

    sats = None
    if sat_resp.status_code == 200:
        try:
            sats = sat_resp.json()
            print(f"Success - found {len(sats)} controller(s) on this account.")
        except Exception:
            print("Success (HTTP 200), but couldn't parse the list of controllers.")
    else:
        print(f"Read-only call FAILED - HTTP {sat_resp.status_code}")
        print(sat_resp.text[:500])

    print("\n" + "=" * 70)
    print("Main test DONE. Please copy everything above and send it back.")
    print("No irrigation zone was started or stopped yet.")
    print("=" * 70)

    # ------------------------------------------------------------------
    # OPTIONAL EXTRA TEST - only runs if you explicitly opt in below.
    # This WILL physically turn on a sprinkler zone for a few seconds.
    # ------------------------------------------------------------------
    print(
        "\nThere is an OPTIONAL extra test available: briefly starting one\n"
        "irrigation zone using this same mobile-app login, to see if it\n"
        "succeeds where the Home Assistant integration gets a 403 error.\n"
        "\n"
        "This WILL physically turn on a sprinkler zone for a few seconds.\n"
        "Only continue if that is okay right now (e.g. no one is standing\n"
        "in the area, and running the zone briefly won't cause a problem).\n"
    )
    proceed = input(
        "Type 'yes' to continue with the optional zone test, or press "
        "Enter to skip it: "
    ).strip().lower()

    if proceed != "yes":
        print("\nSkipped the optional zone test. Nothing further was done.")
        return

    if not sats:
        print(
            "\nCan't continue - the list of controllers wasn't available "
            "(see the error above). Skipping the zone test."
        )
        return

    chosen_sat = choose_from_list(
        sats,
        lambda s: f"{s.get('name', 'Unknown')} (model type {s.get('type', '?')})",
        "Enter the number of the controller you want to test: ",
    )
    satellite_id = chosen_sat.get("id")

    stations_resp = session.get(
        f"{API_BASE}/api/Station/GetStationListForSatellite",
        params={"satelliteId": satellite_id},
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "*/*",
        },
    )
    if stations_resp.status_code != 200:
        print(f"\nCouldn't fetch stations - HTTP {stations_resp.status_code}")
        print(stations_resp.text[:500])
        return

    stations = stations_resp.json()
    if not stations:
        print("\nNo zones/stations found for that controller. Skipping.")
        return

    chosen_station = choose_from_list(
        stations,
        lambda s: f"{s.get('name', 'Unknown')} (terminal {s.get('terminal', '?')})",
        "Enter the number of the zone to test: ",
    )
    station_id_int = chosen_station.get("id")
    station_name = chosen_station.get("name", "Unknown")

    print("\nChecking whether this controller is currently online...")
    conn_resp = session.get(
        f"{API_BASE}/api/Satellite/isConnected",
        params={"satelliteIds": satellite_id},
        headers={"Authorization": f"Bearer {access_token}", "Accept": "*/*"},
    )
    is_connected = None
    if conn_resp.status_code == 200:
        try:
            conn_data = conn_resp.json()
            entries = None
            if isinstance(conn_data, dict) and "satellites" in conn_data:
                entries = conn_data["satellites"]
            elif isinstance(conn_data, list):
                entries = conn_data
            if entries:
                first = entries[0]
                is_connected = first.get("isConnected") if isinstance(first, dict) else first
            elif isinstance(conn_data, dict) and "isConnected" in conn_data:
                is_connected = conn_data.get("isConnected")
        except Exception:
            pass

    if is_connected is False:
        print(
            "\nWARNING: this controller currently shows as OFFLINE / not "
            "connected to Rain Bird's cloud. Results may not be meaningful "
            "while it's offline. Consider waiting until it's online."
        )
    elif is_connected is True:
        print("This controller currently shows as ONLINE / connected. Good.")
    else:
        print(
            f"Couldn't clearly determine connectivity status "
            f"(raw response: {conn_resp.text[:200]!r})"
        )

    # ------------------------------------------------------------------
    # A/B comparison. We test the SAME account, SAME zone, back to back:
    #   - isIQ  channel (web portal login = what the integration uses)
    #   - isApp channel (mobile app login)
    # Because both run on the same account with the same subscription
    # state, comparing their HTTP results isolates the login CHANNEL as
    # the variable - independent of whether a subscription is active.
    # ------------------------------------------------------------------
    print(
        "\n"
        + "=" * 70
        + "\nA/B ZONE CONTROL TEST\n"
        + "=" * 70
        + "\nThis will try to start the SAME zone twice, back to back, using\n"
        "two different logins:\n"
        "  1) the WEB PORTAL login (isIQ) - what the integration uses\n"
        "  2) the MOBILE APP login (isApp)\n"
        "\n"
        "Each start requests 60 seconds but is stopped as soon as you\n"
        "answer, so the zone won't run long. Comparing the two results on\n"
        "the same account tells us whether the login channel is what makes\n"
        "the difference.\n"
    )
    confirm = input(
        f"Type 'CONFIRM' (all caps) to run the A/B test on '{station_name}': "
    ).strip()
    if confirm != "CONFIRM":
        print("\nNot confirmed - skipping. Nothing was started.")
        return

    # We already have the isApp access_token. Get the isIQ (web) token now.
    print("\nLogging in a second time via the web-portal channel (isIQ)...")
    isiq_result = None
    try:
        web_token = login_web_portal_isiq(session, username, password)
        web_claims = decode_jwt_claims(web_token)
        print(
            f"Web login OK. isApp={web_claims.get('isApp')!r} "
            f"isIQ={web_claims.get('isIQ')!r}"
        )
        isiq_result = attempt_start_zone(
            session, web_token, "isIQ (web portal)",
            satellite_id, station_id_int, station_name,
        )
    except Exception as exc:
        print(f"\nWeb-portal (isIQ) login/test failed: {exc}")

    # Now the isApp channel, reusing the token from the main test.
    isapp_result = attempt_start_zone(
        session, access_token, "isApp (mobile app)",
        satellite_id, station_id_int, station_name,
    )

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("A/B RESULT SUMMARY")
    print("=" * 70)

    def fmt(r):
        if not r:
            return "login/test failed"
        water = r["saw_water"]
        water_str = (
            "water ran" if water is True
            else "no water" if water is False
            else "n/a"
        )
        return f"HTTP {r['start_status']}  ({water_str})"

    print(f"  isIQ  (web portal, = integration): {fmt(isiq_result)}")
    print(f"  isApp (mobile app):                {fmt(isapp_result)}")
    print("=" * 70)
    print(
        "\nPlease copy EVERYTHING above (from the very top, including the\n"
        "isApp/isIQ claims and this summary) and send it back.\n"
        "No zone should still be running - a stop was sent for each channel."
    )


if __name__ == "__main__":
    main()
