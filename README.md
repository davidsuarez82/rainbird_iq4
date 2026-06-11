# Rain Bird IQ4 Integration for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

A custom Home Assistant integration for the [Rain Bird IQ4](https://www.rainbird.com) cloud-based irrigation controller system.

## Features

- **Real-time zone status** — detects running zones from both manual and scheduled irrigation via event log
- **Last run tracking** — records when each zone last ran and when it completed
- **Manual zone control** — start/stop individual zones with configurable duration (1-30 min)
- **Irrigation calendar** — shows scheduled watering events in the Home Assistant calendar
- **Rain Delay control** — set and clear rain delay (0-14 days) via slider
- **Forecast Rain Delay** — enable/disable with configurable percent, rainfall and delay thresholds
- **Seasonal Adjust** — manual percentage adjustment per program (5-200%)
- **Weather Adjust Method** — switch between manual and automatic seasonal adjust per program
- **Controller status** — connection state, operating mode, alarms and warnings
- **Token caching** — JWT token persisted to disk, survives HA restarts without re-authentication

## Supported Devices

- Rain Bird ESP-TM2 (tested)
- Other Rain Bird IQ4-compatible controllers should work

## Prerequisites

- Rain Bird IQ4 account at [iq4.rainbird.com](https://iq4.rainbird.com)
- Home Assistant 2024.1.0 or newer

## Installation

### Via HACS (recommended)

1. Add this repository as a custom repository in HACS:
   - HACS → Integrations → ⋮ → Custom repositories
   - URL: `https://github.com/davidsuarez82/rainbird_iq4`
   - Category: Integration
2. Install **Rain Bird IQ4** from HACS
3. Restart Home Assistant

### Manual

1. Copy the `custom_components/rainbird_iq4` folder to your HA `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Rain Bird IQ4**
3. Enter your Rain Bird IQ4 username and password
4. The integration will automatically discover your controller

The scan interval (default 30 seconds) can be changed after setup via **Configure**.

## Entities

### Sensors
| Entity | Description |
|--------|-------------|
| Controller Mode | Operating mode: `auto` or `off` |
| Connected | Cloud connection status |
| Station 001-004 | Zone status: `idle`, `running` or `paused` |
| Program A/B/C Status | Program schedule status |
| Alarms | Unacknowledged alarm count |
| Warnings | Unacknowledged warning count |

### Controls
| Entity | Description |
|--------|-------------|
| Station 001-004 | Switch to manually start/stop a zone |
| Station 001-004 Duration | Run duration for manual activation (1-30 min) |
| Rain Delay | Set rain delay in days (0-14) |
| Forecast Rain Delay | Enable/disable forecast-based rain delay |
| Forecast Percent | Rain probability threshold (70/80/90%) |
| Forecast Rainfall | Rainfall threshold (0.125/0.25/0.50/0.75 inches) |
| Forecast Delay Days | Number of delay days (1/2) |
| Program A Weather Adjust | Adjust method: `none` or `automatic` |
| Program A Seasonal Adjust | Manual seasonal adjust percentage (5-200%) |

### Calendar
One calendar entity per scheduled program showing upcoming irrigation events.

## Authentication

This integration uses `curl_cffi` to impersonate a Chrome browser and bypass the AWS WAF that protects the Rain Bird authentication endpoint. The JWT token is cached on disk at `/config/rainbird_iq4_token.json` and refreshed automatically ~60 seconds before expiration (every ~2 hours).

## Technical Notes

- **Zone status detection**: Uses the Rain Bird event log (`EventLog/GetEventLogsBySatelliteIds_V2`) for reliable detection of both manual and scheduled irrigation. The real-time status endpoint (`GetRunStationStatusForSatellite`) only reflects manually triggered runs.
- **Cloud connection**: The Rain Bird ESP-TM2 uses MQTT and only connects to the cloud when the mobile app is active. Commands sent via the API are queued and executed when the controller connects.

## Known Limitations

- Rain Bird API does not provide real-time push notifications — all data is polled
- The AWS WAF may temporarily block authentication after multiple failed attempts; the integration handles this with exponential backoff
- Zone status during scheduled irrigation may show `idle` if the event log poll interval is longer than the irrigation window

## Contributing

Issues and pull requests welcome at [github.com/davidsuarez82/rainbird_iq4](https://github.com/davidsuarez82/rainbird_iq4/issues).

## License

MIT License
