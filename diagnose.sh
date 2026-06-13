#!/bin/bash
# Rain Bird IQ4 - Diagnostic Script
# Usage: ./diagnose.sh your@email.com yourpassword
# Compatible with Linux and macOS
# Output: diagnostic_SATELLITEID.json

set +e
set -uo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <email> <password>"
  exit 1
fi

USERNAME="$1"
PASSWORD="$2"

echo "🌧️  Rain Bird IQ4 Diagnostic Script"
echo "===================================="
echo ""

# Temp directory for auth cookies
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

# Rain Bird constants
CLIENT_ID="C5A6F324-3CD3-4B22-9F78-B4835BA55D25"
AUTH_URL_BASE="https://iq4server.rainbird.com/coreidentityserver"
API_BASE="https://iq4server.rainbird.com/coreapi/api"
USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Generate state and nonce
STATE=$(openssl rand -hex 8 | tr 'a-f' 'A-F')
NONCE=$(openssl rand -hex 8 | tr 'a-f' 'A-F')

RETURN_URL_RAW="/coreidentityserver/connect/authorize/callback?client_id=${CLIENT_ID}&redirect_uri=https%3A%2F%2Fiq4.rainbird.com%2Fauth.html&response_type=id_token%20token&scope=coreAPI.read%20coreAPI.write%20openid%20profile&state=${STATE}&nonce=${NONCE}"

# URL encode using python3 (compatible with both Linux and macOS)
URL_ENCODED_RETURN=$(python3 -c "import urllib.parse, sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$RETURN_URL_RAW")

LOGIN_URL="$AUTH_URL_BASE/Account/Login?ReturnUrl=$URL_ENCODED_RETURN"

# Step 1: Get login page and CSRF token
echo "🔐 Step 1: Authenticating..."
curl -s -c "$TMPDIR/cookies.txt" -A "$USER_AGENT" "$LOGIN_URL" -o "$TMPDIR/login.html"

TOKEN=$(grep -o 'name="__RequestVerificationToken"[^>]*value="[^"]*"' "$TMPDIR/login.html" | sed 's/.*value="\([^"]*\)".*/\1/' | head -n 1)

if [[ -z "$TOKEN" ]]; then
  echo "❌ Failed to get login page. Check your internet connection."
  head -n 5 "$TMPDIR/login.html" 2>/dev/null || echo "[Empty response]"
  exit 1
fi

# Step 2: Submit credentials
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

# Helper functions
api_get() {
  local endpoint="$1"
  local params="${2:-}"
  local url="$API_BASE/$endpoint"
  if [[ -n "$params" ]]; then
    url="$url?$params"
  fi
  curl -s -H "Authorization: Bearer $ACCESS_TOKEN" \
       -H "Accept: application/json" \
       "$url"
}

api_post() {
  local endpoint="$1"
  local body="$2"
  local params="${3:-}"
  local url="$API_BASE/$endpoint"
  if [[ -n "$params" ]]; then
    url="$url?$params"
  fi
  curl -s -X POST \
       -H "Authorization: Bearer $ACCESS_TOKEN" \
       -H "Accept: application/json" \
       -H "Content-Type: application/json" \
       -d "$body" \
       "$url"
}

# Step 3: Get satellite list
echo "🔍 Step 2: Discovering controllers..."
SATELLITE_LIST=$(api_get "Satellite/GetSatelliteList" "includeInvisibleToCurrentUser=false")

if [[ -z "$SATELLITE_LIST" ]] || [[ "$SATELLITE_LIST" == "null" ]]; then
  echo "❌ No controllers found."
  exit 1
fi

echo "✅ Controllers found:"
python3 -c "
import sys, json
data = json.load(sys.stdin)
for s in data:
    print(f\"  - {s.get('name','?')} (ID: {s.get('id','?')}, Type: {s.get('type','?')}, companyId: {s.get('companyId','?')})\")
" <<< "$SATELLITE_LIST" 2>/dev/null || echo "$SATELLITE_LIST"
echo ""

SATELLITE_ID=$(python3 -c "import sys,json; d=json.loads(sys.argv[1]); print(d[0]['id'])" "$SATELLITE_LIST" 2>/dev/null)
COMPANY_ID=$(python3 -c "import sys,json; d=json.loads(sys.argv[1]); print(d[0]['companyId'])" "$SATELLITE_LIST" 2>/dev/null)

if [[ -z "$SATELLITE_ID" ]]; then
  echo "❌ Could not extract satellite ID."
  exit 1
fi

echo "📡 Using Satellite ID: $SATELLITE_ID"
echo "🏢 Company ID: $COMPANY_ID"
echo ""
echo "🔍 Step 3: Collecting diagnostic data..."
echo ""

OUTPUT_FILE="diagnostic_${SATELLITE_ID}.json"

NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
# macOS/Linux compatible date arithmetic
if date -v-24H +%Y-%m-%dT%H:%M:%S > /dev/null 2>&1; then
  # macOS
  START=$(date -u -v-24H +%Y-%m-%dT%H:%M:%S)
  END=$(date -u -v+2H +%Y-%m-%dT%H:%M:%S)
else
  # Linux
  START=$(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%S)
  END=$(date -u -d '2 hours' +%Y-%m-%dT%H:%M:%S)
fi

echo "  📋 GetSatelliteList..."
R_SATELLITE_LIST="$SATELLITE_LIST"

echo "  📋 GetSatellite..."
R_SATELLITE=$(api_get "Satellite/GetSatellite" "satelliteId=$SATELLITE_ID")

echo "  📋 isConnected..."
R_CONNECTED=$(api_get "Satellite/isConnected" "satelliteIds=$SATELLITE_ID")

echo "  📋 GetStationList..."
R_STATIONS=$(api_get "Station/GetStationListForSatellite" "satelliteId=$SATELLITE_ID")

echo "  📋 GetProgramList..."
R_PROGRAMS=$(api_get "Program/GetProgramList" "satelliteId=$SATELLITE_ID")

echo "  📋 GetRunStationStatus..."
R_RUNSTATUS=$(api_get "ProgramStep/GetRunStationStatusForSatellite" "satelliteId=$SATELLITE_ID")

echo "  📋 GetProgramsAssignedRuntime..."
R_ASSIGNED=$(api_get "ProgramStep/GetProgramsAssignedAndRunTimeBySatelliteId" "satelliteId=$SATELLITE_ID")

echo "  📋 GetSensorList..."
R_SENSORS=$(api_get "Sensor/GetSensorListBySatelliteId" "satelliteId=$SATELLITE_ID")

echo "  📋 GetFlowElements..."
R_FLOW=$(api_get "FlowElement/GetFlowElements" "parentId=&satelliteId=$SATELLITE_ID&includeHiddenFlowZones=False")

echo "  📋 GetFlowMonitoring..."
R_FLOWMON=$(api_get "FlowMonitoring/GetFlowMonitoringBySatelliteId" "satelliteId=$SATELLITE_ID")

echo "  📋 GetCompanyStatus..."
R_COMPANY=$(api_get "Company/GetCompanyStatusCore" "companyId=$COMPANY_ID")

echo "  📋 GetEventLogs..."
R_EVENTS=$(api_post "EventLog/GetEventLogsBySatelliteIds_V2" "[$SATELLITE_ID]" \
  "startTime=$START&endTime=$END&types=15&includeAcknowledgedAlarms=true&includeAcknowledgedWarnings=true")

# Build JSON output using python3
python3 << PYEOF
import json, sys

def safe_parse(s, label):
    if not s or s.strip() == '':
        return {"_error": "empty response"}
    try:
        return json.loads(s)
    except Exception as e:
        return {"_error": str(e), "_raw": s[:500]}

results = {
    "diagnostic_timestamp": "$NOW",
    "satellite_id": "$SATELLITE_ID",
    "company_id": "$COMPANY_ID",
    "endpoints": {
        "GetSatelliteList":                    safe_parse("""$R_SATELLITE_LIST""", "GetSatelliteList"),
        "GetSatellite":                        safe_parse("""$R_SATELLITE""", "GetSatellite"),
        "isConnected":                         safe_parse("""$R_CONNECTED""", "isConnected"),
        "GetStationListForSatellite":          safe_parse("""$R_STATIONS""", "GetStationList"),
        "GetProgramList":                      safe_parse("""$R_PROGRAMS""", "GetProgramList"),
        "GetRunStationStatusForSatellite":     safe_parse("""$R_RUNSTATUS""", "GetRunStationStatus"),
        "GetProgramsAssignedRuntime":          safe_parse("""$R_ASSIGNED""", "GetProgramsAssigned"),
        "GetSensorListBySatelliteId":          safe_parse("""$R_SENSORS""", "GetSensorList"),
        "GetFlowElements":                     safe_parse("""$R_FLOW""", "GetFlowElements"),
        "GetFlowMonitoringBySatelliteId":      safe_parse("""$R_FLOWMON""", "GetFlowMonitoring"),
        "GetCompanyStatusCore":                safe_parse("""$R_COMPANY""", "GetCompanyStatus"),
        "GetEventLogsBySatelliteIds_V2":       safe_parse("""$R_EVENTS""", "GetEventLogs"),
    }
}

with open("$OUTPUT_FILE", "w") as f:
    json.dump(results, f, indent=2)

print("✅ Diagnostic complete!")
print("")
print(f"📄 Results saved to: $OUTPUT_FILE")
print("")
print("Endpoint summary:")
for name, data in results["endpoints"].items():
    if isinstance(data, dict) and "_error" in data:
        print(f"  ❌ {name}: {data['_error']}")
    elif isinstance(data, list):
        print(f"  ✅ {name}: {len(data)} items")
    elif isinstance(data, dict):
        print(f"  ✅ {name}: ok")
    else:
        print(f"  ✅ {name}: ok")
PYEOF

echo ""
echo "Please share the file '$OUTPUT_FILE' in the GitHub issue."
echo "⚠️  The file contains your satellite ID and company ID but NOT your password or token."
