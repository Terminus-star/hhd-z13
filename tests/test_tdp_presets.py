"""Tests for the TDP preset plugin.

Run: python3 tests/test_tdp_presets.py   (no root, no writes, no hardware)

The plugin writes no hardware by design -- it only sets config keys adjustor
reads -- so everything is exercised against a real Config and a real merge
against adjustor's OWN settings.yml. _on_ac is stubbed; nothing else is.
"""

import glob
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hhd.plugins.settings import merge_settings
from hhd.plugins.conf import Config

from hhd_z13 import tdp as t

fail = 0


def ok(cond, msg, detail=""):
    global fail
    print(("  PASS " if cond else "  FAIL ") + msg + ("  " + detail if detail else ""))
    if not cond:
        fail += 1


def plugin(ready=True, count=2):
    p = t.Z13TdpPlugin()
    p._tdp_ready = ready
    p._count = count
    p.events = []
    p.emit = p.events.append
    return p


def slot(w=45, boost=True, ppt="auto", sppt=45, fppt=45):
    return {
        "tdp": w, "boost": boost,
        "ppt": {"mode": ppt, "manual": {"sppt": sppt, "fppt": fppt}},
    }


def mkconf(mode="balanced", auto="disabled", ac="p1", dc="p2", active="p1",
           count=2, slots=None):
    slots = slots or {"p1": slot(45), "p2": slot(20)}
    return Config({
        "tdp": {"asus": {
            "tdp_v2": {"mode": mode, "custom": {"tdp": 45, "boost": True}},
            "z13_presets": {
                "active": {"mode": active, **slots},
                "count": count, "add": False, "remove": False,
                "sppt": 0, "fppt": 0,
            },
            "z13_tdp_auto": {
                "mode": auto,
                "enabled": {"ac_preset": ac, "dc_preset": dc},
            },
            "z13_tdp_status": "",
        }},
        "hhd": {"settings": {"tdp_ready": True}},
    })


# ==========================================================================
# 1. The generated tree
# ==========================================================================
s = plugin(count=3).settings()
node = s["tdp"]["asus"]["children"]["z13_presets"]
modes = node["children"]["active"]["modes"]
ok(sorted(modes) == ["p1", "p2", "p3"], f"count drives the slot list ({sorted(modes)})")
ok(modes["p2"]["title"] == "Preset 2", f"slots are titled ({modes['p2']['title']!r})")

# deepcopy per slot, not a YAML alias: the tree is walked and annotated in
# several places, so shared dict objects would be a latent aliasing bug.
modes["p1"]["children"]["tdp"]["default"] = 999
ok(modes["p2"]["children"]["tdp"]["default"] != 999,
   "slots are independent objects, not aliases")

pt = modes["p1"]["children"]
ok(pt["tdp"]["min"] == t.MIN_TDP and pt["tdp"]["max"] == t.MAX_TDP,
   f"PL1 spans adjustor's enforced range ({pt['tdp']['min']}..{pt['tdp']['max']})")
ok(sorted(pt["ppt"]["modes"]) == ["auto", "manual"], "PL2/PL3 have an auto/manual gate")
ok(sorted(pt["ppt"]["modes"]["manual"]["children"]) == ["fppt", "sppt"],
   "manual exposes PL2 and PL3")

opts = s["tdp"]["asus"]["children"]["z13_tdp_auto"]["modes"]["enabled"]["children"]["ac_preset"]["options"]
ok(sorted(opts) == ["balanced", "p1", "p2", "p3", "performance", "quiet"],
   f"power-change targets are presets + stock profiles ({sorted(opts)})")
# adjustor's keys and titles disagree; using its titles avoids two different
# meanings of "Performance" in one list.
ok(opts["balanced"] == "Performance" and opts["performance"] == "Turbo"
   and opts["quiet"] == "Silent",
   f"stock profiles use adjustor's own labels ({[opts[k] for k in t.STOCK_MODES]})")

ok(plugin(ready=False).settings() == {}, "contributes nothing until TDP is enabled")

# --- merge against adjustor's real tree -----------------------------------
hits = sorted(glob.glob("/usr/lib/python3*/site-packages/adjustor/drivers/asus/settings.yml"))
if hits:
    base = {"tdp": {"asus": yaml.safe_load(open(hits[-1]))}}
    m = merge_settings([base, plugin().settings()])
    asus = m["tdp"]["asus"]
    ok(asus.get("title") == "Asus TDP", "adjustor's container title survives")
    tv = asus["children"]["tdp_v2"]
    ok(sorted(tv["modes"]) == ["balanced", "custom", "performance", "quiet"],
       f"adjustor's dropdown is UNTOUCHED -- no preset modes ({sorted(tv['modes'])})")
    ok("tdp" in tv["modes"]["custom"]["children"], "the mirror target still exists")
    ok("fan" in asus["children"], "adjustor's fan curve survives")
    ok("z13_presets" in asus["children"], "our preset container merged in")
else:
    print("  SKIP  adjustor not installed (merge checks)")


# ==========================================================================
# 2. Applying a preset
# ==========================================================================
p = plugin()
c = mkconf(active="p1")
p.prepare(c)
ok(c[t.MODE_PATH].to(str) == "balanced",
   "first tick adopts and applies nothing (hhd starting must not change TDP)")

c["tdp.asus.z13_presets.active.p1.tdp"] = 9
p.prepare(c)
ok(c[t.CUSTOM_TDP].to(int) == 9, f"editing a preset applies it ({c[t.CUSTOM_TDP].to(int)})")
ok(c[t.MODE_PATH].to(str) == "custom", "and selects adjustor's Custom mode")
# Auto scales from THIS panel's firmware proportions (60/75/86 W shipped),
# not from a fixed ceiling. 9 W -> 11/13.
ok((c[t.SPPT].to(int), c[t.FPPT].to(int)) == (11, 13),
   f"Auto scales PL2/PL3 from the panel's own ratios "
   f"({c[t.SPPT].to(int)}, {c[t.FPPT].to(int)})")

# The value that matters: at 45 W a fixed 35 W ceiling would give 45/45/45,
# i.e. the boost flag doing nothing at the wattage this machine runs at most.
p_ = plugin()
c_ = mkconf(active="p1", slots={"p1": slot(20), "p2": slot(20)})
p_.prepare(c_)
c_["tdp.asus.z13_presets.active.p1.tdp"] = 45
p_.prepare(c_)
ok((c_[t.CUSTOM_TDP].to(int), c_[t.SPPT].to(int), c_[t.FPPT].to(int)) == (45, 56, 64),
   f"45 W yields real headroom, not 45/45/45 "
   f"({c_[t.CUSTOM_TDP].to(int)}/{c_[t.SPPT].to(int)}/{c_[t.FPPT].to(int)})")

# Ceilings are the firmware's own max_value figures.
c_["tdp.asus.z13_presets.active.p1.tdp"] = 65
p_.prepare(c_)
ok(c_[t.SPPT].to(int) <= t.MAX_SPPT and c_[t.FPPT].to(int) <= t.MAX_FPPT,
   f"capped at the firmware maxima "
   f"({c_[t.SPPT].to(int)} <= {t.MAX_SPPT}, {c_[t.FPPT].to(int)} <= {t.MAX_FPPT})")

# Boost off means one flat limit, at every wattage.
p_ = plugin()
c_ = mkconf(active="p1", slots={"p1": slot(45, boost=False), "p2": slot(20)})
p_.prepare(c_)
c_["tdp.asus.z13_presets.active.p1.tdp"] = 46
p_.prepare(c_)
ok(c_[t.SPPT].to(int) == 46 and c_[t.FPPT].to(int) == 46,
   f"boost off gives one flat limit ({c_[t.SPPT].to(int)}, {c_[t.FPPT].to(int)})")

# THE KDE TOLERANCE. adjustor's ppd handler force-writes a stock mode over the
# config on every power transition. Stock Custom survives that because the
# quiet branch writes only a fan curve and never touches PPT -- the wattage
# stays. We must inherit that by NOT fighting for the label; re-asserting
# would be an endless loop against KDE for no gain.
c[t.MODE_PATH] = "quiet"
p.prepare(c)
p.prepare(c)
ok(c[t.MODE_PATH].to(str) == "quiet",
   f"the mode is never re-asserted once applied ({c[t.MODE_PATH].to(str)})")
ok(c[t.CUSTOM_TDP].to(int) == 9, "and the applied wattage is untouched")

# --- manual PL2/PL3 -------------------------------------------------------
p = plugin()
c = mkconf(active="p1", slots={"p1": slot(30, ppt="manual", sppt=50, fppt=70),
                               "p2": slot(20)})
p.prepare(c)
c["tdp.asus.z13_presets.active.p1.tdp"] = 31
p.prepare(c)
ok((c[t.SPPT].to(int), c[t.FPPT].to(int)) == (50, 70),
   f"manual limits are published verbatim ({c[t.SPPT].to(int)}, {c[t.FPPT].to(int)})")

# The firmware REJECTS a lower tier exceeding a higher one rather than
# clamping, so a nonsensical order would silently apply nothing.
p = plugin()
c = mkconf(active="p1", slots={"p1": slot(60, ppt="manual", sppt=10, fppt=5),
                               "p2": slot(20)})
p.prepare(c)
c["tdp.asus.z13_presets.active.p1.tdp"] = 61
p.prepare(c)
w, sp, fp = c[t.CUSTOM_TDP].to(int), c[t.SPPT].to(int), c[t.FPPT].to(int)
ok(w <= sp <= fp, f"PL1 <= PL2 <= PL3 is enforced before writing ({w}, {sp}, {fp})")


# ==========================================================================
# 3. Auto-switch
# ==========================================================================
def with_ac(val):
    t._on_ac = lambda: val


p = plugin()
c = mkconf(auto="enabled", ac="p1", dc="p2", active="p1")
with_ac(True)
p.prepare(c)
ok(c[t.MODE_PATH].to(str) == "balanced", "first tick adopts the power source only")

with_ac(False)
p.prepare(c)
ok(c["tdp.asus.z13_presets.active.mode"].to(str) == "p2",
   f"unplugging selects the battery preset ({c[t.ACTIVE].to(str)})")
ok(c[t.CUSTOM_TDP].to(int) == 20, f"and applies it ({c[t.CUSTOM_TDP].to(int)})")
ok(c[t.MODE_PATH].to(str) == "custom", "and selects Custom")

# The transition must re-assert even when the VALUES are unchanged, because
# the mode may have been moved off custom in between.
c[t.MODE_PATH] = "quiet"
with_ac(True)
p.prepare(c)
with_ac(False)
p.prepare(c)
ok(c[t.MODE_PATH].to(str) == "custom",
   "a transition re-asserts Custom even with identical values")

# A stock profile is a legitimate target.
p = plugin()
c = mkconf(auto="enabled", ac="quiet", dc="performance", active="p1")
with_ac(True)
p.prepare(c)
with_ac(False)
p.prepare(c)
ok(c[t.MODE_PATH].to(str) == "performance",
   f"a stock profile can be the battery target ({c[t.MODE_PATH].to(str)})")

with_ac(None)
before = c[t.MODE_PATH].to(str)
p.prepare(c)
ok(c[t.MODE_PATH].to(str) == before, "an unreadable charger changes nothing")

p2 = plugin()
c2 = mkconf(auto="disabled", active="p1")
with_ac(True)
p2.prepare(c2)
ok(p2.old_ac is None, "a disabled policy holds no edge state")


# ==========================================================================
# 4. Add / remove
# ==========================================================================
p = plugin(count=2)
c = mkconf(count=2, active="p1", slots={"p1": slot(9), "p2": slot(20)})
c[t.ADD] = True
p.update(c)
ok(c[t.COUNT].to(int) == 3, f"Add grows the list ({c[t.COUNT].to(int)})")
ok(any(e.get("type") == "settings" for e in p.events),
   "and asks hhd to rebuild the tree (the list is fixed at settings() time)")
ok(c["tdp.asus.z13_presets.active.p3.tdp"].to(int) == 9,
   "a new slot is seeded from the active one, not from a default nobody chose")

p = plugin(count=2)
c = mkconf(count=2, active="p2")
c[t.REMOVE] = True
p.update(c)
ok(c[t.COUNT].to(int) == 1, f"Remove shrinks the list ({c[t.COUNT].to(int)})")
ok(c[t.ACTIVE].to(str) == "p1",
   f"removing the SELECTED slot steps the selection back ({c[t.ACTIVE].to(str)})")

p = plugin(count=1)
c = mkconf(count=1, active="p1", slots={"p1": slot(9)})
c[t.REMOVE] = True
p.update(c)
ok(c[t.COUNT].to(int) == 1, "the last preset cannot be removed")

p = plugin(count=t.MAX_PRESETS)
c = mkconf(count=t.MAX_PRESETS, active="p1")
c[t.ADD] = True
p.update(c)
ok(c[t.COUNT].to(int) == t.MAX_PRESETS, f"capped at {t.MAX_PRESETS}")

# An externally edited count (or one restored from state.yml at startup) must
# rebuild exactly once, not every tick.
p = plugin(count=2)
c = mkconf(count=5)
p.update(c)
ok(p._count == 5, f"the stored count is adopted ({p._count})")
n = len([e for e in p.events if e.get("type") == "settings"])
p.update(c)
ok(len([e for e in p.events if e.get("type") == "settings"]) == n,
   "and rebuilds once, not on every tick")


# ==========================================================================
# 5. The missing-patch banner
# ==========================================================================
for state, expect in ((False, t.PATCH_WARNING), (True, ""), (None, "")):
    p = plugin()
    c = mkconf()
    t._adjustor_is_patched = lambda: state
    p._patched = None
    p.update(c)
    label = {False: "unpatched is reported", True: "patched shows nothing",
             None: "unanswerable makes no claim"}[state]
    ok(c[t.STATUS].to(str) == expect, label)

p = plugin()
try:
    p.prepare(Config({}))
    ok(True, "prepare() survives an empty config")
except Exception as e:
    ok(False, "prepare() survives an empty config", repr(e))

print("\n  RESULT: " + ("ALL PASS" if not fail else f"{fail} FAILURE(S)"))
sys.exit(1 if fail else 0)
