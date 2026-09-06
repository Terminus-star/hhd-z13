"""Tests for the TDP preset plugin.

Run: python3 tests/test_tdp_presets.py   (no root, no writes outside a tmpdir)

The plugin writes no hardware -- it sets config keys adjustor reads, plus one
JSON store. The store is redirected into a tmpdir and the sysfs reader is
stubbed, so nothing on the machine is touched.
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

TMP = tempfile.mkdtemp(prefix="z13tdp-")
t.STORE_PATH = os.path.join(TMP, "presets.json")

fail = 0


def ok(cond, msg, detail=""):
    global fail
    print(("  PASS " if cond else "  FAIL ") + msg + ("  " + detail if detail else ""))
    if not cond:
        fail += 1


def fresh_store(presets=None):
    try:
        os.unlink(t.STORE_PATH)
    except FileNotFoundError:
        pass
    if presets is not None:
        with open(t.STORE_PATH, "w") as f:
            json.dump({"presets": presets, "applied": {"sppt": 0, "fppt": 0}}, f)


def plugin(ready=True, boost=True):
    p = t.Z13TdpPlugin()
    p._tdp_ready = ready
    p._gen_boost = boost
    p.events = []
    p.emit = p.events.append
    return p


def mkconf(mode="balanced", power="disabled", ac="p1", dc="p2", preset="p1",
           tdp=45, boost=True, ppt="auto", sppt=0, fppt=0):
    return Config({
        "tdp": {"asus": {
            "tdp_v2": {"mode": mode, "custom": {
                # adjustor's own two -- the single editor
                "tdp": tdp, "boost": boost,
                # ours
                "z13_preset": {"mode": preset},
                "z13_ppt": {"mode": ppt, "manual": {"sppt": sppt, "fppt": fppt}},
                "z13_applied": "", "z13_add": False, "z13_remove": False,
                "z13_status": "",
            }},
            "z13_power": {
                "mode": power,
                "enabled": {"ac_preset": ac, "dc_preset": dc},
            },
        }},
        "hhd": {"settings": {"tdp_ready": True}},
    })


# ==========================================================================
# 1. The generated tree
# ==========================================================================
fresh_store()
s = plugin().settings()
asus = s["tdp"]["asus"]["children"]
kids = asus["tdp_v2"]["modes"]["custom"]["children"]

ok(sorted(kids["z13_preset"]["modes"]) == ["p1", "p2"],
   f"slots come from the store ({sorted(kids['z13_preset']['modes'])})")

# THE fix for the duplicate sliders: a slot carries no controls of its own.
# adjustor's TDP / TDP Boost are the single editor; the slot is storage.
ok(all("children" not in v for v in kids["z13_preset"]["modes"].values()),
   "slot containers are EMPTY — no second TDP slider")
ok("tdp" not in kids and "boost" not in kids,
   f"we contribute no TDP/boost control of our own ({sorted(kids)})")

# The power-change policy is its own area, not nested in Custom: it has to be
# reachable from any mode, including the one KDE flips you to on unplug.
ok("z13_power" in asus, "power-change policy is a sibling, in its own area")
ok("z13_power" not in kids, "and NOT buried inside Custom")

opts = asus["z13_power"]["modes"]["enabled"]["children"]["ac_preset"]["options"]
ok(sorted(opts) == ["balanced", "p1", "p2", "performance", "quiet"],
   f"targets are presets + stock profiles ({sorted(opts)})")
ok(opts["balanced"] == "Performance" and opts["performance"] == "Turbo",
   "stock profiles use adjustor's own labels, which disagree with its keys")

# Boost is what makes PL2/PL3 differ from PL1 at all, so with it off the
# control has nothing to say and is dropped rather than shown inert.
ok("z13_ppt" in kids, "Power Limits shown while boost is on")
off = plugin(boost=False).settings()["tdp"]["asus"]["children"]["tdp_v2"]["modes"]["custom"]["children"]
ok("z13_ppt" not in off, f"Power Limits GONE while boost is off ({sorted(off)})")
ok("z13_applied" in off, "telemetry stays either way")

ok(kids["z13_applied"]["type"] == "display",
   f"Applied is telemetry, not sliders ({kids['z13_applied']['type']})")

ok(plugin(ready=False).settings() == {}, "contributes nothing until TDP is enabled")

# --- merge against adjustor's real tree -----------------------------------
hits = sorted(glob.glob("/usr/lib/python3*/site-packages/adjustor/drivers/asus/settings.yml"))
if hits:
    base = {"tdp": {"asus": yaml.safe_load(open(hits[-1]))}}
    fresh_store()
    m = merge_settings([base, plugin().settings()])
    a = m["tdp"]["asus"]
    tv = a["children"]["tdp_v2"]
    ok(a.get("title") == "Asus TDP", "adjustor's container title survives")
    ok(sorted(tv["modes"]) == ["balanced", "custom", "performance", "quiet"],
       f"adjustor's dropdown UNTOUCHED ({sorted(tv['modes'])})")
    cu = tv["modes"]["custom"]["children"]
    raw = yaml.safe_load(open(hits[-1]))["children"]["tdp_v2"]["modes"]["custom"]["children"]
    ok(cu["tdp"].get("min") == raw["tdp"]["min"]
       and cu["tdp"].get("tags") == raw["tdp"].get("tags", []),
       "adjustor's TDP slider is left completely alone now")
    ok(len([k for k in cu if "tdp" in k.lower()]) == 1,
       f"exactly one TDP control in the menu ({[k for k in cu if 'tdp' in k.lower()]})")
    ok("z13_preset" in cu, "presets merged inside Custom")
    ok("fan" in a["children"], "adjustor's fan curve survives")
else:
    print("  SKIP  adjustor not installed (merge checks)")


# ==========================================================================
# 2. Load / save against adjustor's sliders
# ==========================================================================
fresh_store([{"tdp": 45, "boost": True, "ppt": "auto", "sppt": 0, "fppt": 0},
             {"tdp": 9, "boost": True, "ppt": "auto", "sppt": 0, "fppt": 0}])
p = plugin()
c = mkconf(preset="p1", tdp=45)
p.prepare(c)
ok(c[t.MODE_PATH].to(str) == "balanced",
   "first tick adopts: hhd starting changes nothing")

c[t.PRESET] = "p2"
p.prepare(c)
ok(c[t.CUSTOM_TDP].to(int) == 9,
   f"selecting a preset loads it into adjustor's slider ({c[t.CUSTOM_TDP].to(int)})")
ok(c[t.MODE_PATH].to(str) == "custom", "and selects Custom")

# There is only one slider, so editing it is the only way to edit a preset.
c[t.CUSTOM_TDP] = 20
p.prepare(c)
saved = json.load(open(t.STORE_PATH))["presets"][1]["tdp"]
ok(saved == 20, f"editing the slider saves into the active preset ({saved})")

c[t.PRESET] = "p1"
p.prepare(c)
ok(c[t.CUSTOM_TDP].to(int) == 45,
   f"the other preset keeps its own value ({c[t.CUSTOM_TDP].to(int)})")
c[t.PRESET] = "p2"
p.prepare(c)
ok(c[t.CUSTOM_TDP].to(int) == 20, f"and the edit persisted ({c[t.CUSTOM_TDP].to(int)})")

# A load must never be mistaken for an edit, or switching presets would
# overwrite the one you switched away from.
ok(json.load(open(t.STORE_PATH))["presets"][0]["tdp"] == 45,
   "switching presets does not overwrite the previous one")


# ==========================================================================
# 3. The three limits
# ==========================================================================
p = plugin()
ok(p._limits(45, True, "auto", 0, 0) == (56, 64),
   f"Auto uses this panel's ratios ({p._limits(45, True, 'auto', 0, 0)})")
ok(p._limits(9, True, "auto", 0, 0) == (11, 13),
   f"...at any wattage ({p._limits(9, True, 'auto', 0, 0)})")
ok(p._limits(65, True, "auto", 0, 0) == (81, 93),
   f"...capped at the firmware maxima ({p._limits(65, True, 'auto', 0, 0)})")
# The case that matters: a fixed 35 W ceiling would make this 45/45/45.
ok(p._limits(45, True, "auto", 0, 0) != (45, 45), "45 W is not flattened")
ok(p._limits(45, False, "auto", 0, 0) == (45, 45),
   "boost off means one flat limit — which is why Power Limits is hidden then")
ok(p._limits(30, True, "manual", 10, 5) == (30, 30),
   f"PL1 <= PL2 <= PL3 enforced ({p._limits(30, True, 'manual', 10, 5)})")
ok(p._limits(30, True, "manual", 50, 70) == (50, 70), "manual values pass through")

# The patch reads the store, so the targets must land there.
fresh_store([{"tdp": 45, "boost": True, "ppt": "auto", "sppt": 0, "fppt": 0}])
p = plugin()
c = mkconf(preset="p1", tdp=45)
p.prepare(c)
c[t.CUSTOM_TDP] = 46
p.prepare(c)
app = json.load(open(t.STORE_PATH))["applied"]
ok(app == {"sppt": 58, "fppt": 66}, f"targets published for the patch ({app})")


# ==========================================================================
# 4. Telemetry
# ==========================================================================
t._read_w = lambda a: {"ppt_pl1_spl": 45, "ppt_pl2_sppt": 56, "ppt_fppt": 64}[a]
p = plugin()
c = mkconf()
p.update(c)
ok(c[t.APPLIED].to(str).startswith("45 / 56 / 64 W"),
   f"Applied reads the hardware back ({c[t.APPLIED].to(str)!r})")

# Every config write wakes hhd's long-poll and redraws the UI, so an unchanged
# reading must not be rewritten.
c[t.APPLIED] = "sentinel"
p.update(c)
ok(c[t.APPLIED].to(str) == "sentinel", "unchanged telemetry is not rewritten")

t._read_w = lambda a: None
p2 = plugin()
c2 = mkconf()
p2.update(c2)
ok(c2[t.APPLIED].to(str) == "unavailable", "unreadable hardware says so")


# ==========================================================================
# 5. Add / remove, and the boost gate rebuild
# ==========================================================================
t._read_w = lambda a: 45
fresh_store([{"tdp": 9, "boost": True, "ppt": "auto", "sppt": 0, "fppt": 0},
             {"tdp": 20, "boost": True, "ppt": "auto", "sppt": 0, "fppt": 0}])
p = plugin()
c = mkconf(preset="p1")
c[t.ADD] = True
p.update(c)
st = json.load(open(t.STORE_PATH))["presets"]
ok(len(st) == 3, f"Add grows the store ({len(st)})")
ok(st[2]["tdp"] == 9, f"and seeds from the active slot ({st[2]['tdp']})")
ok(any(e.get("type") == "settings" for e in p.events),
   "and asks hhd to rebuild — the slot list is fixed at settings() time")

p = plugin()
c = mkconf(preset="p3")
c[t.REMOVE] = True
p.update(c)
ok(len(json.load(open(t.STORE_PATH))["presets"]) == 2, "Remove shrinks the store")
ok(c[t.PRESET].to(str) == "p2",
   f"removing the SELECTED slot steps the selection back ({c[t.PRESET].to(str)})")

fresh_store([{"tdp": 9, "boost": True, "ppt": "auto", "sppt": 0, "fppt": 0}])
p = plugin()
c = mkconf()
c[t.REMOVE] = True
p.update(c)
ok(len(json.load(open(t.STORE_PATH))["presets"]) == 1,
   "the last preset cannot be removed")

# Toggling boost rebuilds the tree once, so Power Limits appears or goes.
t._adjustor_is_patched = lambda: True
p = plugin(boost=True)
c = mkconf(boost=False)
p.update(c)
n = len([e for e in p.events if e.get("type") == "settings"])
ok(n == 1, f"toggling boost rebuilds the tree ({n})")
p.update(c)
ok(len([e for e in p.events if e.get("type") == "settings"]) == 1,
   "and only once, not on every tick")

# THE SAFETY PROPERTY. A settings rebuild is fatal to an UNPATCHED adjustor:
# hhd.settings.tdp_ready has no default, so a rebuild resets it to None, and
# adjustor only ever sets it once -- behind a self.init that latches on first
# initialisation. Its asus/battery/init plugins all gate settings() on that
# flag, so the entire TDP section disappears from the QAM until hhd restarts.
# Observed exactly that. A control of ours appearing is never worth the user's
# TDP controls vanishing.
t._adjustor_is_patched = lambda: False
p = plugin(boost=True)
c = mkconf(boost=False)
p.update(c)
ok(not [e for e in p.events if e.get("type") == "settings"],
   "no rebuild while adjustor is unpatched — it would lose its TDP section")
ok(c[t.STATUS].to(str) == t.PATCH_WARNING, "and the QAM says why")

# Add still records the change; it just cannot refresh the list until restart.
fresh_store([{"tdp": 9, "boost": True, "ppt": "auto", "sppt": 0, "fppt": 0}])
p = plugin()
c = mkconf()
c[t.ADD] = True
p.update(c)
ok(len(json.load(open(t.STORE_PATH))["presets"]) == 2,
   "Add is still saved while unpatched")
ok(not [e for e in p.events if e.get("type") == "settings"],
   "but still no rebuild")

# Unanswerable must not block: only a KNOWN-unpatched adjustor is unsafe.
t._adjustor_is_patched = lambda: None
p = plugin(boost=True)
c = mkconf(boost=False)
p.update(c)
ok(len([e for e in p.events if e.get("type") == "settings"]) == 1,
   "an unanswerable patch check does not block the rebuild")

t._adjustor_is_patched = lambda: True


# ==========================================================================
# 6. Power change, and robustness
# ==========================================================================
def with_ac(v):
    t._on_ac = lambda: v


fresh_store([{"tdp": 45, "boost": True, "ppt": "auto", "sppt": 0, "fppt": 0},
             {"tdp": 9, "boost": True, "ppt": "auto", "sppt": 0, "fppt": 0}])
p = plugin()
c = mkconf(power="enabled", ac="p1", dc="p2", preset="p1")
with_ac(True)
p.prepare(c)
ok(c[t.PRESET].to(str) == "p1", "first tick adopts the power source only")
with_ac(False)
p.prepare(c)
ok(c[t.PRESET].to(str) == "p2",
   f"unplugging selects the battery preset ({c[t.PRESET].to(str)})")
ok(c[t.CUSTOM_TDP].to(int) == 9, f"and loads it ({c[t.CUSTOM_TDP].to(int)})")
ok(c[t.MODE_PATH].to(str) == "custom", "and selects Custom")

with_ac(None)
before = c[t.PRESET].to(str)
p.prepare(c)
ok(c[t.PRESET].to(str) == before, "an unreadable charger changes nothing")

p2 = plugin()
c2 = mkconf(power="disabled")
with_ac(True)
p2.prepare(c2)
ok(p2.old_ac is None, "a disabled policy holds no edge state")

# A corrupt store must self-heal rather than take the plugin down.
with open(t.STORE_PATH, "w") as f:
    f.write("{ not json")
p3 = plugin()
ok(len(p3._load_store()["presets"]) >= 1, "a corrupt store falls back to defaults")

p4 = plugin()
try:
    p4.prepare(Config({}))
    ok(True, "prepare() survives an empty config")
except Exception as e:
    ok(False, "prepare() survives an empty config", repr(e))

print("\n  RESULT: " + ("ALL PASS" if not fail else f"{fail} FAILURE(S)"))
sys.exit(1 if fail else 0)
