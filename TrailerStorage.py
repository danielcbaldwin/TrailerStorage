#!/usr/bin/env python3
"""
TrailerStorage -- hold a LiFePO4 bank in a storage SOC band on a Victron GX.

Runs on the GX (Cerbo) as a SetupHelper-managed service. Does NOTHING until
/Settings/TrailerStorage/Enabled = 1. While enabled ("engaged") it turns DVCC on
and trims the DVCC max-charge-current so the battery drifts gently:

  HOLD_DOWN  SOC >= Ceiling   -> aim for a small net DISCHARGE (DrainCurrent A)
  HOLD_UP    SOC <= Floor     -> aim for a small net CHARGE    (ChargeCurrent A)
  FULL       every FullChargeDays (or ForceFullCharge=1) -> no limit until SOC 100
             and the battery monitor has synchronised, then back to HOLD_DOWN
  LOW        SOC < Floor - 5  -> no limit until back at Floor

Gentle drift (not a hard 0 A block) means that if this service ever dies, the
last limit stays in place and the battery only creeps slowly in either direction.

It disengages and restores the original settings (DVCC off, system max charge
current, AND the VE.Bus /Dc/0/MaxChargeCurrent which otherwise stays latched)
when: Enabled goes to 0, or the battery data goes stale.

v1.2: losing shore power does NOT end storage mode any more. With no shore power
(for ShoreLossSeconds, so short blips are ignored) it keeps holding the band on
solar alone; a due full charge only STARTS with shore power, and a full charge
already running keeps charging on whatever solar there is while its timeout clock
is paused until shore returns. Storage mode can also be enabled while off shore.
Turn storage mode off before using the trailer with a generator -- the generator
would otherwise be treated like shore power and held to the band.

v1.3 (deep winter: BMS low-temperature cut-off powers everything down):
* After a service start or a gap in battery data, a HIGH SOC reading is not trusted
  (a SmartShunt that lost power can wake up "synchronised" at 100 %). Until it is
  verified the controller never drains the bank: it is verified if the shunt kept its
  history (time since full charge >= 6 h), or by a real sync (100 % with the voltage to
  match); with shore power present it does a full charge to resync.
* Voltage backstop: below LowVoltage (default 12.9 V) it stops throttling charging.
* Cold: below MinChargeTempC the target battery current is <= 0 A in EVERY state
  (chargers keep powering the loads, the cells just aren't charged).
"""

import logging
import os
import sys
import time

LOG = logging.getLogger("TrailerStorage")

VERSION = "1.3"
TICK_S = 15

# ---------------------------------------------------------------- controller --
# Pure logic, no D-Bus, so it can be simulated off-device.

HOLD_DOWN, HOLD_UP, FULL, LOW, IDLE, FAULT = "hold_down", "hold_up", "full_charge", "low_recover", "idle", "fault"


class Controller:
    """Decides the DVCC charge-current limit from SOC and battery current."""

    GAIN = 0.5           # A of limit change per A of error, per tick
    DEADBAND = 1.5       # A; ignore small errors (fewer flash-backed setting writes)
    MAX_LIMIT = 150      # "no limit" value used while charging freely

    FULL_TIMEOUT_S = 24 * 3600
    FULL_RETRY_S = 3 * 86400      # after a full charge that didn't sync, wait before retrying

    def __init__(self, cfg):
        self.cfg = cfg
        self.state = IDLE
        self.limit = float(self.MAX_LIMIT)
        self.full_started = None
        self.full_ended = None
        self.full_result = ""
        self.full_noshore_s = 0.0     # time spent without shore power during the current full charge
        self._last_now = None
        self.verified = True          # False after a restart / battery-data gap until the SOC is confirmed

    SYNC_VOLTAGE = 13.35              # a real full LiFePO4 sits at/above this; a "fake" 100 % after power-up doesn't

    def mark_unverified(self, since_full_s):
        """Call after a service start or a battery-data gap. If the shunt kept its history it wasn't reset."""
        self.verified = since_full_s is not None and since_full_s >= 6 * 3600

    def start(self, soc):
        self.limit = float(self.MAX_LIMIT)
        self.state = HOLD_DOWN if soc >= self.cfg["Ceiling"] else HOLD_UP

    def step(self, soc, batt_i, temp_c, since_full_s, force_full, now=None, shore=True, volt=None):
        """shore: True if shore power is present (debounced by the caller); a full charge only starts with it."""
        now = time.time() if now is None else now
        dt = 0.0 if self._last_now is None else max(0.0, now - self._last_now)
        self._last_now = now
        c = self.cfg
        floor, ceil = c["Floor"], c["Ceiling"]
        cold = temp_c is not None and temp_c < c["MinChargeTempC"]
        low_v = volt is not None and volt < c.get("LowVoltage", 12.9)
        if not self.verified and since_full_s is not None and since_full_s < 3600 and soc >= 99.9 \
                and volt is not None and volt >= self.SYNC_VOLTAGE:
            self.verified = True            # a real sync (100 % and the voltage of a full pack)
        cooled_down = self.full_ended is None or now - self.full_ended > self.FULL_RETRY_S
        full_due = force_full or (c["FullChargeDays"] > 0 and cooled_down and since_full_s is not None
                                  and since_full_s > c["FullChargeDays"] * 86400)

        # --- state transitions
        if self.state == FULL:
            if not shore:
                self.full_noshore_s += dt        # timeout only counts time WITH shore power
            synced = soc >= 99.9 and (since_full_s is None or since_full_s < 3600) \
                and (volt is None or volt >= self.SYNC_VOLTAGE)      # v1.3: a fake power-up "100 %" isn't a sync
            if synced or cold or now - self.full_started - self.full_noshore_s > self.FULL_TIMEOUT_S:
                self.full_result = "ok" if synced else ("aborted: cold" if cold else "timeout: never synced")
                if synced:
                    self.verified = True
                self.full_ended = now
                self.full_started = None
                self.state = HOLD_DOWN
        elif full_due and not cold and shore:
            self.state = FULL
            self.full_started = now
            self.full_noshore_s = 0.0
            self.full_result = "running"
        elif soc < floor - 5:
            self.state = LOW
        elif self.state == LOW and soc >= floor:
            self.state = HOLD_UP
        elif soc >= ceil:
            self.state = HOLD_DOWN
        elif soc <= floor:
            self.state = HOLD_UP
        elif self.state not in (HOLD_DOWN, HOLD_UP):
            self.state = HOLD_UP
        # v1.3 guards
        if low_v and self.state != FULL:
            self.state = LOW                # voltage says the pack is low, whatever the SOC reading says
        if not self.verified and self.state == HOLD_DOWN:
            if shore and not cold:          # resync with a full charge instead of draining on a doubtful reading
                self.state = FULL
                self.full_started = now
                self.full_noshore_s = 0.0
                self.full_result = "running (resync after power loss)"
            else:
                self.state = HOLD_UP        # never drain on an unverified reading

        # --- output
        if self.state in (FULL, LOW) and not cold:
            self.limit = float(self.MAX_LIMIT)
            return int(self.limit)

        target = -c["DrainCurrent"] if self.state == HOLD_DOWN else c["ChargeCurrent"]
        if cold:
            target = min(target, 0.0)       # never push charge into a cold pack (any state): chargers still run the loads
        err = target - batt_i
        if abs(err) < self.DEADBAND:
            err = 0.0
        # leaving free-charge: start from something near the needed value, not 150
        if self.limit > 60 and err < 0:
            self.limit = max(0.0, min(self.limit, batt_i + err))
        self.limit = max(0.0, min(float(self.MAX_LIMIT), self.limit + self.GAIN * err))
        return int(round(self.limit))


# ------------------------------------------------------------------ service --

def main():
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    for p in ("/data/SetupHelper/velib_python", "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python"):
        if os.path.isdir(p):
            sys.path.insert(1, p)
            break
    from vedbus import VeDbusService
    from settingsdevice import SettingsDevice

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    SETTINGS = {
        "Enabled":          ["/Settings/TrailerStorage/Enabled", 0, 0, 1],
        "Floor":            ["/Settings/TrailerStorage/Floor", 50, 10, 95],
        "Ceiling":          ["/Settings/TrailerStorage/Ceiling", 60, 15, 100],
        "DrainCurrent":     ["/Settings/TrailerStorage/DrainCurrent", 3.0, 0.0, 20.0],
        "ChargeCurrent":    ["/Settings/TrailerStorage/ChargeCurrent", 5.0, 1.0, 50.0],
        "FullChargeDays":   ["/Settings/TrailerStorage/FullChargeDays", 30, 0, 365],
        "ForceFullCharge":  ["/Settings/TrailerStorage/ForceFullCharge", 0, 0, 1],
        "MinChargeTempC":   ["/Settings/TrailerStorage/MinChargeTempC", 2.0, -20.0, 20.0],
        "ShoreLossSeconds": ["/Settings/TrailerStorage/ShoreLossSeconds", 120, 15, 3600],
        "LowVoltage":       ["/Settings/TrailerStorage/LowVoltage", 12.9, 11.0, 13.4],
        "Engaged":          ["/Settings/TrailerStorage/Engaged", 0, 0, 1],
        # values written back on disengage (this system's normal configuration)
        "RestoreBol":            ["/Settings/TrailerStorage/RestoreBol", 0, 0, 1],
        "RestoreMaxChargeCurrent": ["/Settings/TrailerStorage/RestoreMaxChargeCurrent", 150, -1, 1000],
        "RestoreVebusChargeCurrent": ["/Settings/TrailerStorage/RestoreVebusChargeCurrent", 120, 1, 1000],
    }
    settings = SettingsDevice(bus, SETTINGS, lambda *a: None)

    def cfg():
        return {k: settings[k] for k in SETTINGS}

    # ---- own service for status / heartbeat
    name = "com.victronenergy.trailerstorage"
    try:
        svc = VeDbusService(name, bus=dbus.SystemBus(private=True), register=False)
    except TypeError:
        svc = VeDbusService(name, bus=dbus.SystemBus(private=True))
    svc.add_path("/Mgmt/ProcessName", "TrailerStorage")
    svc.add_path("/Mgmt/ProcessVersion", VERSION)
    svc.add_path("/Mgmt/Connection", "local")
    svc.add_path("/DeviceInstance", 0)
    svc.add_path("/ProductId", 0)
    svc.add_path("/ProductName", "Trailer Storage")
    svc.add_path("/FirmwareVersion", VERSION)
    svc.add_path("/HardwareVersion", 0)
    svc.add_path("/Connected", 1)
    svc.add_path("/State", IDLE)
    svc.add_path("/StatusText", "starting")
    svc.add_path("/ChargeLimit", None)
    svc.add_path("/Soc", None)
    svc.add_path("/Heartbeat", 0)
    svc.add_path("/LastExitReason", "")
    svc.add_path("/FullChargeResult", "")
    svc.add_path("/LimitWritesToday", 0)
    if hasattr(svc, "register"):
        try:
            svc.register()
        except Exception:
            pass

    # ---- D-Bus helpers
    def get(service, path):
        try:
            v = bus.get_object(service, path, introspect=False).GetValue(dbus_interface="com.victronenergy.BusItem")
            if isinstance(v, dbus.Array) and len(v) == 0:
                return None   # Venus "invalid" value
            return v
        except Exception:
            return None

    def put(service, path, value):
        try:
            bus.get_object(service, path, introspect=False).SetValue(value, dbus_interface="com.victronenergy.BusItem")
            return True
        except Exception as e:
            LOG.warning("SetValue %s%s=%s failed: %s", service, path, value, e)
            return False

    SYS = "com.victronenergy.system"
    SET = "com.victronenergy.settings"

    def vebus():
        return get(SYS, "/VebusService")

    def battery_service():
        # /Dc/Battery/BatteryService is a D-Bus name (com.victronenergy.battery.ttyS5);
        # /ActiveBatteryService is "com.victronenergy.battery/278" (service/instance), NOT queryable
        name = get(SYS, "/Dc/Battery/BatteryService")
        if name and "/" not in str(name):
            return str(name)
        return None

    ctl = Controller(cfg())
    st = {"shore_lost_at": None, "restore_checks": 0, "last_soc_ok": time.time(), "last_limit": None, "hb": 0,
          "last_write": 0.0, "writes_today": 0, "day": time.strftime("%j")}

    def set_status(state, text):
        svc["/State"] = state
        svc["/StatusText"] = text

    def engage(soc):
        LOG.info("engaging storage mode at SOC %.1f", soc)
        put(SET, "/Settings/Services/Bol", 1)
        settings["Engaged"] = 1
        ctl.cfg = cfg()
        ctl.start(soc)
        st["last_limit"] = None
        svc["/LastExitReason"] = ""

    def disengage(reason, clear_enabled=False):
        c = cfg()
        LOG.info("disengaging: %s", reason)
        put(SET, "/Settings/SystemSetup/MaxChargeCurrent", int(c["RestoreMaxChargeCurrent"]))
        put(SET, "/Settings/Services/Bol", int(c["RestoreBol"]))
        vb = vebus()
        if vb:
            put(vb, "/Dc/0/MaxChargeCurrent", float(c["RestoreVebusChargeCurrent"]))
        settings["Engaged"] = 0
        if clear_enabled:
            settings["Enabled"] = 0
        ctl.state = IDLE
        st["restore_checks"] = 4      # re-assert the VE.Bus limit for the next few ticks
        svc["/LastExitReason"] = reason
        svc["/ChargeLimit"] = None
        set_status(IDLE, "off (%s)" % reason)

    def reassert_restore():
        # DVCC can rewrite the VE.Bus limit once more after Bol=0; keep putting it back
        c = cfg()
        vb = vebus()
        if vb:
            cur = get(vb, "/Dc/0/MaxChargeCurrent")
            if cur is None or abs(float(cur) - float(c["RestoreVebusChargeCurrent"])) > 0.5:
                put(vb, "/Dc/0/MaxChargeCurrent", float(c["RestoreVebusChargeCurrent"]))
        if int(get(SET, "/Settings/Services/Bol") or 0) != int(c["RestoreBol"]):
            put(SET, "/Settings/Services/Bol", int(c["RestoreBol"]))
        st["restore_checks"] -= 1

    def tick():
        try:
            _tick()
        except Exception:
            LOG.exception("tick failed")
        st["hb"] = (st["hb"] + 1) % 100000
        svc["/Heartbeat"] = st["hb"]
        return True

    def _tick():
        c = cfg()
        ctl.cfg = c
        soc = get(SYS, "/Dc/Battery/Soc")
        batt_i = get(SYS, "/Dc/Battery/Current")
        volt = get(SYS, "/Dc/Battery/Voltage")
        temp = get(SYS, "/Dc/Battery/Temperature")
        svc["/Soc"] = None if soc is None else round(float(soc), 1)
        vb = vebus()
        shore = None
        if vb:
            conn = get(vb, "/Ac/ActiveIn/Connected")
            shore = None if conn is None else bool(int(conn))
        bs = battery_service()
        since_full = get(bs, "/History/TimeSinceLastFullCharge") if bs else None

        if st["restore_checks"] > 0:
            reassert_restore()

        if not int(c["Enabled"]):
            if int(c["Engaged"]):
                disengage("disabled")
            elif svc["/State"] != IDLE or svc["/StatusText"] == "starting":
                set_status(IDLE, "off")
            return

        # ---- enabled
        if soc is None or batt_i is None:
            if time.time() - st["last_soc_ok"] > 300:
                if int(c["Engaged"]):
                    disengage("battery data stale")
                st["data_gap"] = True
                set_status(FAULT, "battery data unavailable")
            return
        st["last_soc_ok"] = time.time()
        if st.get("data_gap", True):        # first data after a service start or a gap: re-check the SOC
            st["data_gap"] = False
            ctl.mark_unverified(None if since_full is None else float(since_full))
            LOG.info("battery data (re)appeared: SOC %s verified=%s", soc, ctl.verified)
        soc, batt_i = float(soc), float(batt_i)
        temp = None if temp is None else float(temp)

        # shore power only affects WHEN a full charge may start (v1.2: no disengage on shore loss)
        if shore is False:
            if st["shore_lost_at"] is None:
                st["shore_lost_at"] = time.time()
            shore_ok = time.time() - st["shore_lost_at"] < c["ShoreLossSeconds"]
        else:
            st["shore_lost_at"] = None
            shore_ok = True          # present, or unknown (no VE.Bus) -> behave as before

        if not int(c["Engaged"]):
            engage(soc)
        elif ctl.state == IDLE:          # service restarted while engaged
            put(SET, "/Settings/Services/Bol", 1)
            ctl.start(soc)

        limit = ctl.step(soc, batt_i, temp, None if since_full is None else float(since_full),
                         bool(int(c["ForceFullCharge"])), shore=shore_ok,
                         volt=None if volt is None else float(volt))
        if ctl.state == HOLD_DOWN and int(c["ForceFullCharge"]):
            settings["ForceFullCharge"] = 0          # forced full charge finished
        # MaxChargeCurrent is a flash-backed setting: write at most once a minute
        # unless the change is large (entering/leaving free charge)
        last = st["last_limit"]
        big = last is None or abs(limit - last) >= 10
        if limit != last and (big or time.time() - st["last_write"] >= 60):
            if put(SET, "/Settings/SystemSetup/MaxChargeCurrent", int(limit)):
                st["last_limit"] = limit
                st["last_write"] = time.time()
                st["writes_today"] += 1
        svc["/ChargeLimit"] = limit
        if time.strftime("%j") != st["day"]:
            st["day"], st["writes_today"] = time.strftime("%j"), 0
        svc["/LimitWritesToday"] = st["writes_today"]
        svc["/FullChargeResult"] = ctl.full_result
        texts = {HOLD_DOWN: "holding: drifting down to %d%%" % c["Floor"],
                 HOLD_UP: "holding: charging slowly to %d%%" % c["Ceiling"],
                 FULL: "monthly full charge (balancing / shunt sync)",
                 LOW: "below band: charging normally to %d%%" % c["Floor"]}
        text = texts.get(ctl.state, ctl.state)
        if temp is not None and temp < c["MinChargeTempC"]:
            text = "too cold to charge (%.0f C): charging paused" % temp
        elif volt is not None and float(volt) < c["LowVoltage"] and ctl.state == LOW:
            text = "battery voltage low (%.2f V): charging normally" % float(volt)
        elif not ctl.verified:
            text += " - SOC unverified since power-up (resyncs on next full charge)"
        if not shore_ok:
            text += " (solar only, no shore power)"
            if ctl.state != FULL and ctl.cfg["FullChargeDays"] > 0 and since_full is not None \
                    and float(since_full) > ctl.cfg["FullChargeDays"] * 86400:
                text += "; full charge waiting for shore power"
        set_status(ctl.state, text)

    # startup: if we left the system modified but are no longer enabled, clean up
    if int(settings["Engaged"]) and not int(settings["Enabled"]):
        disengage("startup cleanup")
    GLib.timeout_add_seconds(TICK_S, tick)
    LOG.info("TrailerStorage %s started", VERSION)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
