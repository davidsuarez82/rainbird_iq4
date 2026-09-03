"""Rain Bird IQ4 authentication using curl_cffi to bypass AWS WAF."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from urllib.parse import parse_qs, quote, urljoin, urlparse

from curl_cffi import requests as cf

from homeassistant.core import HomeAssistant

from .const import (
    APP_CLIENT_ID,
    APP_CLIENT_SECRET,
    APP_REDIRECT_URI,
    APP_SCOPE,
    AUTH_BASE,
    AUTH_CHANNEL_APP,
    AUTH_CHANNEL_WEB,
    CLIENT_ID,
    DEFAULT_AUTH_CHANNEL,
)

_LOGGER = logging.getLogger(__name__)

_MAX_REDIRECTS = 10

# AWS WAF serves its JavaScript challenge instead of the real page. It
# answers with 202 (challenge issued), and sometimes 405/429 under load.
# The challenge body always carries one of these markers, so a 200 that
# contains them is a challenge too.
_WAF_STATUS = frozenset({202, 405, 429})
_WAF_MARKERS = ("gokuProps", "challenge.js", "awswaf", "token.awswaf.com")

_WAF_MESSAGE = (
    "AWS WAF served a JavaScript challenge instead of the Rain Bird login "
    "page (HTTP {status}). This is not a credentials problem — the request "
    "never reached the login form. It is usually intermittent; wait a few "
    "minutes and try again."
)


class RainBirdWafChallenge(RuntimeError):
    """AWS WAF intercepted the request with a JavaScript challenge.

    Subclasses RuntimeError so existing callers that only catch
    RuntimeError keep working unchanged.
    """


class RainBirdAuthRejected(RuntimeError):
    """The identity server returned the login page instead of a token.

    Means the credentials were refused, or the account is in a state that
    needs attention in the web portal (forced password change, accepting
    new terms, temporary lockout).
    """


def _looks_like_waf(resp) -> bool:
    """Return True if the response is an AWS WAF challenge rather than content."""
    if resp.status_code in _WAF_STATUS:
        return True
    try:
        body = resp.text or ""
    except Exception:
        return False
    return any(marker in body for marker in _WAF_MARKERS)


def _extract_csrf(resp) -> str:
    """Validate a login-page response and return its CSRF token.

    Shared by both auth channels so the WAF is reported identically
    whichever one the user picked.
    """
    if _looks_like_waf(resp):
        raise RainBirdWafChallenge(_WAF_MESSAGE.format(status=resp.status_code))
    if resp.status_code != 200:
        raise RuntimeError(f"Login page failed: HTTP {resp.status_code}")

    match = re.search(
        r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', resp.text
    )
    if not match:
        raise RuntimeError("CSRF token not found in login page")
    return match.group(1)


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

    with cf.Session(impersonate="chrome") as session:
        # Step 1: load login page and extract CSRF token
        csrf = _extract_csrf(session.get(login_url))

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

        # No token. Work out why before reporting it, so the user is not
        # sent chasing their password when the WAF is what blocked them.
        if _looks_like_waf(r2):
            raise RainBirdWafChallenge(_WAF_MESSAGE.format(status=r2.status_code))

        if "/Account/Login" in url:
            raise RainBirdAuthRejected(
                "Login rejected (the server returned the login page instead of "
                "redirecting with a token). Check the username and password, and "
                "try logging in at https://iq4.rainbird.com — the account may need "
                "a password change or new terms accepted."
            )

        raise RuntimeError(
            f"access_token not found in redirect. "
            f"HTTP {r2.status_code}, URL: {r2.url[:200]}"
        )


def _make_pkce_pair() -> tuple[str, str]:
    """Generate a PKCE (code_verifier, code_challenge) pair using S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def fetch_token_isapp(username: str, password: str) -> str:
    """
    Authenticate the way the official Rain Bird 2.0 mobile app does and
    return a JWT access token.

    Uses the Authorization Code + PKCE flow with the mobile app's OAuth
    client. The resulting token carries isApp: true / isIQ: false, which
    (unlike the web-portal token) is not subject to the US free-tier
    "0 controllers" cap, so it can issue zone-control commands that the
    web channel rejects with 403.

    Uses curl_cffi to impersonate Chrome and bypass the AWS WAF challenge.
    """
    state = secrets.token_hex(8).upper()
    nonce = secrets.token_hex(8).upper()
    code_verifier, code_challenge = _make_pkce_pair()

    return_url_raw = (
        f"/coreidentityserver/connect/authorize/callback"
        f"?client_id={APP_CLIENT_ID}"
        f"&redirect_uri={quote(APP_REDIRECT_URI, safe='')}"
        f"&response_type=code"
        f"&code_challenge={code_challenge}"
        f"&code_challenge_method=S256"
        f"&scope={quote(APP_SCOPE, safe='')}"
        f"&state={state}&nonce={nonce}"
    )
    login_url = f"{AUTH_BASE}/Account/Login?ReturnUrl={quote(return_url_raw, safe='')}"

    with cf.Session(impersonate="chrome") as session:
        # Step 1: load login page and extract CSRF token
        csrf = _extract_csrf(session.get(login_url))

        # Step 2: submit credentials without auto-following redirects; we
        # need to intercept the authorization code, which lands on a
        # non-http redirect URI (com.rainbird.mobile://auth) the client
        # cannot follow itself.
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

        if not (resp.headers.get("location") or resp.headers.get("Location")):
            if _looks_like_waf(resp):
                raise RainBirdWafChallenge(
                    _WAF_MESSAGE.format(status=resp.status_code)
                )
            if resp.status_code == 200:
                raise RainBirdAuthRejected(
                    "Login rejected (the server returned the login page instead "
                    "of redirecting). Check the username and password, and try "
                    "logging in at https://iq4.rainbird.com — the account may "
                    "need a password change or new terms accepted."
                )

        # Step 3: walk the redirect chain until the authorization code appears
        code = None
        current_url = login_url
        for _ in range(_MAX_REDIRECTS):
            location = resp.headers.get("location") or resp.headers.get("Location")
            if not location:
                raise RuntimeError(
                    f"No redirect while logging in (HTTP {resp.status_code})."
                )
            absolute = urljoin(current_url, location)
            parsed = urlparse(absolute)
            found = parse_qs(parsed.query).get("code", [None])[0]
            if found:
                code = found
                break
            if parsed.scheme not in ("http", "https"):
                raise RuntimeError(
                    f"Reached final redirect but found no authorization code: {absolute}"
                )
            current_url = absolute
            resp = session.get(current_url, allow_redirects=False)

        if not code:
            raise RuntimeError("Too many redirects while logging in — no code found.")

        # Step 4: exchange the code for a token using the app client secret
        basic = base64.b64encode(
            f"{APP_CLIENT_ID}:{APP_CLIENT_SECRET}".encode()
        ).decode()
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
                f"Token exchange failed: HTTP {token_resp.status_code}, "
                f"body: {token_resp.text[:200]}"
            )

        token = token_resp.json().get("access_token")
        if not token:
            raise RuntimeError("Token exchange succeeded but no access_token returned.")

        _LOGGER.debug("Successfully obtained Rain Bird app-channel access token")
        return token


class RainBirdAuth:
    """
    Manages Rain Bird JWT token lifecycle.

    Caches the token in memory and on disk, and refreshes it automatically
    60 seconds before expiration. Disk cache survives HA restarts and
    integration updates (it lives in the HA config dir, keyed per account).
    All disk I/O happens inside get_token() which runs in an executor thread.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        username: str,
        password: str,
        channel: str = DEFAULT_AUTH_CHANNEL,
    ) -> None:
        self._username = username
        self._password = password
        self._channel = channel
        self._token: str | None = None
        self._token_exp: int = 0
        self._cache_loaded = False
        # Serializes token refreshes across the three coordinators, which
        # poll from different executor threads. Without this, an expired
        # token can trigger up to three simultaneous logins.
        self._refresh_lock = threading.Lock()
        # Per-account, per-channel cache file in the HA config dir (survives
        # HACS updates, never mixes tokens between different Rain Bird
        # accounts, and keeps web/app tokens separate so switching channels
        # never reuses a token from the other channel).
        key = f"{username.strip().lower()}|{channel}"
        account_hash = hashlib.sha256(key.encode()).hexdigest()[:12]
        self._cache_path = hass.config.path(
            ".storage", f"rainbird_iq4_token_{account_hash}.json"
        )

    def _load_token_cache(self) -> None:
        """Load token from disk cache if available and still valid.
        Must be called from an executor thread, not the event loop."""
        try:
            with open(self._cache_path) as f:
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
            with open(self._cache_path, "w") as f:
                json.dump({"token": self._token, "exp": self._token_exp}, f)
            os.chmod(self._cache_path, 0o600)
            _LOGGER.debug("Token saved to disk cache")
        except Exception as e:
            _LOGGER.warning("Could not save token cache: %s", e)

    def _is_token_valid(self) -> bool:
        """Return True if the cached token is still valid."""
        return bool(self._token) and time.time() < (self._token_exp - 60)

    def get_token(self) -> str:
        """Return a valid token, refreshing if necessary.
        Runs in executor thread — disk I/O is safe here."""
        if not self._cache_loaded:
            self._load_token_cache()

        if not self._is_token_valid():
            # Serialize refreshes: the three coordinators can all land here
            # at once with an expired token. Only the first thread through
            # the lock should actually log in; the rest just wait and reuse
            # the token it fetched.
            with self._refresh_lock:
                if not self._is_token_valid():
                    _LOGGER.debug(
                        "Fetching new Rain Bird token (channel=%s)", self._channel
                    )
                    if self._channel == AUTH_CHANNEL_APP:
                        self._token = fetch_token_isapp(self._username, self._password)
                    else:
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
