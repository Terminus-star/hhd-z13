"""Tests for the TDP preset plugin.

Run: python3 tests/test_tdp_presets.py   (no root, no writes outside a tmpdir)

The store is redirected into a tmpdir and the sysfs reader is stubbed, so
nothing on the machine is touched.
"""

import glob
import json
import os
import sys
import tempfile

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hhd.plugins.settings import merge_settings
from hhd.plugins.conf import Config

from hhd_z13 import tdp as t

# The real path must NOT be under /etc/hhd: hhd registers F_NOTIFY with
# DN_MODIFY there, so every write triggered a full "Reloading configuration"
# -- 65 in 90 minutes -- each rebuilding the tree and cycling the overlay,
# which is what kept restarting Steam.
REAL_STORE = t.STORE_PATH
TMP = tempfile.mkdtemp(prefix="z13tdp-")
t.STORE_PATH = os.path.join(TMP, "presets.json")

fail = 0


def ok(cond, msg, detail=""):
    global fail
    print(("  PASS " if cond else "  FAIL ") + msg + ("  " + detail if detail else ""))
    if not cond:
        fail += 1


def store(presets=None):
    try:
        os.unlink(t.STORE_PATH)
    except FileNotFoundError:
        pass
    if presets is not None:
        with open(t.STORE_PATH, "w") as f:
            json.dump({"presets": presets, "applied": {"sppt": 0, "fppt": 0}}, f)


def presets_on_disk():
    return json.load(open(t.STORE_PATH))["presets"]


def plugin(ready=True, boost=True, patched=True):
    p = t.Z13TdpPlugin()
    p._tdp_ready = ready
    p._gen_boost = boost
    p._patched = patched
    p.events = []
    p.emit = p.events.append
    return p


def mkconf(mode="custom", power="disabled", ac="z13_p1", dc="z13_p2",
           tdp=45, boost=True, sppt=0, fppt=0, presets=1):
    modes = {f"z13_p{i}": {"remove": False} for i in range(1, presets + 1)}
    return Config({
        "tdp": {"asus": {
            "tdp_v2": {
                "mode": mode,
                "custom": {
                    "tdp": tdp, "boost": boost,          # adjustor's own
                    "z13_sppt": sppt, "z13_fppt": fppt,  # ours
                    "z13_add": False, "z13_status": "",
                },
                **modes,
            },
            "z13_applied": "",
            "z13_power": {"mode": power,
                          "enabled": {"ac_preset": ac, "dc_preset": dc}},
        }},
        "hhd": {"settings": {"tdp_ready": True}},
    })


ok(not REAL_STORE.startswith("/etc/hhd"),
   f"the store is not in a directory hhd watches ({REAL_STORE})")


# ==========================================================================
# 1. The generated tree
# ==========================================================================
store([{"tdp": 20, "boost": True, "sppt": 25, "fppt": 29},
       {"tdp": 9, "boost": False, "sppt": 9, "fppt": 9}])
s = plugin().settings()
asus = s["tdp"]["asus"]["children"]
tv = asus["tdp_v2"]["modes"]
kids = tv["custom"]["children"]

ok(sorted(tv) == ["custom", "z13_p1", "z13_p2"],
   f"saved presets are modes beside Custom ({sorted(tv)})")
ok(tv["z13_p1"]["title"] == "Preset 1 · 20 W",
   f"the title carries the wattage ({tv['z13_p1']['title']!r})")
ok("no boost" in tv["z13_p2"]["title"],
   f"...and says when boost is off ({tv['z13_p2']['title']!r})")

# A saved preset is a finished configuration: no sliders, only a way out.
ok(sorted(tv["z13_p1"]["children"]) == ["remove"],
   f"a preset carries no editable controls ({sorted(tv['z13_p1']['children'])})")

# PL2/PL3 sit directly under TDP Boost, not behind an auto/manual dropdown.
ok("z13_sppt" in kids and "z13_fppt" in kids,
   f"PL2/PL3 are direct children of Custom ({sorted(kids)})")
ok(all(kids[k]["type"] == "int" for k in ("z13_sppt", "z13_fppt")),
   "and are plain sliders, not a mode")
ok("z13_ppt" not in kids, "the auto/manual dropdown is gone")

# Boost is what makes them differ from PL1 at all.
off = plugin(boost=False).settings()["tdp"]["asus"]["children"]["tdp_v2"]["modes"]["custom"]["children"]
ok("z13_sppt" not in off and "z13_fppt" not in off,
   f"PL2/PL3 GONE while boost is off ({sorted(off)})")
# Telemetry is a SIBLING of the TDP Mode selector, not a child of Custom: the
# reading matters most while a preset or a stock profile is active, which is
# exactly when Custom's sliders are not on screen to read it off.
ok("z13_applied" not in off and "z13_applied" not in kids,
   "telemetry is not inside Custom")
ok(asus["z13_applied"]["type"] == "display",
   "Applied is read-only telemetry, beside the TDP Mode selector")
ok("z13_power" in asus and "z13_power" not in kids,
   "the power-change policy is its own area")
opts = asus["z13_power"]["modes"]["enabled"]["children"]["ac_preset"]["options"]
ok(sorted(opts) == ["balanced", "performance", "quiet", "z13_p1", "z13_p2"],
   f"policy targets are saved presets + stock profiles ({sorted(opts)})")

store([])
ok(sorted(plugin().settings()["tdp"]["asus"]["children"]["tdp_v2"]["modes"]) == ["custom"],
   "no saved presets means no extra entries")

# --- merge against adjustor's real tree -----------------------------------
hits = sorted(glob.glob("/usr/lib/python3*/site-packages/adjustor/drivers/asus/settings.yml"))
if hits:
    base = {"tdp": {"asus": yaml.safe_load(open(hits[-1]))}}
    store([{"tdp": 20, "boost": True, "sppt": 25, "fppt": 29}])
    m = merge_settings([base, plugin().settings()])
    a = m["tdp"]["asus"]
    mm = a["children"]["tdp_v2"]["modes"]
    ok(sorted(mm) == ["balanced", "custom", "performance", "quiet", "z13_p1"],
       f"presets join adjustor's own four ({sorted(mm)})")
    cu = mm["custom"]["children"]
    raw = yaml.safe_load(open(hits[-1]))["children"]["tdp_v2"]["modes"]["custom"]["children"]
    ok(cu["tdp"].get("min") == raw["tdp"]["min"]
       and cu["tdp"].get("tags") == raw["tdp"].get("tags", []),
       "adjustor's TDP slider is untouched")
    ok(len([k for k in cu if k in ("tdp", "boost")]) == 2
       and not [k for k in cu if k.startswith("z13_") and k.endswith("tdp")],
       "still exactly one TDP slider and one boost toggle")
    ok("fan" in a["children"], "adjustor's fan curve survives")
else:
    print("  SKIP  adjustor not installed (merge checks)")


# ==========================================================================
# 2. Custom is the scratchpad; PL2/PL3 track PL1
# ==========================================================================
store([])
p = plugin()
c = mkconf(mode="custom", tdp=45, presets=0)
p.prepare(c)
ok(c[t.CUSTOM_TDP].to(int) == 45, "first tick leaves the sustained value alone")
# The boost limits are derived from it, so a stored pair from another wattage
# would show limits unrelated to the slider above them.
ok((c[t.SPPT].to(int), c[t.FPPT].to(int)) == (56, 64),
   f"PL2/PL3 are brought into line on the first tick ({c[t.SPPT].to(int)}, {c[t.FPPT].to(int)})")

c[t.CUSTOM_TDP] = 20
p.prepare(c)
ok((c[t.SPPT].to(int), c[t.FPPT].to(int)) == (25, 29),
   f"moving PL1 recomputes them ({c[t.SPPT].to(int)}, {c[t.FPPT].to(int)})")

# Nudging one on its own sticks -- that is the whole point of them being
# editable rather than derived-only.
c[t.SPPT] = 40
p.prepare(c)
ok(c[t.SPPT].to(int) == 40, f"a nudged PL2 sticks ({c[t.SPPT].to(int)})")
ok(c[t.FPPT].to(int) == 40, f"and PL3 is raised to keep the order ({c[t.FPPT].to(int)})")

# ...until PL1 moves again, so they cannot drift far from the sustained value.
c[t.CUSTOM_TDP] = 30
p.prepare(c)
ok((c[t.SPPT].to(int), c[t.FPPT].to(int)) == (38, 43),
   f"moving PL1 pulls them back in line ({c[t.SPPT].to(int)}, {c[t.FPPT].to(int)})")

# The firmware rejects a lower tier that exceeds a higher one, so order is
# enforced rather than left to produce a silent no-op.
c[t.SPPT] = 5
p.prepare(c)
w, sp, fp = c[t.CUSTOM_TDP].to(int), c[t.SPPT].to(int), c[t.FPPT].to(int)
ok(w <= sp <= fp, f"PL1 <= PL2 <= PL3 enforced ({w}, {sp}, {fp})")

# Boost off collapses them, which is why the sliders are hidden then.
ok(t._derive(45, False) == (45, 45), "boost off means one flat limit")
ok(t._derive(45, True) == (56, 64), "boost on gives real headroom, not 45/45/45")


# ==========================================================================
# 3. Add / select / remove
# ==========================================================================
store([])
p = plugin()
c = mkconf(mode="custom", tdp=20, boost=True, sppt=25, fppt=29, presets=0)
c[t.ADD] = True
p.update(c)
ps = presets_on_disk()
ok(len(ps) == 1 and ps[0]["tdp"] == 20 and ps[0]["sppt"] == 25,
   f"Add freezes the current scratchpad ({ps})")
ok(any(e.get("type") == "settings" for e in p.events),
   "and rebuilds, since the dropdown is fixed at settings() time")

# Selecting a saved preset applies it through adjustor's own custom keys.
store([{"tdp": 9, "boost": True, "sppt": 11, "fppt": 13}])
p = plugin()
c = mkconf(mode="custom", tdp=45, presets=1)
p.prepare(c)
c[t.MODE_PATH] = "z13_p1"
p.prepare(c)
ok(c[t.CUSTOM_TDP].to(int) == 9, f"selecting a preset applies it ({c[t.CUSTOM_TDP].to(int)})")
ok((c[t.SPPT].to(int), c[t.FPPT].to(int)) == (11, 13), "including its limits")
ok(json.load(open(t.STORE_PATH))["applied"] == {"sppt": 11, "fppt": 13},
   "and publishes them for the patch to read")

# A saved preset is immutable: anything that drifts while it is selected is
# put back, so it cannot be edited in place by accident.
c[t.CUSTOM_TDP] = 60
p.prepare(c)
ok(c[t.CUSTOM_TDP].to(int) == 9, f"a saved preset restores its own value ({c[t.CUSTOM_TDP].to(int)})")
ok(presets_on_disk()[0]["tdp"] == 9, "and is never overwritten")

# Remove takes the preset away and lands on Custom, where the values came
# from -- validate_config would otherwise reset the mode to adjustor's default.
p = plugin()
c = mkconf(mode="z13_p1", presets=1)
c["tdp.asus.tdp_v2.z13_p1.remove"] = True
p.update(c)
ok(presets_on_disk() == [], "Remove deletes the preset")
ok(c[t.MODE_PATH].to(str) == "custom", f"and returns to Custom ({c[t.MODE_PATH].to(str)})")


# ==========================================================================
# 4. Telemetry, power change, safety
# ==========================================================================
t._read_w = lambda a: {"ppt_pl1_spl": 9, "ppt_pl2_sppt": 11, "ppt_fppt": 13}[a]
p = plugin()
c = mkconf()
p.update(c)
ok(c[t.APPLIED].to(str).startswith("9 / 11 / 13 W"),
   f"Applied reads the hardware back ({c[t.APPLIED].to(str)!r})")
c[t.APPLIED] = "sentinel"
p.update(c)
ok(c[t.APPLIED].to(str) == "sentinel", "unchanged telemetry is not rewritten")
t._read_w = lambda a: None
p2 = plugin()
c2 = mkconf()
p2.update(c2)
ok(c2[t.APPLIED].to(str) == "unavailable", "unreadable hardware says so")
t._read_w = lambda a: 9


def with_ac(v):
    t._on_ac = lambda: v


store([{"tdp": 45, "boost": True, "sppt": 56, "fppt": 64},
       {"tdp": 9, "boost": True, "sppt": 11, "fppt": 13}])
p = plugin()
c = mkconf(mode="custom", power="enabled", ac="z13_p1", dc="z13_p2", presets=2)
with_ac(True)
p.prepare(c)
ok(c[t.MODE_PATH].to(str) == "custom", "first tick adopts the power source only")
with_ac(False)
p.prepare(c)
ok(c[t.MODE_PATH].to(str) == "z13_p2", f"unplugging selects the preset ({c[t.MODE_PATH].to(str)})")
ok(c[t.CUSTOM_TDP].to(int) == 9, "and applies it")
with_ac(None)
before = c[t.MODE_PATH].to(str)
p.prepare(c)
ok(c[t.MODE_PATH].to(str) == before, "an unreadable charger changes nothing")


# --- the transition is held against KDE ------------------------------------
# Unplugging fires two policies at once: ours picks a mode, and KDE switches
# its own power profile. KDE's arrives as a ppd event a moment later, adjustor
# maps it to a stock mode and FORCE-writes it after our prepare(), so it wins.
# Measured on hardware: z13_p1 selected on unplug, 40 W (quiet's wattage)
# applied two seconds later, and the QAM showing Silent.
class _Clock:
    now = 1000.0

    def monotonic(self):
        return self.now


clock = _Clock()
t.time = clock

store([{"tdp": 45, "boost": True, "sppt": 56, "fppt": 64},
       {"tdp": 9, "boost": True, "sppt": 11, "fppt": 13}])
p = plugin()
c = mkconf(mode="custom", power="enabled", ac="z13_p1", dc="z13_p2", presets=2)
with_ac(True)
p.prepare(c)
with_ac(False)
p.prepare(c)
ok(c[t.MODE_PATH].to(str) == "z13_p2", "unplugging selects the battery preset")

c[t.MODE_PATH] = "quiet"          # what adjustor's ppd handler does to us
clock.now += 2                    # the measured delay
p.prepare(c)
ok(c[t.MODE_PATH].to(str) == "z13_p2",
   f"a ppd change during the transition is overridden ({c[t.MODE_PATH].to(str)})")
ok(c[t.CUSTOM_TDP].to(int) == 9, "and the preset's own values are restored with it")

# ...but only for the window. A profile picked later is a real choice.
clock.now += t.HOLD_S + 1
c[t.MODE_PATH] = "quiet"
p.prepare(c)
p.prepare(c)
ok(c[t.MODE_PATH].to(str) == "quiet",
   f"after the window, a hand-picked mode sticks ({c[t.MODE_PATH].to(str)})")

# The hold arms even when the mode is ALREADY the target -- the overwrite
# lands seconds later, so arming only on a change leaves the common case
# (unplugging while already on the battery preset) unprotected.
clock.now += 100
with_ac(True)
p.prepare(c)
clock.now += 100
with_ac(False)
c[t.MODE_PATH] = "z13_p2"
p.prepare(c)
c[t.MODE_PATH] = "balanced"
clock.now += 2
p.prepare(c)
ok(c[t.MODE_PATH].to(str) == "z13_p2",
   f"the hold arms with no mode change to make ({c[t.MODE_PATH].to(str)})")

# Turning the policy off must drop the hold, or it would keep re-asserting a
# choice the policy is no longer making.
p2 = plugin()
c2 = mkconf(mode="custom", power="disabled", presets=2)
p2._hold = ("z13_p1", clock.now + 100)
p2.prepare(c2)
ok(p2._hold is None, "disabling the policy drops the hold")

# A rebuild is fatal to an unpatched adjustor -- it clears tdp_ready, which
# has no default, and adjustor only ever sets it once behind a latched
# self.init. The whole TDP section disappears until hhd restarts.
store([])
p = plugin(patched=False)
c = mkconf(mode="custom", presets=0)
c[t.ADD] = True
p.update(c)
ok(len(presets_on_disk()) == 1, "Add is still saved while unpatched")
ok(not [e for e in p.events if e.get("type") == "settings"],
   "but no rebuild — it would cost the user their TDP section")
ok(c[t.STATUS].to(str) == t.PATCH_WARNING, "and the QAM says why")

p = plugin(patched=None)
c = mkconf(mode="custom", boost=False, presets=0)
p.update(c)
ok(len([e for e in p.events if e.get("type") == "settings"]) == 1,
   "an unanswerable patch check does not block the rebuild")

with open(t.STORE_PATH, "w") as f:
    f.write("{ not json")
ok(plugin()._load()["presets"] == [], "a corrupt store falls back to empty")

try:
    plugin().prepare(Config({}))
    ok(True, "prepare() survives an empty config")
except Exception as e:
    ok(False, "prepare() survives an empty config", repr(e))

print("\n  RESULT: " + ("ALL PASS" if not fail else f"{fail} FAILURE(S)"))
sys.exit(1 if fail else 0)
