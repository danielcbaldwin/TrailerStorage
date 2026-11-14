# TrailerStorage

A small [SetupHelper](https://github.com/kwindrem/SetupHelper) package for a Victron GX (Cerbo) that holds a
LiFePO4 bank in a storage state-of-charge band (default 50–60 %) while the RV/trailer sits on shore power,
**without** ESS and without a BMS connected to the GX.

It runs on the GX, so it keeps working when the internet / VRM / Home Assistant is down.

## How it works

The battery SOC comes from the battery monitor (e.g. a SmartShunt). While enabled, the service turns DVCC on and
continuously trims the DVCC *max charge current* so the battery **drifts gently** instead of being hard-blocked:

| State | When | Aim |
|---|---|---|
| `hold_down` | SOC ≥ Ceiling | small net discharge (`DrainCurrent`, default 3 A) |
| `hold_up` | SOC ≤ Floor | small net charge (`ChargeCurrent`, default 5 A) |
| `full_charge` | every `FullChargeDays` (default 30) or `ForceFullCharge=1` | no limit until 100 % and the shunt syncs (cell balancing + SOC re-sync); 24 h timeout |
| `low_recover` | SOC < Floor − 5 | no limit until back at Floor |

Below `MinChargeTempC` (default 2 °C) it never targets a net charge in the hold states.

Because the limit is a gentle trim, if the service ever stops the frozen limit only moves the battery a few % per
day in either direction (simulated ~6 %/day), never a fast drain.

### Disengage / restore

It restores the system's normal settings — `Settings/Services/Bol` (DVCC), `Settings/SystemSetup/MaxChargeCurrent`
**and** the VE.Bus `/Dc/0/MaxChargeCurrent` (which otherwise stays latched at the last DVCC value) — when:

* `Enabled` is set to 0,
* shore power is lost for `ShoreLossSeconds` (default 120 s) — `Enabled` is cleared too, so a generator later sees a
  normal charger,
* battery data is unavailable for 5 minutes,
* the package is uninstalled.

Restore values are settings (`RestoreBol`, `RestoreMaxChargeCurrent`, `RestoreVebusChargeCurrent`) — set them to
your system's normal configuration before enabling.

## Settings (`com.victronenergy.settings`, `/Settings/TrailerStorage/...`)

`Enabled`, `Floor`, `Ceiling`, `DrainCurrent`, `ChargeCurrent`, `FullChargeDays`, `ForceFullCharge`,
`MinChargeTempC`, `ShoreLossSeconds`, `Engaged` (internal), `RestoreBol`, `RestoreMaxChargeCurrent`,
`RestoreVebusChargeCurrent`.

They are reachable over VRM MQTT, e.g. `W/<portal>/settings/0/Settings/TrailerStorage/Enabled {"value": 1}`.

## Status (`com.victronenergy.trailerstorage`)

`/State`, `/StatusText`, `/ChargeLimit`, `/Soc`, `/Heartbeat` (increments every 15 s), `/LastExitReason`,
`/FullChargeResult`, `/LimitWritesToday`.

## Install

Package Manager → Inactive packages → new: package `TrailerStorage`, GitHub user `danielcbaldwin`, branch `latest`.
The service does nothing until `Enabled = 1`.

## Test

`python3 test_controller.py` runs an off-device simulation of the controller.
