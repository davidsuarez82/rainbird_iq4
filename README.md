# Rain Bird IQ4 Integration for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

A Home Assistant custom integration for **Rain Bird IQ4** cloud-connected irrigation controllers (ESP-TM2 and compatible models).

> **Note:** This integration uses the unofficial Rain Bird IQ4 cloud API. It is not affiliated with or endorsed by Rain Bird Corporation. The API may change without notice.

---

## Requirements

- Home Assistant 2024.1 or newer
- Rain Bird IQ4 account (https://iq4.rainbird.com)
- A Rain Bird controller connected to the IQ4 cloud (e.g. ESP-TM2)

---

## Features

- **Real-time station monitoring** — zone status (idle / running / paused), last run time, last completed run
- **Program monitoring** — schedule status, weather adjust method, seasonal adjust percentage
- **Rain delay sensor** — days remaining
- **Forecast rain delay** — enabled/disabled with threshold attributes
- **Controller status** — connected to cloud, operating mode, alarms and warnings
- **7 actions** to control irrigation, rain delay, and seasonal adjust settings
- **Calendar** — upcoming irrigation events per program
- **Configurable polling intervals** — separate intervals for real-time, config, and program data
- **Manual refresh button** — force immediate update of all data
- **Fault tolerance** — tolerates up to 3 consecutive API errors before marking entities unavailable

---

## Installation

### Via HACS (recommended)

1. Open HACS in Home Assistant
2. Go to **Integrations** → click the three dots menu → **Custom repositories**
3. Add `https://github.com/davidsuarez82/rainbird_iq4` as an **Integration**
4. Search for **Rain Bird IQ4** and install
5. Restart Home Assistant

### Manual

1. Download the latest release
2. Copy the `custom_components/rainbird_iq4` folder to your HA `config/custom_components/` directory
3. Restart Home Assistant

---

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Rain Bird IQ4**
3. Enter your Rain Bird IQ4 username and password

The integration will automatically discover your controller and satellite ID.

### Options

After setup, click **Configure** on the integration to adjust polling intervals:

| Setting | Default | Range | Description |
|---|---|---|---|
| Real-time interval | 30s | 10–300s | Station status, alarms |
| Config interval | 5 min | 60–3600s | Rain delay, forecast, controller mode |
| Program interval | 1 hour | 5 min–24h | Programs, schedules, seasonal adjust |

---

## Entities

### Sensors
| Entity | Description |
|---|---|
| Station 001–N | Zone status: `idle` / `running` / `paused` |
| Program A–N Status | Program status: `scheduled` / `not scheduled` / `disabled` |
| Rain Delay | Days remaining (0 = no delay) |
| Controller Mode | `auto` / `off` |
| Alarms | Unacknowledged alarm count |
| Warnings | Unacknowledged warning count |

**Station attributes:** terminal, last_run, last_run_completed, remaining time

**Program attributes:** start_time, week_days, steps, weather_adjust (none/automatic), seasonal_adjust (%)

### Binary Sensors
| Entity | Description |
|---|---|
| Connected | Controller connected to IQ4 cloud |
| Forecast Rain Delay | Forecast-based rain delay enabled |

**Forecast attributes (when enabled):** percent threshold, rainfall threshold (inches), delay_days

### Calendar
One calendar entity per scheduled program showing upcoming irrigation events.

### Button
- **Refresh** — triggers immediate update of all three coordinators

---

## Actions

| Action | Description |
|---|---|
| `rainbird_iq4.start_zone` | Start a zone manually (1–30 min) |
| `rainbird_iq4.stop_zone` | Stop a running zone |
| `rainbird_iq4.set_rain_delay` | Set rain delay in days (0 = clear) |
| `rainbird_iq4.enable_forecast_rain_delay` | Enable forecast-based rain delay |
| `rainbird_iq4.disable_forecast_rain_delay` | Disable forecast-based rain delay |
| `rainbird_iq4.set_weather_adjust_automatic` | Enable automatic seasonal adjust for a program |
| `rainbird_iq4.set_weather_adjust_manual` | Set manual seasonal adjust % for a program |

All zone and program actions use entity selectors filtered to Rain Bird IQ4 entities.

---

## Known Limitations

- **Unofficial API** — Rain Bird does not provide a public API. This integration reverse-engineers the IQ4 web app traffic. It may break if Rain Bird changes their backend.
- **AWS WAF** — The Rain Bird cloud is protected by AWS WAF, which blocks standard HTTP clients. This integration uses `curl_cffi` to impersonate a browser. Excessive login attempts may trigger a temporary ban.
- **Cloud-dependent** — The integration requires an active internet connection and Rain Bird IQ4 cloud service. Local control is not supported.
- **Token-based auth** — Authentication tokens are cached on disk and refreshed automatically every ~2 hours. A restart may briefly show entities as unavailable while the token is refreshed.
- **Timestamps in local time** — Rain Bird event log timestamps are in controller local time, not UTC.

---

## Tested Hardware

- Rain Bird ESP-TM2 (firmware 2.29.0)

Other IQ4-compatible controllers may work but have not been tested. Please open an issue if you have a different model.

---

## Contributing

Contributions are welcome. Please open an issue before submitting a pull request to discuss the proposed change.

If Rain Bird changes their API and the integration breaks, issues with debug logs are especially helpful.

---

## License

MIT
