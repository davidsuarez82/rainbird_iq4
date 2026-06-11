"""Constants for the Rain Bird IQ4 integration."""

DOMAIN = "rainbird_iq4"

# Configuration keys
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_SATELLITE_ID = "satellite_id"
CONF_COMPANY_ID = "company_id"
CONF_SCAN_INTERVAL = "scan_interval"

# Defaults
DEFAULT_SCAN_INTERVAL = 30  # seconds
DEFAULT_NAME = "Rain Bird IQ4"

# Rain Bird API
AUTH_BASE = "https://iq4server.rainbird.com/coreidentityserver"
API_BASE = "https://iq4server.rainbird.com/coreapi/api"
CLIENT_ID = "C5A6F324-3CD3-4B22-9F78-B4835BA55D25"
REDIRECT_URI = "https://iq4.rainbird.com/auth.html"

# Week days mapping (position 0=Sun, 1=Mon, 2=Tue, 3=Wed, 4=Thu, 5=Fri, 6=Sat)
WEEKDAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

# Station status
STATUS_IDLE = "-"
STATUS_RUNNING = "R"
STATUS_PAUSED = "P"
