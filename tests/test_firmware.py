"""Tests for the firmware plugin: detection, merge placement, apply logic.

Run: python3 tests/test_firmware.py   (no pytest, no root)

Writes are stubbed, so no hardware is touched. Live *reads* are exercised but
never asserted on a particular value -- the point is that the accessors work
and degrade correctly, not what this machine's boot sound happens to be set
to today.

Requires hhd installed (it merges against hhd's real gamemode.yml, because
the whole risk being tested is how our containers land in someone else's
settings tree -- a merge against a hand-written stub would prove nothing).
"""

import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hhd.plugins.settings import merge_settings
from hhd.plugins.conf import Config

import hhd_z13
from hhd_z13 import firmware as fw

fail = 0


def ok(cond, msg, detail=""):
    global fail
    print(("  PASS " if cond else "  FAIL ") + msg + ("  " + detail if detail else ""))
    if not cond:
        fail += 1


# --- accessors -------------------------------------------------------------
ok(fw._supported("nonexistent_attr") is False, "unknown attribute reports unsupported")
ok(fw._read("nonexistent_attr") is None, "unknown attribute reads None")
ok(fw._read("boot_sound") in (True, False, None),
   f"boot_sound reads a tri-state ({fw._read('boot_sound')})")

# ATTRS is the plugin's ownership list. panel_overdrive is deliberately NOT in
# it: it became a tier of the Refresh Rate selector and is applied from
# display.py. If it ever reappears here, two writers own one attribute.
ok([a for _, a in fw.ATTRS] == ["boot_sound"],
   f"firmware.py owns boot_sound only ({[a for _, a in fw.ATTRS]})")

# --- placement in the real merge ------------------------------------------
gm_path = os.path.join(
    os.path.dirname(os.path.dirname(yaml.__file__)),
    "hhd", "plugins", "overlay", "gamemode.yml",
)
if not os.path.exists(gm_path):
    import hhd.plugins.overlay as _ov
    gm_path = os.path.join(os.path.dirname(_ov.__file__), "gamemode.yml")

gm = yaml.safe_load(open(gm_path))
base = {"gamemode": {"display": gm["display"], "gamescope": gm["gamescope"]}}
m = merge_settings([base] + [x.settings() for x in hhd_z13.autodetect([])])

disp = m["gamemode"]["display"]
ok(disp.get("title") == "Display", f"hhd's Display title preserved ({disp.get('title')!r})")
ok("touchscreen_disable" in disp["children"], "hhd's own toggles survive the merge")
ok("status" in disp["children"], "our refresh controls are there (no clobber)")
ok("panel_overdrive" not in disp["children"],
   f"overdrive is NOT a Display toggle -- it is a Refresh Rate tier ({sorted(disp['children'])})")

rr = disp["children"]["status"]["modes"]["available"]["children"]["refresh_rate"]
ok("overdrive" in rr["modes"]["manual"]["children"]["rate"]["options"],
   "the overdrive tier is an option of the Manual rate")
ok("overdrive" not in str(rr["modes"]["auto"].get("children", {})),
   "Auto exposes no overdrive control")

fwc = m["gamemode"].get("z13_firmware", {})
ok(fwc.get("title") == "Firmware", f"firmware container titled ({fwc.get('title')!r})")
ok("boot_sound" in fwc.get("children", {}),
   f"boot_sound inside it ({sorted(fwc.get('children', {}))})")
ok("boot_sound" not in disp["children"], "boot_sound NOT dumped into Display")

# Tag weight: ours must not sit below the upstream controls beside them, or
# they vanish from hhd's sidebar while upstream's stay. This is the regression
# guard for the propagation loop removed in cdab080.
ok("non-essential" not in disp["children"]["status"].get("tags", []),
   f"refresh controls are essential, like their neighbours "
   f"({disp['children']['status'].get('tags')})")

# --- apply logic, reads AND writes stubbed --------------------------------
# The read has to be stubbed too, not just the write. Without it the first
# tick adopts whatever the real hardware happens to hold, and the "user
# change" below is only a change when boot_sound happens to be ON: with it off
# the test sets False over an adopted False and correctly writes nothing, so
# the assertion fails for a reason that has nothing to do with the code.
#
# Caught exactly that way -- the suite passed all day, then failed once
# boot_sound was switched off in the QAM.
wrote = []
fw._write = lambda a, v: (wrote.append((a, v)), True)[1]
fw._read = lambda a: True          # live value is ON for these cases

conf = Config({"gamemode": {"z13_firmware": {"boot_sound": True}}})
p = fw.Z13FirmwarePlugin()
p.update(conf)
ok(not wrote, f"first tick adopts the live value and writes nothing ({wrote})")

conf["gamemode.z13_firmware.boot_sound"] = False
p.update(conf)
ok(wrote == [("boot_sound", False)], f"a user change is written once ({wrote})")

wrote.clear()
p.update(conf)
ok(not wrote, "an unchanged value is not rewritten")

print("\n  RESULT: " + ("ALL PASS" if not fail else f"{fail} FAILURE(S)"))
sys.exit(1 if fail else 0)
