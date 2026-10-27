# Changelog

## [Unreleased]

### Added

- Added AirTouch 5 runtime sensors with daily and hourly history per air conditioner and zone, plus air conditioner mode breakdowns. Daily totals use the console's approximate 6-minute units.
- Added AirTouch 5 console numbers for sleep cool, sleep heat, away minimum, and away maximum.
- Added an AirTouch 5 console switch for energy wastage detection.
- Added AirTouch 5 weather-adaptation switches and differential and comfort numbers on the console.
- Added per-air-conditioner cool and heat limits and an auto-off duration for AirTouch 5.
- Added AirTouch 5 console numbers for the home alert minimum and maximum.
- Added AirTouch 5 relay output switches for Toggle outputs and buttons for Pulse outputs.
- Added an AirTouch 5 Programs sensor that shows the weekly programs saved on the console and their settings. Programs still run on the console and cannot be changed from Home Assistant.
- Added an AirTouch 5 Geofence sensor and a Post geofence presence action. The sensor shows the console's geofence air conditioners and accounts. The action sends one enter or exit event to the console's own geofence, which can create or update the account, and reports whether the console queued air conditioner commands.
- Added climate entities and on and off timers for Beam wireless air conditioners paired with an AirTouch 5. Beam temperatures are whole degrees Celsius, the current temperature is not shown, and each change is sent once without confirmation from the console.

### Fixed

- Fixed Sleep and Away temperature increments to use 1.8°F in Fahrenheit and 1°C in Celsius, including after display-unit changes.
