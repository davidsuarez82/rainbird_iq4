"""Rain Bird IQ4 authentication using curl_cffi to bypass AWS WAF."""
from __future__ import annotations

import base64
import json
import logging
import re
import secrets
import time
from urllib.parse import quote

from curl_cffi import requests as cf

from .const import AUTH_BASE, CLIENT_ID

_LOGGER = logging.getLogger(__name__)

# Token cache file — survives HA restarts
TOKEN_CACHE_PATH = "/config/rainbird_iq4_token.json"


def _decode_jwt_exp(token: str) -> int:
    """Extract expiration timestamp from JWT payload."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return int(claims.get("exp", 0))
    except Exception:
        return 0


def fetch_token(username: str, password: str) -> str:
    """
    Authenticate with Rain Bird IQ4 and return a JWT access token.

    Uses curl_cffi to impersonate a real Chrome browser and bypass
    the AWS WAF challenge that blocks standard HTTP clients.
    """
    state = secrets.token_hex(8).upper()
    nonce = secrets.token_hex(8).upper()

    return_url_raw = (
        f"/coreidentityserver/connect/authorize/callback"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri=https%3A%2F%2Fiq4.rainbird.com%2Fauth.html"
        f"&response_type=id_token%20token"
        f"&scope=coreAPI.read%20coreAPI.write%20openid%20profile"
        f"&state={state}&nonce={nonce}"
    )

    return_url_encoded = quote(return_url_raw, safe="")
    login_url = f"{AUTH_BASE}/Account/Login?ReturnUrl={return_url_encoded}"

    session = cf.Session(impersonate="chrome")

    # Step 1: load login page and extract CSRF token
    r1 = session.get(login_url)
    if r1.status_code != 200:
        raise RuntimeError(f"Login page failed: HTTP {r1.status_code}")

    match = re.search(
        r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', r1.text
    )
    if not match:
        raise RuntimeError("CSRF token not found in login page")
    csrf = match.group(1)

    # Step 2: submit credentials
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

    # Step 3: extract access_token from redirect URL fragment
    url = r2.url
    if "access_token=" in url:
        fragment = url.split("#")[1] if "#" in url else url.split("?")[1]
        params = dict(p.split("=", 1) for p in fragment.split("&") if "=" in p)
        token = params.get("access_token")
        if token:
            _LOGGER.debug("Successfully obtained Rain Bird access token")
            return token

    raise RuntimeError(
        f"access_token not found in redirect. "
        f"HTTP {r2.status_code}, URL: {r2.url[:200]}"
    )


class RainBirdAuth:
    """
    Manages Rain Bird JWT token lifecycle.

    Caches the token in memory and on disk, and refreshes it automatically
    60 seconds before expiration. Disk cache survives HA restarts.
    All disk I/O happens inside get_token() which runs in an executor thread.
    """

    def __init__(self, username: str, password: str) -> None:
        self._username = username
        self._password = password
        self._token: str | None = None
        self._token_exp: int = 0
        self._cache_loaded = False

    def _load_token_cache(self) -> None:
        """Load token from disk cache if available and still valid.
        Must be called from an executor thread, not the event loop."""
        try:
            with open(TOKEN_CACHE_PATH) as f:
                data = json.load(f)
            token = data.get("token")
            exp = data.get("exp", 0)
            if token and time.time() < (exp - 60):
                self._token = token
                self._token_exp = exp
                _LOGGER.debug("Loaded valid token from disk cache")
        except Exception:
            pass
        finally:
            self._cache_loaded = True

    def _save_token_cache(self) -> None:
        """Save current token to disk cache.
        Must be called from an executor thread, not the event loop."""
        try:
            with open(TOKEN_CACHE_PATH, "w") as f:
                json.dump({"token": self._token, "exp": self._token_exp}, f)
            _LOGGER.debug("Token saved to disk cache")
        except Exception as e:
            _LOGGER.warning("Could not save token cache: %s", e)

    def _is_token_valid(self) -> bool:
        """Return True if the cached token is still valid."""
        return bool(self._token) and time.time() < (self._token_exp - 60)

    def get_token(self) -> str:
        """Return a valid token, refreshing if necessary.
        Runs in executor thread — disk I/O is safe here."""
        # Load disk cache on first call
        if not self._cache_loaded:
            self._load_token_cache()

        if not self._is_token_valid():
            _LOGGER.debug("Fetching new Rain Bird token")
            self._token = fetch_token(self._username, self._password)
            self._token_exp = _decode_jwt_exp(self._token)
            self._save_token_cache()

        return self._token

    def invalidate(self) -> None:
        """Force token refresh on next call."""
        self._token = None
        self._token_exp = 0

    def get_headers(self) -> dict:
        """Return authorization headers for API requests."""
        return {
            "Authorization": f"Bearer {self.get_token()}",
            "Accept": "application/json",
        }
