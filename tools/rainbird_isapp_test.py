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
the website. The token issued to the mobile app contains a claim
"isApp": "True", which the integration's website-based login does not
get. This script logs in exactly the way the mobile app does (same
OAuth client, same request shape) so we can compare the two tokens and
see whether "isApp" is the reason some accounts get blocked.

This script:
  1. Logs in with your Rain Bird username/password using the SAME OAuth
     client the mobile app uses.
  2. Prints out whether the resulting token has "isApp": True.
  3. Makes ONE harmless, READ-ONLY API call (GetSatelliteList) to confirm
     the token works at all.
  4. Does NOT start or stop any irrigation zone. It never sends water
     anywhere. It is 100% safe to run.

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
3. Run the script:
       python3 rainbird_isapp_test.py
4. Enter your Rain Bird IQ4 username and password when asked.
5. Copy the full output and send it back - no need to understand it,
   just paste everything that gets printed.
"""

import base64
import getpass
import json
import sys

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
MOBILE_SCOPE = "coreAPI.read coreAPI.write openid profile offline_access"


def decode_jwt_claims(token: str) -> dict:
    """Decode a JWT payload without verifying the signature (for display only)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception as exc:
        return {"_decode_error": str(exc)}


def main() -> None:
    print("=" * 70)
    print("Rain Bird IQ4 - isApp auth test")
    print("=" * 70)
    print(
        "\nThis will log in using the same method the Rain Bird 2.0 mobile\n"
        "app uses, and check one field in the resulting token. It will NOT\n"
        "start or stop any irrigation zone.\n"
    )

    username = input("Rain Bird username (email): ").strip()
    password = getpass.getpass("Rain Bird password (hidden while typing): ")

    if not username or not password:
        print("\nUsername and password are required. Exiting.")
        sys.exit(1)

    session = cf.Session(impersonate="chrome")

    basic_auth = base64.b64encode(
        f"{MOBILE_CLIENT_ID}:{MOBILE_CLIENT_SECRET}".encode()
    ).decode()

    print("\nStep 1/3: Logging in with the mobile app's OAuth client...")
    token_resp = session.post(
        f"{AUTH_BASE}/connect/token",
        headers={
            "Authorization": f"Basic {basic_auth}",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "*/*",
        },
        data={
            "grant_type": "password",
            "username": username,
            "password": password,
            "scope": MOBILE_SCOPE,
        },
    )

    if token_resp.status_code != 200:
        print(f"\nLogin FAILED - HTTP {token_resp.status_code}")
        print("Response body:")
        print(token_resp.text[:1000])
        print(
            "\nThis usually means either the username/password is wrong, or "
            "this particular login method (grant_type=password) is not "
            "accepted for your account. Please send this full output back."
        )
        sys.exit(1)

    try:
        token_json = token_resp.json()
    except Exception:
        print("\nLogin returned HTTP 200 but the response wasn't valid JSON:")
        print(token_resp.text[:1000])
        sys.exit(1)

    access_token = token_json.get("access_token")
    if not access_token:
        print("\nLogin succeeded but no access_token was found in the response:")
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
    print("DONE. Please copy everything above and send it back.")
    print("No irrigation zone was started or stopped by this script.")
    print("=" * 70)


if __name__ == "__main__":
    main()
