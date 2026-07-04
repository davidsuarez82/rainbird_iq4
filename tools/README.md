# Community debugging scripts

These are standalone scripts used to diagnose specific issues reported
by users. They are **not** part of the integration installed via HACS
and are not required for normal use.

Use at your own discretion. They are not reviewed or endorsed by
Rain Bird.

## rainbird_isapp_test.py

Checks whether your Rain Bird account receives an `isApp: true` claim
when authenticating the same way the official mobile app does. Used to
investigate 403 errors on `ManualOps/StartStations` for some accounts.

Read-only: does not start or stop any irrigation zone.
