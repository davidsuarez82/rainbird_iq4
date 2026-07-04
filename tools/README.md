# Community debugging scripts

These are standalone scripts used to diagnose specific issues reported by users. They are not part of the integration installed via HACS and are not required for normal use.

Use at your own discretion. They are not reviewed or endorsed by Rain Bird.

## Background

Some US-based accounts get a `403 Permission denied` from this integration when starting/stopping zones, even though the same account can control zones from the official Rain Bird 2.0 mobile app. This integration logs in the same way the `iq4.rainbird.com` web portal does (its token carries `isApp: false`), while the mobile app uses a different OAuth login channel (`isApp: true`).

The open question these scripts help answer: **does the mobile app's login channel (`isApp`) let a US account control zones where the web-portal channel (`isIQ`) returns a 403?** If so, switching the integration's login method could fix the 403 for US users without anyone needing a paid subscription.

> Note: one thing we already learned from this investigation is that the Rain Bird backend **silently ignores start durations under 60 seconds** - it returns HTTP 204 (success) but never actually runs the zone. Both scripts and the integration should always request at least 60 seconds. The test script below already does this.

## rainbird_isapp_test.py

Logs in using the mobile app's exact login flow (Authorization Code + PKCE, same OAuth client the app uses) and:
1. Prints the `isApp` claim (and a few other account fields) from the resulting token.
2. Makes one **read-only** API call (`GetSatelliteList`) to confirm the token works.

The main test is read-only and safe to stop there. There is also an **optional** zone test, off by default, that starts one zone through this same login channel to see whether it physically runs (and reports the HTTP status - e.g. a `403` would tell us the channel is still blocked). It requires typing `yes` and then a separate `CONFIRM`, always requests 60 seconds (the app's enforced minimum), lets you stop the zone early, and sends a stop command afterwards.

Your username and password are sent only to Rain Bird's own servers (`iq4server.rainbird.com`), exactly like the mobile app would. Nothing is sent anywhere else and nothing is saved to disk.

## rainbird_dump_stations.py

A read-only helper that logs in the same way and prints the raw station list for each controller (showing the internal `id` used for control vs. the `terminal` number). Useful for debugging station-identification issues. Never starts or stops anything.

## Who should run these

Mainly useful for **US-based accounts** that get a 403 from the integration on zone control. Not needed if the integration already works for you.

## How to run

```bash
pip install curl_cffi
python3 rainbird_isapp_test.py
```

If `pip install` fails with an "externally-managed-environment" error (common on recent Ubuntu/Debian), use a virtual environment:

```bash
python3 -m venv rainbird_test_env
source rainbird_test_env/bin/activate
pip install curl_cffi
python3 rainbird_isapp_test.py
```

Then paste the full output into the forum thread or a GitHub issue.
