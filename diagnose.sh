#!/bin/bash

set +e
set -uo pipefail

USERNAME="davidsuarez82@gmail.com"
PASSWORD="Ckz!9p2wi#q#GP"

# Secure sandbox directory for cookie validation states
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

# Static Rain Bird Authentication Gateways
CLIENT_ID="C5A6F324-3CD3-4B22-9F78-B4835BA55D25"
REDIRECT_URI="https%3A%2F%2Fiq4.rainbird.com%2Fauth.html"
AUTH_URL_BASE="https://iq4server.rainbird.com/coreidentityserver"
USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# Generate valid, uppercase hexadecimal state validations
STATE=$(openssl rand -hex 8 | tr 'a-f' 'A-F')
NONCE=$(openssl rand -hex 8 | tr 'a-f' 'A-F')

# Build the authorization signature callback parameters
RETURN_URL_ENCODED="/coreidentityserver/connect/authorize/callback?client_id=${CLIENT_ID}&redirect_uri=${REDIRECT_URI}&response_type=id_token%20token&scope=coreAPI.read%20coreAPI.write%20openid%20profile&state=${STATE}&nonce=${NONCE}"

# Encode characters natively into raw URL-compatible layout strings
URL_ENCODED_RETURN=$(echo -n "$RETURN_URL_ENCODED" | od -An -tx1 | tr ' ' % | tr -d '\n' | tr 'a-z' 'A-Z')
LOGIN_URL="$AUTH_URL_BASE/Account/Login?ReturnUrl=$URL_ENCODED_RETURN"


curl -s -c "$TMPDIR/cookies.txt" -A "$USER_AGENT" "$LOGIN_URL" -o "$TMPDIR/login.html"

# Extract cross-site scripting authorization verification token
TOKEN=$(grep -o 'name="__RequestVerificationToken"[^>]*value="[^"]*"' "$TMPDIR/login.html" | sed 's/.*value="\([^"]*\)".*/\1/' | head -n 1)

if [[ -z "$TOKEN" ]]; then
  echo "❌ Error en Paso 1: El servidor devolvió una página vacía o inesperada."
  echo "Inspeccionando los primeros 5 renglones devueltos por Rain Bird:"
  head -n 5 "$TMPDIR/login.html" || echo "[Archivo Vacío]"
  exit 1
fi

curl -s -b "$TMPDIR/cookies.txt" -c "$TMPDIR/cookies.txt" -A "$USER_AGENT" -L \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "Username=$USERNAME" \
  -d "Password=$PASSWORD" \
  --data-urlencode "ReturnUrl=$RETURN_URL_ENCODED" \
  --data-urlencode "__RequestVerificationToken=$TOKEN" \
  "$LOGIN_URL" -o "$TMPDIR/response.html" -D "$TMPDIR/headers.txt"

ACCESS_TOKEN=$(sed -n 's/.*access_token=\([^&"]*\).*/\1/p' "$TMPDIR/response.html" | head -n 1)
if [[ -z "$ACCESS_TOKEN" ]]; then
  ACCESS_TOKEN=$(sed -n 's/.*access_token=\([^&"]*\).*/\1/p' "$TMPDIR/headers.txt" | head -n 1)
fi

if [[ -z "$ACCESS_TOKEN" ]]; then
  echo "❌ Error en Paso 3: Autenticación rechazada."
  echo "Verifica que el usuario y contraseña coincidan con tu app Rain Bird 2.0."
  exit 1
fi

echo "$ACCESS_TOKEN"
