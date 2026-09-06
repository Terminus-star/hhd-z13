"""Regression tests for the panel-overdrive tier's adopt path.

Run: python3 tests/test_display_overdrive.py   (no pytest, no root, no hardware)

The firmware reader/writer is stubbed, so nothing here touches
/sys/class/firmware-attributes or the panel.

WHY THIS FILE EXISTS
    The overdrive tier shipped with a latching bug that no existing check
    could have caught. read_rate() reports Hz, so it returns "60" or "180"
    and can never return "overdrive" -- the tier is ours, not the
    compositor's. The adopt branch stored that raw reading, which dropped a
    stored "overdrive" selection to plain "180" while leaving the firmware
    flag ON. Nothing downstream noticed: the apply branch compares config
    against old_rate (now equal), and the reconcile compares against
    _rate_hz(), which maps overdrive to "180" as well. The UI read "180Hz"
    with the panel still overdriven and no path back to off.

    Every assertion below is a direct consequence of that trace.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hhd_z13 import display as d
from hhd_z13 import firmware as fw

fail = 0


def ok(cond, msg, detail=""):
    global fail
    print(("  PASS " if cond else "  FAIL ") + msg + ("  " + detail if detail else ""))
    if not cond:
        fail += 1


def stub(flag, supported=True):
    """Replace firmware's sysfs accessors, recording writes."""
    wrote = []
    state = {"v": flag}

    def _read(attr):
        return state["v"] if attr == "panel_overdrive" else None

    def _write(attr, on):
        wrote.append((attr, on))
        state["v"] = on
        return True

    fw._read = _read
    fw._write = _write
    fw._supported = lambda attr: supported
    return wrote


# --- the tier constants are what the rest of this file assumes -------------
ok(d.TOP_RATE == "180", f"TOP_RATE is the top Hz ({d.TOP_RATE!r})")
ok(d.BATTERY_RATE == "60", f"BATTERY_RATE is the low Hz ({d.BATTERY_RATE!r})")
ok(d.OVERDRIVE_RATE not in ("60", "180"),
   f"overdrive is a tier, not a rate ({d.OVERDRIVE_RATE!r})")
ok(d.OVERDRIVE_RATE not in d.AUTO_RATES,
   f"Auto can never reach overdrive ({d.AUTO_RATES})")

# --- _adopt_overdrive ------------------------------------------------------
w = stub(False)
ok(d._adopt_overdrive("180") == "180", "flag off at 180: adopts 180 unchanged")
ok(not w, f"flag off: writes nothing ({w})")

w = stub(True)
got = d._adopt_overdrive("180")
ok(got == d.OVERDRIVE_RATE,
   f"flag ON at 180: adopts the overdrive TIER, not plain 180 ({got!r})")
ok(not w, f"reconstructing the tier writes nothing ({w})")

# The pairing the tier exists to make unreachable. Overdrive's overshoot is
# tuned for a 5.6 ms frame; held across a 16.7 ms one it shows as inverse
# ghosting. There is no way to render "60 Hz + overdrive" in the selector, so
# adopting it would be invisible drift -- turn it off instead.
w = stub(True)
got = d._adopt_overdrive("60")
ok(got == "60", f"flag ON at 60: adopts 60 ({got!r})")
ok(w == [("panel_overdrive", False)],
   f"flag ON at 60: turns overdrive OFF rather than adopt it ({w})")

# --- never let an image-quality refinement break adopting the rate ---------
def _boom(attr):
    raise OSError("attribute vanished")


fw._read = _boom
ok(d._adopt_overdrive("180") == "180", "unreadable flag: falls back to the raw rate")

w = stub(True, supported=False)
ok(d._adopt_overdrive("60") == "60", "unsupported attribute: still adopts the rate")

# --- the round trip the bug broke -----------------------------------------
# Parked on the overdrive tier, hhd restarts (or the backend transitions, which
# also resets old_rate). Adopt must return the SAME selection the user left,
# or the apply branch and the reconcile both see agreement and the flag is
# stranded on with no path back.
w = stub(True)
adopted = d._adopt_overdrive(d._rate_hz(d.OVERDRIVE_RATE))
ok(adopted == d.OVERDRIVE_RATE,
   f"round trip: overdrive selection survives a restart ({adopted!r})")
ok(d._rate_hz(adopted) == d.TOP_RATE,
   "round trip: the tier still resolves to 180 Hz for the compositor")

print("\n  RESULT: " + ("ALL PASS" if not fail else f"{fail} FAILURE(S)"))
sys.exit(1 if fail else 0)
