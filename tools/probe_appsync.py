#!/usr/bin/env python3
"""
Rain Bird IQ4 -- AppSync (GraphQL) probe.

Community reporting suggests live device state (e.g. rain sensor state on
MQTT-based controllers) lives in an AWS AppSync GraphQL backend rather than
in the coreapi REST API. This script verifies that claim against your own
account and dumps whatever it can discover:

  * whether the IdentityServer bearer token is accepted by AppSync at all
  * GraphQL introspection (available queries, subscriptions, and the shape
    of the device-state type)
  * getDeviceStateTable for a set of candidate SK keys
  * any list-style query that returns every SK for a controller

Standalone: does not import Home Assistant. Only needs curl_cffi.

    pip install curl_cffi
    python3 probe_appsync.py you@example.com

Nothing here writes to your system. Every call is a read.

Output goes to appsync_diagnostic_<timestamp>.json. Coordinates, addresses,
serials and tokens are redacted by default; pass --no-redact to keep them.
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

# Reported by a community member (KRH1009) from a US account. Unverified for
# other regions -- the region is baked into the hostname, so EU accounts may
# well be served by a different one. --appsync-url overrides it.
APPSYNC_URL_DEFAULT = (
    "https://m3iuhu3l3zbjpkctbnh2of4chm.appsync-api.us-west-2.amazonaws.com/graphql"
)

# SK values to try against getDeviceStateTable. Only the first is confirmed;
# the rest are guesses following the same Event#<Name> convention and are
# expected to miss. Add your own with --extra-sk.
CANDIDATE_SKS = (
    "Event#RainSensorState",
    "Event#RainDelayState",
    "Event#ConnectivityState",
    "Event#IrrigationState",
    "Event#StationState",
    "Event#ZoneState",
    "Event#RunState",
    "Event#SensorState",
    "Event#FlowState",
    "Event#DeviceState",
    "Event#AlarmState",
    "Event#ShutdownState",
    "Event#ProgramState",
    "Event#ManualState",
    "Event#Heartbeat",
    "Event#Status",
    "State",
    "Status",
)

REDACT_HINTS = (
    "latitude", "longitude", "address", "street", "zip", "postal",
    "serial", "macaddress", "imei", "iccid", "phone", "email",
    "password", "token", "apikey", "secret",
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
    """Recursively blank out fields whose names look like personal data."""
    if not enabled:
        return obj
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if any(h in key.lower() for h in REDACT_HINTS) and value not in (None, "", 0):
                out[key] = "<redacted>"
            else:
                out[key] = redact(value, enabled)
        return out
    if isinstance(obj, list):
        return [redact(item, enabled) for item in obj]
    return obj


def rest_get(session, token, path, params=None):
    """GET against the coreapi REST API. Returns (payload, error)."""
    try:
        r = session.get(
            f"{API_BASE}/{path}",
            params=params,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=30,
        )
    except Exception as err:
        return None, f"request failed: {err}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    if not r.text.strip():
        return None, "empty response"
    try:
        return r.json(), None
    except Exception:
        return None, f"non-JSON response ({r.text[:120]!r})"


def graphql(session, url, token, query, variables=None, auth_style="bearer"):
    """POST a GraphQL document. Returns a dict describing the outcome."""
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if auth_style == "bearer":
        headers["Authorization"] = f"Bearer {token}"
    elif auth_style == "raw":
        headers["Authorization"] = token
    elif auth_style == "apikey":
        headers["x-api-key"] = token

    payload = {"query": query}
    if variables is not None:
        payload["variables"] = variables

    try:
        r = session.post(url, json=payload, headers=headers, timeout=30)
    except Exception as err:
        return {"error": f"request failed: {err}"}

    out = {"status": r.status_code}
    try:
        out["body"] = r.json()
    except Exception:
        out["body"] = r.text[:2000]
    return out


def ok(result):
    """True if the call returned data and no GraphQL errors."""
    body = result.get("body")
    return (
        result.get("status") == 200
        and isinstance(body, dict)
        and body.get("data") is not None
        and not body.get("errors")
    )


INTROSPECT_ROOT = """
query IntrospectRoot {
  __schema {
    queryType { name fields { name args { name type { name kind ofType { name kind } } } } }
    subscriptionType { name fields { name args { name } } }
    mutationType { name fields { name } }
  }
}
"""

INTROSPECT_TYPE = """
query IntrospectType($name: String!) {
  __type(name: $name) {
    name
    kind
    fields { name type { name kind ofType { name kind } } }
  }
}
"""

GET_DEVICE_STATE = """
query getDeviceStateTable($PK: String, $SK: String) {
  getDeviceStateTable(PK: $PK, SK: $SK) {
    SK
    Data
    __typename
  }
}
"""


FULL_INTROSPECTION = """
query FullIntrospection {
  __schema {
    queryType { name }
    subscriptionType { name }
    types {
      kind
      name
      fields(includeDeprecated: false) {
        name
        args { name type { ...TypeRef } }
        type { ...TypeRef }
      }
      inputFields { name type { ...TypeRef } }
      enumValues(includeDeprecated: false) { name }
    }
  }
}
fragment TypeRef on __Type {
  kind name
  ofType { kind name ofType { kind name ofType { kind name ofType { kind name } } } }
}
"""

# Argument names we can satisfy from the controller record, lowercased.
ARG_SOURCES = {
    "pk": "deviceUUID",
    "deviceuuid": "deviceUUID",
    "deviceid": "deviceUUID",
    "uuid": "deviceUUID",
    "thingname": "deviceUUID",
    "gatewayuuid": "ioTGatewayUUID",
    "iotgatewayuuid": "ioTGatewayUUID",
    "satelliteid": "satelliteId",
    "controllerid": "satelliteId",
    "siteid": "siteId",
    "companyid": "companyId",
}


def unwrap(type_ref):
    """Return (base_type_name, kind, required) for a possibly wrapped type."""
    required = type_ref.get("kind") == "NON_NULL"
    node = type_ref
    while node and node.get("kind") in ("NON_NULL", "LIST"):
        node = node.get("ofType")
    if not node:
        return None, None, required
    return node.get("name"), node.get("kind"), required


def index_types(schema):
    return {t["name"]: t for t in schema.get("types", []) if t.get("name")}


def selection_for(type_name, types, depth=0, max_depth=2):
    """Build a GraphQL selection set for a type, one level of nesting deep."""
    node = types.get(type_name)
    if not node or node.get("kind") not in ("OBJECT", "INTERFACE"):
        return ""
    parts = []
    for field in node.get("fields") or []:
        if field.get("args"):
            continue
        base, kind, _ = unwrap(field["type"])
        if kind in ("SCALAR", "ENUM"):
            parts.append(field["name"])
        elif depth < max_depth and base:
            inner = selection_for(base, types, depth + 1)
            if inner:
                parts.append(f"{field['name']} {{ {inner} }}")
    return " ".join(parts)


def gql_type_name(type_ref):
    """Render a type reference the way it must appear in a variable declaration."""
    kind = type_ref.get("kind")
    if kind == "NON_NULL":
        return gql_type_name(type_ref["ofType"]) + "!"
    if kind == "LIST":
        return "[" + gql_type_name(type_ref["ofType"]) + "]"
    return type_ref.get("name") or "String"


def build_call(field, types, context):
    """Return (document, variables, skipped_required) for a query field."""
    decls, args, variables = [], [], {}
    missing = []
    for arg in field.get("args") or []:
        source = ARG_SOURCES.get(arg["name"].lower())
        _, _, required = unwrap(arg["type"])
        if source and context.get(source) is not None:
            decls.append(f"${arg['name']}: {gql_type_name(arg['type'])}")
            args.append(f"{arg['name']}: ${arg['name']}")
            variables[arg["name"]] = context[source]
        elif required:
            missing.append(arg["name"])
    if missing:
        return None, None, missing

    base, kind, _ = unwrap(field["type"])
    selection = "" if kind in ("SCALAR", "ENUM") else selection_for(base, types)
    if kind not in ("SCALAR", "ENUM") and not selection:
        return None, None, ["<unrenderable return type>"]

    header = f"query Probe({', '.join(decls)})" if decls else "query Probe"
    call = field["name"] + (f"({', '.join(args)})" if args else "")
    doc = f"{header} {{ {call}{(' { ' + selection + ' }') if selection else ''} }}"
    return doc, variables, None


# Queries polled by --watch, with any argument that isn't derived from the
# controller record. Names and casing come straight from introspection --
# note getStationStateList uses "DeviceUUID", not "deviceUUID".
WATCH_QUERIES = (
    ("getStationStateList", {}),
    ("getProgramStateList", {}),
    ("getU2IrrigationQueuePages", {}),
    ("listRecentU2DeviceStateEvents", {"limit": 20}),
    ("getU2WeatherSensorNotifications", {"limit": 20}),
    ("getDeviceStateTable", {"SK": "Event#RainSensorState"}),
)


def is_empty(payload):
    """True for the various shapes AppSync uses to mean 'nothing here'."""
    if payload is None:
        return True
    if isinstance(payload, list):
        return not payload
    if isinstance(payload, dict):
        items = payload.get("items")
        if items is not None:
            return not items
    return False


def watch(session, url, token, auth_style, types, context, interval, duration, report):
    """Poll the state queries and print whatever changes."""
    import time

    root_name = context.pop("_queryRoot")
    query_root = types.get(root_name) or {}
    fields = {f["name"]: f for f in query_root.get("fields") or []}

    calls = {}
    for name, extra in WATCH_QUERIES:
        field = fields.get(name)
        if not field:
            print(f"  {name}: not in schema, skipping")
            continue
        merged = dict(context)
        # Let per-query extras satisfy arguments the controller record can't.
        for arg in field.get("args") or []:
            if arg["name"] in extra:
                merged[f"_literal_{arg['name']}"] = extra[arg["name"]]
        doc, variables, missing = build_call_with_literals(field, types, merged, extra)
        if missing:
            print(f"  {name}: cannot build call (needs {', '.join(missing)}), skipping")
            continue
        calls[name] = (doc, variables)

    print(f"\nPolling {len(calls)} queries every {interval}s for {duration}s.")
    print("Start a zone now -- only changes are printed.\n")

    samples = []
    previous = {}
    deadline = time.time() + duration
    while time.time() < deadline:
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        sample = {"at": stamp, "data": {}}
        for name, (doc, variables) in calls.items():
            res = graphql(session, url, token, doc, variables, auth_style=auth_style)
            payload = res["body"]["data"].get(name) if ok(res) else {"_error": res.get("status")}
            sample["data"][name] = payload

            rendered = json.dumps(payload, default=str, sort_keys=True)
            if rendered != previous.get(name):
                previous[name] = rendered
                if not is_empty(payload):
                    shown = rendered if len(rendered) <= 400 else rendered[:397] + "..."
                    print(f"[{stamp}] {name}\n          {shown}\n")
                else:
                    print(f"[{stamp}] {name} -> empty")
        samples.append(sample)
        time.sleep(interval)

    report["watchSamples"] = samples
    print(f"Captured {len(samples)} samples.")


def build_call_with_literals(field, types, context, literals):
    """build_call, but arguments named in `literals` are passed as given."""
    decls, args, variables = [], [], {}
    missing = []
    for arg in field.get("args") or []:
        name = arg["name"]
        _, _, required = unwrap(arg["type"])
        if name in literals:
            decls.append(f"${name}: {gql_type_name(arg['type'])}")
            args.append(f"{name}: ${name}")
            variables[name] = literals[name]
            continue
        source = ARG_SOURCES.get(name.lower())
        if source and context.get(source) is not None:
            decls.append(f"${name}: {gql_type_name(arg['type'])}")
            args.append(f"{name}: ${name}")
            variables[name] = context[source]
        elif required:
            missing.append(name)
    if missing:
        return None, None, missing

    base, kind, _ = unwrap(field["type"])
    selection = "" if kind in ("SCALAR", "ENUM") else selection_for(base, types)
    if kind not in ("SCALAR", "ENUM") and not selection:
        return None, None, ["<unrenderable return type>"]

    header = f"query Probe({', '.join(decls)})" if decls else "query Probe"
    call = field["name"] + (f"({', '.join(args)})" if args else "")
    doc = f"{header} {{ {call}{(' { ' + selection + ' }') if selection else ''} }}"
    return doc, variables, None


def main():
    parser = argparse.ArgumentParser(description="Rain Bird IQ4 AppSync probe")
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
    parser.add_argument("--appsync-url", default=APPSYNC_URL_DEFAULT,
                        help="override the AppSync endpoint")
    parser.add_argument("--device-uuid",
                        help="use this PK instead of the one from GetSatelliteList")
    parser.add_argument("--extra-sk", action="append", default=[],
                        help="additional SK value to try (repeatable)")
    parser.add_argument("--deep", action="store_true",
                        help="dump the full schema and auto-invoke every read "
                             "query whose arguments we can satisfy")
    parser.add_argument("--watch", action="store_true",
                        help="poll the state queries and print changes; run "
                             "this while starting a zone")
    parser.add_argument("--interval", type=int, default=10,
                        help="seconds between --watch polls (default: 10)")
    parser.add_argument("--duration", type=int, default=300,
                        help="how long --watch runs, in seconds (default: 300)")
    parser.add_argument(
        "--no-redact", action="store_true",
        help="Keep coordinates, addresses and serials in the output.",
    )
    args = parser.parse_args()

    username = args.email
    password = args.password
    if not password:
        password = getpass.getpass(f"Rain Bird password for {username}: ")
    if not password:
        print("No password given.")
        return 1
    do_redact = not args.no_redact

    report = {
        "generatedAt": datetime.datetime.now().isoformat(timespec="seconds"),
        "channel": args.channel,
        "appsyncUrl": args.appsync_url,
        "redacted": do_redact,
    }

    with cf.Session(impersonate="chrome") as session:
        print(f"Authenticating ({args.channel} channel)...")
        try:
            fetch = fetch_token_web if args.channel == "web" else fetch_token_app
            token = fetch(session, username, password)
        except Exception as err:
            print(f"\nAuthentication failed: {err}")
            return 1
        print("Authenticated.\n")

        # -- controller / PK -------------------------------------------------
        device_uuid = args.device_uuid
        if not device_uuid:
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
            device_uuid = sat.get("deviceUUID")
            report["controller"] = {
                "id": sat.get("id"),
                "name": sat.get("name"),
                "type": sat.get("type"),
                "version": sat.get("version"),
                "isMQTT": sat.get("isMQTT"),
                "deviceUUID": device_uuid,
                "ioTGatewayUUID": sat.get("ioTGatewayUUID"),
                "siteId": sat.get("siteId"),
                "companyId": sat.get("companyId"),
            }
            print(f"Controller: {sat.get('name')} "
                  f"(id {sat.get('id')}, type {sat.get('type')}, "
                  f"isMQTT={sat.get('isMQTT')})")

        if not device_uuid:
            print("No deviceUUID available -- pass --device-uuid explicitly.")
            return 1
        print(f"PK (deviceUUID): {device_uuid}\n")

        # -- does AppSync accept our token, and in what form? -----------------
        print("Testing authorization styles against AppSync...")
        report["authStyles"] = {}
        working_style = None
        for style in ("bearer", "raw"):
            res = graphql(session, args.appsync_url, token,
                          "query Ping { __typename }", auth_style=style)
            report["authStyles"][style] = res
            verdict = "accepted" if ok(res) else f"rejected (HTTP {res.get('status')})"
            print(f"  Authorization: {style:7s} -> {verdict}")
            if ok(res) and working_style is None:
                working_style = style

        if working_style is None:
            print("\nNo authorization style was accepted. The endpoint may be "
                  "region-specific, or it may need a credential we don't have.\n"
                  "Report written anyway so the error bodies can be inspected.")
            _write(report, do_redact)
            return 1
        print(f"\nUsing Authorization style: {working_style}\n")

        # -- introspection ---------------------------------------------------
        print("Introspecting schema...")
        root = graphql(session, args.appsync_url, token, INTROSPECT_ROOT,
                       auth_style=working_style)
        report["introspection"] = {"root": root}

        query_fields = []
        if ok(root):
            schema = root["body"]["data"].get("__schema") or {}
            query_fields = [f["name"] for f in (schema.get("queryType") or {}).get("fields", [])]
            sub_fields = [f["name"] for f in (schema.get("subscriptionType") or {}).get("fields", [])]
            print(f"  queries      ({len(query_fields)}): {', '.join(query_fields[:20]) or '-'}")
            print(f"  subscriptions ({len(sub_fields)}): {', '.join(sub_fields[:20]) or '-'}")
            if sub_fields:
                print("  ^ subscriptions exist: real-time state over WebSocket is possible")

            type_res = graphql(session, args.appsync_url, token, INTROSPECT_TYPE,
                               {"name": "DeviceStateTable"}, auth_style=working_style)
            report["introspection"]["DeviceStateTable"] = type_res
            if ok(type_res) and type_res["body"]["data"].get("__type"):
                names = [f["name"] for f in type_res["body"]["data"]["__type"]["fields"]]
                print(f"  DeviceStateTable fields: {', '.join(names)}")
        else:
            print("  introspection disabled or rejected "
                  f"(HTTP {root.get('status')}) -- falling back to known query")
        print()

        # -- any list-style query that returns every SK at once ---------------
        list_candidates = [f for f in query_fields
                           if "devicestate" in f.lower() and f.lower().startswith(("list", "query"))]
        if list_candidates:
            print(f"Trying list-style queries: {', '.join(list_candidates)}")
            report["listQueries"] = {}
            for field in list_candidates:
                doc = (f"query L($PK: String) {{ {field}(PK: $PK) "
                       f"{{ items {{ SK Data }} }} }}")
                res = graphql(session, args.appsync_url, token, doc,
                              {"PK": device_uuid}, auth_style=working_style)
                report["listQueries"][field] = res
                print(f"  {field}: {'ok' if ok(res) else 'failed'}")
            print()


        # -- deep schema dump + auto-invocation --------------------------------
        if args.deep:
            print("Fetching full schema...")
            full = graphql(session, args.appsync_url, token, FULL_INTROSPECTION,
                           auth_style=working_style)
            report["fullSchema"] = full
            if not ok(full):
                print(f"  full introspection failed (HTTP {full.get('status')})\n")
            else:
                schema = full["body"]["data"]["__schema"]
                types = index_types(schema)
                report["schemaSummary"] = {}

                for root_key in ("queryType", "subscriptionType"):
                    root_name = (schema.get(root_key) or {}).get("name")
                    root = types.get(root_name) if root_name else None
                    if not root:
                        continue
                    summary = {}
                    for field in root.get("fields") or []:
                        base, _, _ = unwrap(field["type"])
                        summary[field["name"]] = {
                            "args": {a["name"]: gql_type_name(a["type"])
                                     for a in field.get("args") or []},
                            "returns": base,
                        }
                    report["schemaSummary"][root_key] = summary
                    print(f"\n  {root_key} ({len(summary)}):")
                    for name, info in summary.items():
                        sig = ", ".join(f"{k}: {v}" for k, v in info["args"].items())
                        print(f"    {name}({sig}) -> {info['returns']}")

                enums = {
                    name: [v["name"] for v in (t.get("enumValues") or [])]
                    for name, t in types.items()
                    if t.get("kind") == "ENUM" and not name.startswith("__")
                    and t.get("enumValues")
                }
                report["enums"] = enums
                if enums:
                    print("\n  enums:")
                    for name, values in enums.items():
                        print(f"    {name}: {', '.join(values)}")

                context = {
                    "deviceUUID": device_uuid,
                    "ioTGatewayUUID": (report.get("controller") or {}).get("ioTGatewayUUID"),
                    "satelliteId": (report.get("controller") or {}).get("id"),
                    "siteId": (report.get("controller") or {}).get("siteId"),
                    "companyId": (report.get("controller") or {}).get("companyId"),
                }

                query_root = types.get((schema.get("queryType") or {}).get("name"))
                print("\nInvoking read queries we can satisfy...")
                report["deepQueries"] = {}
                for field in (query_root or {}).get("fields") or []:
                    name = field["name"]
                    if name.startswith("__"):
                        continue
                    doc, variables, missing = build_call(field, types, context)
                    if missing:
                        report["deepQueries"][name] = {"skipped": missing}
                        print(f"  {name:35s} skipped (needs {', '.join(missing)})")
                        continue
                    res = graphql(session, args.appsync_url, token, doc, variables,
                                  auth_style=working_style)
                    report["deepQueries"][name] = {
                        "document": doc, "variables": variables, "result": res,
                    }
                    if ok(res):
                        payload = res["body"]["data"].get(name)
                        rendered = json.dumps(payload, default=str)
                        if len(rendered) > 160:
                            rendered = rendered[:157] + "..."
                        print(f"  {name:35s} {rendered}")
                    else:
                        body = res.get("body")
                        detail = ""
                        if isinstance(body, dict) and body.get("errors"):
                            first = body["errors"][0]
                            detail = (first.get("errorType")
                                      or first.get("message", ""))[:70]
                        print(f"  {name:35s} failed: {detail or res.get('status')}")
            print()

        # -- watch mode --------------------------------------------------------
        if args.watch:
            full = report.get("fullSchema")
            if not (full and ok(full)):
                full = graphql(session, args.appsync_url, token, FULL_INTROSPECTION,
                               auth_style=working_style)
                report["fullSchema"] = full
            if not ok(full):
                print("Cannot watch without the schema; introspection failed.")
                return 1
            schema = full["body"]["data"]["__schema"]
            types = index_types(schema)
            ctrl = report.get("controller") or {}
            watch_context = {
                "deviceUUID": device_uuid,
                "ioTGatewayUUID": ctrl.get("ioTGatewayUUID"),
                "satelliteId": ctrl.get("id"),
                "siteId": ctrl.get("siteId"),
                "companyId": ctrl.get("companyId"),
                "_queryRoot": (schema.get("queryType") or {}).get("name"),
            }
            watch(session, args.appsync_url, token, working_style, types,
                  watch_context, args.interval, args.duration, report)
            path = _write(report, do_redact)
            print(f"Full report written to {path}")
            return 0

        # -- probe candidate SK values ---------------------------------------
        sks = list(CANDIDATE_SKS) + [s for s in args.extra_sk if s not in CANDIDATE_SKS]
        print(f"Probing {len(sks)} candidate SK values...")
        report["deviceState"] = {}
        found = {}
        for sk in sks:
            res = graphql(session, args.appsync_url, token, GET_DEVICE_STATE,
                          {"PK": device_uuid, "SK": sk}, auth_style=working_style)
            report["deviceState"][sk] = res
            data = None
            if ok(res):
                node = res["body"]["data"].get("getDeviceStateTable")
                if node:
                    data = node.get("Data")
            if data is not None:
                found[sk] = data
                print(f"  {sk:30s} -> {data}")
            elif ok(res):
                print(f"  {sk:30s} -> null")
            else:
                errs = ""
                body = res.get("body")
                if isinstance(body, dict) and body.get("errors"):
                    errs = f" ({body['errors'][0].get('errorType') or body['errors'][0].get('message', '')[:60]})"
                print(f"  {sk:30s} -> HTTP {res.get('status')}{errs}")

        report["found"] = found
        print(f"\n{len(found)} of {len(sks)} candidate keys returned data.")

    path = _write(report, do_redact)
    print(f"Full report written to {path}")
    if do_redact:
        print("Coordinates, addresses and serials were redacted. "
              "Please still skim the file before sharing it.")
    return 0


def _write(report, do_redact=True):
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = f"appsync_diagnostic_{stamp}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(redact(report, do_redact), fh, indent=2, default=str)
    return path


if __name__ == "__main__":
    sys.exit(main())
