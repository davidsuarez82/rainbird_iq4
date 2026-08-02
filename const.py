"""Constants for the Rain Bird IQ4 integration."""

DOMAIN = "rainbird_iq4"

# Configuration keys
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_SATELLITE_ID = "satellite_id"
CONF_COMPANY_ID = "company_id"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_SCAN_INTERVAL_CONFIG = "scan_interval_config"
CONF_SCAN_INTERVAL_PROGRAM = "scan_interval_program"

# Defaults
DEFAULT_SCAN_INTERVAL = 30          # seconds — real-time data
DEFAULT_SCAN_INTERVAL_CONFIG = 300  # seconds — satellite config (5 min)
DEFAULT_SCAN_INTERVAL_PROGRAM = 3600  # seconds — programs (1 hour)
DEFAULT_NAME = "Rain Bird IQ4"

# Rain Bird API
AUTH_BASE = "https://iq4server.rainbird.com/coreidentityserver"
API_BASE = "https://iq4server.rainbird.com/coreapi/api"

# --- Web portal channel (isIQ) — the original/default login method ---
# Implicit flow, same as the iq4.rainbird.com web portal. Token carries
# isApp: false / isIQ: true. On US free-tier accounts this channel is
# capped at "0 controllers", so zone control returns 403.
CLIENT_ID = "C5A6F324-3CD3-4B22-9F78-B4835BA55D25"
REDIRECT_URI = "https://iq4.rainbird.com/auth.html"

# --- Mobile app channel (isApp) — alternative login method ---
# Authorization Code + PKCE, same OAuth client the official Rain Bird 2.0
# app uses. Token carries isApp: true / isIQ: false. This channel is not
# subject to the web-portal subscription cap, so it can control zones on
# US free-tier accounts where the web channel returns 403.
APP_CLIENT_ID = "5B0FA4CD-8248-4BEB-B89A-F0AF8A254DB5"
APP_CLIENT_SECRET = "537C58B6-DCCF-4718-BFE6-CCD0D3FCDC07"
APP_REDIRECT_URI = "com.rainbird.mobile://auth"
APP_SCOPE = "coreAPI.read coreAPI.write openid profile offline_access"

# Auth channel selection
CONF_AUTH_CHANNEL = "auth_channel"
AUTH_CHANNEL_WEB = "web"
AUTH_CHANNEL_APP = "app"
DEFAULT_AUTH_CHANNEL = AUTH_CHANNEL_WEB


# Week days mapping (position 0=Sun, 1=Mon, 2=Tue, 3=Wed, 4=Thu, 5=Fri, 6=Sat)
WEEKDAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

# Station status
STATUS_IDLE = "-"
STATUS_RUNNING = "R"
STATUS_PAUSED = "P"

# Controller type ID to model name mapping
# Add new models here as they are reported by the community
CONTROLLER_MODELS: dict[int, str] = {
    57: "ESP-ME3",
    69: "ESP-TM2",
}


def get_controller_model(controller_type: int | None) -> str:
    """Return model name for a controller type ID."""
    if controller_type is None:
        return "Rain Bird IQ4"
    return CONTROLLER_MODELS.get(controller_type, f"Rain Bird Controller (type {controller_type})")
