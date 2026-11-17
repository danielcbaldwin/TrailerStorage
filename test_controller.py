#!/usr/bin/env python3
"""Off-device simulation of TrailerStorage.Controller (run: python3 -I test_controller.py)."""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from TrailerStorage import Controller, HOLD_DOWN, HOLD_UP, FULL, LOW  # noqa: E402

CAP_AH = 824.0
TICK = 15
CFG = dict(Floor=50, Ceiling=60, DrainCurrent=3.0, ChargeCurrent=5.0, FullChargeDays=30,
           MinChargeTempC=2.0)


def load_a(t):          # DC load: 4-8 A, fridge-ish cycling
    return 6.0 + 2.0 * math.sin(t / 1800.0)


def solar_a(t):         # daytime up to 20 A
    h = (t / 3600.0) % 24
    return max(0.0, 20.0 * math.sin(math.pi * (h - 7) / 10)) if 7 <= h <= 17 else 0.0


def sim(days, soc0, since_full0, temp=15.0, cfg=CFG, force=False, die_at=None, shore=lambda t: True):
    ctl = Controller(dict(cfg))
    ctl.start(soc0)
    soc, since_full, applied = soc0, since_full0, 150.0
    t, out = 0.0, []
    sim.writes, last_w, last_t = 0, None, -1e9
    states = set()
    while t < days * 86400:
        load = load_a(t)
        # chargers deliver up to the limit applied last tick (+~1 A slop seen in the field)
        avail = (120.0 if shore(t) else 0.0) + solar_a(t)     # no shore: solar only
        charge = min(applied + 1.0, avail)
        if soc >= 100.0:
            charge = min(charge, load)            # full: chargers only cover loads
        batt_i = charge - load
        soc = max(0.0, min(100.0, soc + batt_i * TICK / 3600.0 / CAP_AH * 100.0))
        since_full = 0.0 if soc >= 100.0 else since_full + TICK
        if die_at is None or t < die_at:
            lim = ctl.step(soc, batt_i, temp, since_full, force, now=t, shore=shore(t))
            if lim != last_w and (last_w is None or abs(lim - last_w) >= 10 or t - last_t >= 60):
                applied, last_w, last_t = lim, lim, t
                sim.writes += 1
        states.add(ctl.state)
        out.append((t, soc, batt_i, applied, ctl.state))
        t += TICK
    return out, states, ctl


def summary(name, out, states, ctl, skip_h=0):
    socs = [s for (t, s, *_rest) in out if t >= skip_h * 3600]
    print(f"{name:42} SOC min {min(socs):5.1f} max {max(socs):5.1f} end {out[-1][1]:5.1f} "
          f"states={sorted(states)} full={ctl.full_result!r} writes/day={sim.writes / (out[-1][0] / 86400):.0f}")
    return min(socs), max(socs)


ok = True

# 1. normal storage, shunt synced recently: should stay ~50-60
o, s, c = sim(10, 52.0, 5 * 86400)
lo, hi = summary("1 band hold (10 d)", o, s, c)
ok &= lo >= 48 and hi <= 62 and FULL not in s

# 2. overdue full charge (35 d): goes to 100, syncs, then drifts back into band
o, s, c = sim(10, 52.0, 35 * 86400)
lo, hi = summary("2 overdue full charge", o, s, c)
ok &= hi >= 99.9 and c.full_result == "ok" and o[-1][1] <= 75

# 3. starts low (30 %): recovers at full rate, then holds
o, s, c = sim(5, 30.0, 5 * 86400)
lo, hi = summary("3 start below band", o, s, c, skip_h=24)
ok &= LOW in s and lo >= 48 and hi <= 62

# 4. cold pack: never targets net charge in hold states
o, s, c = sim(3, 55.0, 5 * 86400, temp=-5.0)
lo, hi = summary("4 cold (-5 C)", o, s, c)
ok &= FULL not in s

# 5. controller dies mid-run: how fast does the frozen limit drift?
o, s, c = sim(6, 60.5, 5 * 86400, die_at=3600)
drift_per_day = (o[-1][1] - o[int(3600 / TICK)][1]) / (6 - 1 / 24)
summary("5 service dies after 1 h (frozen limit)", o, s, c)
print(f"   drift after death: {drift_per_day:+.1f} %/day  (frozen limit {o[-1][3]} A)")
ok &= drift_per_day > -15

# 6. shunt never syncs: full charge must time out, not loop forever
ctl = Controller(dict(CFG))
ctl.start(55)
for i in range(int(30 * 3600 / TICK)):
    ctl.step(99.0, 1.0, 15.0, 40 * 86400, False, now=i * TICK)
print(f"{'6 full charge never syncs':42} state={ctl.state} result={ctl.full_result!r}")
ok &= ctl.full_result.startswith("timeout") and ctl.state != FULL
ctl.step(55.0, 0.0, 15.0, 40 * 86400, False, now=31 * 3600)
ok &= ctl.state != FULL          # cooldown: no immediate retry

# 7. (v1.2) 3-day shore outage while holding: stays engaged, holds on solar, no full charge started
out_7 = lambda t: not (6 * 3600 <= t < 3 * 86400 + 6 * 3600)
o, s, c = sim(5, 58.0, 5 * 86400, shore=out_7)
lo, hi = summary("7 3-day shore outage while holding", o, s, c)
# below the band with no shore the controller must not throttle solar at all (LOW = no limit)
throttled_low = [x for x in o if x[1] < 45 and x[4] != LOW]
unlimited_in_low = all(x[3] >= 150 for x in o if x[4] == LOW)
print(f"   below 45% without LOW state: {len(throttled_low)} ticks; LOW always unlimited: {unlimited_in_low}")
ok &= FULL not in s and hi <= 62 and not throttled_low and unlimited_in_low

# 8. (v1.2) full charge falls due during an outage: waits, then runs + syncs after shore returns
out_8 = lambda t: not (0 <= t < 2 * 86400)
o, s, c = sim(6, 55.0, 29.9 * 86400, shore=out_8)
first_full = next((t for (t, *_r, st) in o if st == FULL), None)
summary("8 full due during outage", o, s, c)
print(f"   full charge started at {first_full / 3600 if first_full is not None else None} h (shore back at 48 h)")
ok &= first_full is not None and first_full >= 2 * 86400 and c.full_result == "ok"

# 9. (v1.2) shore lost mid full charge for 2 days: no timeout, finishes after shore returns (or on solar)
out_9 = lambda t: not (2 * 3600 <= t < 2 * 86400 + 2 * 3600)
o, s, c = sim(6, 40.0, 40 * 86400, shore=out_9)
summary("9 shore lost during full charge", o, s, c)
ok &= c.full_result == "ok"

print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
