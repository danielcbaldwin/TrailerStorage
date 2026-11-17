# TrailerStorage

A small [SetupHelper](https://github.com/kwindrem/SetupHelper) package for a Victron GX (Cerbo) that holds a
LiFePO4 bank in a storage state-of-charge band (default 50–60 %) while the RV/trailer sits in storage (on shore power and/or solar),
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
* battery data is unavailable for 5 minutes,
* the package is uninstalled.

### Without shore power (v1.2)

Losing shore power does **not** end storage mode. After `ShoreLossSeconds` (default 120 s, so short blips are
ignored) it simply keeps holding the band with whatever solar is available: above the band it still trims the
(solar) charge current, below the band it lets all solar in. A due monthly full charge only **starts** with shore
power present; a full charge already running keeps charging on solar while its 24 h timeout is paused until shore
returns. The status text adds "(solar only, no shore power)". Storage mode can also be enabled while off shore.

**Turn storage mode off before using the trailer with a generator** — a generator on the AC input looks like shore
power and would be held to the storage band.

### Deep winter / BMS cut-off (v1.3)

When the batteries' BMS cuts off in the cold, the whole DC side (GX, SmartShunt) loses power. A SmartShunt that loses
power can wake up "synchronised" at 100 %, so after a service start or any gap in battery data a **high SOC reading is
not trusted** until it is confirmed: either the shunt kept its history (time since last full charge ≥ 6 h) or a real
sync happens (100 % with full-pack voltage, ≥ 13.35 V). Until then it never drains the bank — with shore power it
runs a full charge to resync, without it it just doesn't throttle charging. Two backstops apply at all times:

* `LowVoltage` (default 12.9 V): below it the pack is treated as low and charging is not throttled, whatever the SOC says.
* `MinChargeTempC` (default 2 °C): below it the target battery current is ≤ 0 A in every state (the chargers still
  power the loads, the cells are not charged).

Restore values are settings (`RestoreBol`, `RestoreMaxChargeCurrent`, `RestoreVebusChargeCurrent`) — set them to
your system's normal configuration before enabling.

## On the GX screen

Settings → **Trailer storage** (classic GUI, added just below DVCC): storage-mode on/off, floor, ceiling,
full-charge interval, *Full charge now*, live status, and an **Advanced** submenu (drift/charge rates,
cold limit, shore-loss delay, restore values) -- every setting is adjustable on the GX itself. The menu entry is a SetupHelper patch to
`PageSettings.qml`; the page itself is loaded at runtime so a problem in it cannot break the Settings menu.

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
