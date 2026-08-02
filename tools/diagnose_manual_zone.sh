#!/bin/bash
# Rain Bird IQ4 - Manual Zone Diagnostic Script
# Captures GetRunStationStatusForSatellite and EventLog behavior (including
# HTTP status codes, not just body) while a single zone is started manually.
# Usage: ./diagnose_manual_zone.sh your@email.com yourpassword
# Compatible with Linux and macOS
# Output: manual_zone_diagnostic_SATELLITEID.json

set +e
set -uo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <email> <password>"
  exit 1
fi

USERNAME="$1"
PASSWORD="$2"

echo "🌧️  Rain Bird IQ4 Manual Zone Diagnostic"
echo "=========================================="
echo ""

TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

CLIENT_ID="C5A6F324-3CD3-4B22-9F78-B4835BA55D25"
AUTH_URL_BASE="https://iq4server.rainbird.com/coreidentityserver"
API_BASE="https://iq4server.rainbird.com/coreapi/api"
USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

STATE=$(openssl rand -hex 8 | tr 'a-f' 'A-F')
NONCE=$(openssl rand -hex 8 | tr 'a-f' 'A-F')

RETURN_URL_RAW="/coreidentityserver/connect/authorize/callback?client_id=${CLIENT_ID}&redirect_uri=https%3A%2F%2Fiq4.rainbird.com%2Fauth.html&response_type=id_token%20token&scope=coreAPI.read%20coreAPI.write%20openid%20profile&state=${STATE}&nonce=${NONCE}"
URL_ENCODED_RETURN=$(python3 -c "import urllib.parse, sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$RETURN_URL_RAW")
LOGIN_URL="$AUTH_URL_BASE/Account/Login?ReturnUrl=$URL_ENCODED_RETURN"

echo "🔐 Step 1: Authenticating..."
curl -s -c "$TMPDIR/cookies.txt" -A "$USER_AGENT" "$LOGIN_URL" -o "$TMPDIR/login.html"

TOKEN=$(grep -o 'name="__RequestVerificationToken"[^>]*value="[^"]*"' "$TMPDIR/login.html" | sed 's/.*value="\([^"]*\)".*/\1/' | head -n 1)

if [[ -z "$TOKEN" ]]; then
  echo "❌ Failed to get login page. Check your internet connection."
  exit 1
fi

curl -s -b "$TMPDIR/cookies.txt" -c "$TMPDIR/cookies.txt" -A "$USER_AGENT" -L \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "Username=$USERNAME" \
  -d "Password=$PASSWORD" \
  --data-urlencode "ReturnUrl=$RETURN_URL_RAW" \
  --data-urlencode "__RequestVerificationToken=$TOKEN" \
  "$LOGIN_URL" -o "$TMPDIR/response.html" -D "$TMPDIR/headers.txt"

ACCESS_TOKEN=$(sed -n 's/.*access_token=\([^&"]*\).*/\1/p' "$TMPDIR/response.html" | head -n 1)
if [[ -z "$ACCESS_TOKEN" ]]; then
  ACCESS_TOKEN=$(sed -n 's/.*access_token=\([^&"]*\).*/\1/p' "$TMPDIR/headers.txt" | head -n 1)
fi

if [[ -z "$ACCESS_TOKEN" ]]; then
  echo "❌ Authentication failed. Check your username and password."
  exit 1
fi

echo "✅ Authenticated successfully"
echo ""

# api_get_with_status: performs a GET and captures both body and HTTP status.
# Writes body to $1 (a file path) and echoes the status code.
api_get_with_status() {
  local endpoint="$1"
  local params="${2:-}"
  local outfile="$3"
  local url="$API_BASE/$endpoint"
  if [[ -n "$params" ]]; then
    url="$url?$params"
  fi
  curl -s -o "$outfile" -w "%{http_code}" \
       -H "Authorization: Bearer $ACCESS_TOKEN" \
       -H "Accept: application/json" \
       "$url"
}

api_post_with_status() {
  local endpoint="$1"
  local body="$2"
  local params="${3:-}"
  local outfile="$4"
  local url="$API_BASE/$endpoint"
  if [[ -n "$params" ]]; then
    url="$url?$params"
  fi
  curl -s -o "$outfile" -w "%{http_code}" -X POST \
       -H "Authorization: Bearer $ACCESS_TOKEN" \
       -H "Accept: application/json" \
       -H "Content-Type: application/json" \
       -d "$body" \
       "$url"
}

echo "🔍 Step 2: Discovering controllers..."
SATELLITE_LIST=$(curl -s -H "Authorization: Bearer $ACCESS_TOKEN" -H "Accept: application/json" \
  "$API_BASE/Satellite/GetSatelliteList?includeInvisibleToCurrentUser=false")

if [[ -z "$SATELLITE_LIST" ]] || [[ "$SATELLITE_LIST" == "null" ]]; then
  echo "❌ No controllers found."
  exit 1
fi

echo "✅ Controllers found:"
python3 -c "
import sys, json
data = json.load(sys.stdin)
for i, s in enumerate(data):
    print(f\"  [{i}] {s.get('name','?')} (ID: {s.get('id','?')}, Type: {s.get('type','?')})\")
" <<< "$SATELLITE_LIST"
echo ""

if python3 -c "import sys,json; d=json.loads(sys.argv[1]); sys.exit(0 if len(d)>1 else 1)" "$SATELLITE_LIST" 2>/dev/null; then
  read -rp "Multiple controllers found. Enter the index of the one to test: " SAT_INDEX
else
  SAT_INDEX=0
fi

SATELLITE_ID=$(python3 -c "import sys,json; d=json.loads(sys.argv[1]); print(d[int(sys.argv[2])]['id'])" "$SATELLITE_LIST" "$SAT_INDEX")
echo "📡 Using Satellite ID: $SATELLITE_ID"
echo ""

echo "🔍 Step 3: One-time snapshot of related endpoints (for issue #10 context)..."
S_GETSATELLITE=$(api_get_with_status "Satellite/GetSatellite" "satelliteId=$SATELLITE_ID" "$TMPDIR/getsatellite.json")
echo "  GetSatellite: HTTP $S_GETSATELLITE"
S_GETPROGRAMS=$(api_get_with_status "Program/GetProgramList" "satelliteId=$SATELLITE_ID" "$TMPDIR/getprograms.json")
echo "  GetProgramList: HTTP $S_GETPROGRAMS"
echo ""

echo "🔍 Step 4: Station list..."
STATIONS=$(curl -s -H "Authorization: Bearer $ACCESS_TOKEN" -H "Accept: application/json" \
  "$API_BASE/Station/GetStationListForSatellite?satelliteId=$SATELLITE_ID")
python3 -c "
import sys, json
data = json.load(sys.stdin)
for s in data:
    print(f\"  - {s.get('name','?')} (station id: {s.get('id','?')}, terminal: {s.get('terminal','?')})\")
" <<< "$STATIONS"
echo ""

echo "=========================================================="
echo "  NOW: start a SINGLE zone manually (from the Rain Bird"
echo "  app or via Home Assistant), the way you normally would"
echo "  when reproducing the bug. Do it in the next 15 seconds."
echo "=========================================================="
sleep 15

echo ""
echo "🔄 Polling GetRunStationStatusForSatellite and EventLog every 5s for 2 minutes..."
echo "   (keep the zone running during this time if possible)"
echo ""

NOW_UTC=$(date -u +%Y-%m-%dT%H:%M:%S)
if date -v-1H +%Y-%m-%dT%H:%M:%S > /dev/null 2>&1; then
  EVT_START=$(date -u -v-1H +%Y-%m-%dT%H:%M:%S)
  EVT_END=$(date -u -v+10M +%Y-%m-%dT%H:%M:%S)
else
  EVT_START=$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S)
  EVT_END=$(date -u -d '10 minutes' +%Y-%m-%dT%H:%M:%S)
fi

POLL_LOG="[]"
for i in $(seq 1 24); do
  TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

  ST_RUN=$(api_get_with_status "ProgramStep/GetRunStationStatusForSatellite" "satelliteId=$SATELLITE_ID" "$TMPDIR/run_$i.json")
  ST_EVT=$(api_post_with_status "EventLog/GetEventLogsBySatelliteIds_V2" "[$SATELLITE_ID]" \
    "startTime=$EVT_START&endTime=$EVT_END&types=15&includeAcknowledgedAlarms=true&includeAcknowledgedWarnings=true" \
    "$TMPDIR/evt_$i.json")

  echo "  [$i/24] $TS  RunStationStatus=HTTP $ST_RUN  EventLog=HTTP $ST_EVT"

  POLL_LOG=$(python3 -c "
import json, sys

def safe_parse(path):
    try:
        with open(path) as f:
            content = f.read()
        if not content.strip():
            return None
        return json.loads(content)
    except Exception as e:
        return {'_parse_error': str(e)}

log = json.loads(sys.argv[1])
log.append({
    'poll': $i,
    'timestamp': '$TS',
    'run_station_status': {'http_status': '$ST_RUN', 'body': safe_parse('$TMPDIR/run_$i.json')},
    'event_log':          {'http_status': '$ST_EVT', 'body': safe_parse('$TMPDIR/evt_$i.json')},
})
print(json.dumps(log))
" "$POLL_LOG")

  sleep 5
done

OUTPUT_FILE="manual_zone_diagnostic_${SATELLITE_ID}.json"

python3 << PYEOF
import json

def safe_parse(path):
    try:
        with open(path) as f:
            content = f.read()
        if not content.strip():
            return None
        return json.loads(content)
    except Exception as e:
        return {"_parse_error": str(e)}

results = {
    "diagnostic_timestamp": "$NOW_UTC",
    "satellite_id": "$SATELLITE_ID",
    "snapshot": {
        "GetSatellite":    {"http_status": "$S_GETSATELLITE", "body": safe_parse("$TMPDIR/getsatellite.json")},
        "GetProgramList":  {"http_status": "$S_GETPROGRAMS",  "body": safe_parse("$TMPDIR/getprograms.json")},
    },
    "polling_during_manual_start": json.loads('''$POLL_LOG'''),
}

with open("$OUTPUT_FILE", "w") as f:
    json.dump(results, f, indent=2)

print("")
print("✅ Diagnostic complete!")
print(f"📄 Results saved to: $OUTPUT_FILE")
PYEOF

echo ""
echo "Please share the file '$OUTPUT_FILE' in the GitHub issue."
echo "⚠️  The file contains your satellite ID and station data but NOT your password or token."
