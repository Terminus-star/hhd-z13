"""Tests for the TDP preset plugin.

Run: python3 tests/test_tdp_presets.py   (no root, no writes, no hardware)

The plugin writes no hardware by design -- it only sets config keys that
adjustor reads -- so everything here is exercised against a real Config and a
real merge against adjustor's OWN settings.yml. _on_ac is stubbed; nothing
else needs to be.
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


def plugin(ready=True):
    p = t.Z13TdpPlugin()
    p._tdp_ready = ready
    return p


# ==========================================================================
# 1. Merge into adjustor's own dropdown
# ==========================================================================
hits = sorted(glob.glob("/usr/lib/python3*/site-packages/adjustor/drivers/asus/settings.yml"))
if not hits:
    print("  SKIP  adjustor not installed")
    sys.exit(0)

base = {"tdp": {"asus": yaml.safe_load(open(hits[-1]))}}
m = merge_settings([base, plugin().settings()])
asus = m["tdp"]["asus"]
tv = asus["children"]["tdp_v2"]

ok(asus.get("title") == "Asus TDP",
   f"adjustor's container title survives ({asus.get('title')!r})")
ok(tv.get("default") == "balanced",
   f"adjustor's default mode survives ({tv.get('default')!r})")
ok(tv.get("type") == "mode", f"tdp_v2 is still a mode ({tv.get('type')!r})")

for stock in ("quiet", "balanced", "performance", "custom"):
    ok(stock in tv["modes"], f"adjustor's {stock!r} mode is still there")
for preset in t.PRESET_MODES:
    ok(preset in tv["modes"], f"our {preset!r} mode was added")

ok(len(tv["modes"]) == 8, f"eight modes in one dropdown ({sorted(tv['modes'])})")

# The mirror targets must still exist, or there is nothing to write into.
cust = tv["modes"]["custom"]["children"]
ok("tdp" in cust and "boost" in cust,
   f"adjustor's custom children intact ({sorted(cust)})")

ok("z13_tdp_auto" in asus["children"], "the power policy control merged in")
ok("z13_tdp_status" in asus["children"], "the status field merged in")
ok("fan" in asus["children"], "adjustor's own fan curve survives")

# Slot ranges must match adjustor's enforced range EXACTLY, or its clamp
# fires, writes custom.tdp back, and the mirror fights it.
for preset in t.PRESET_MODES:
    s = tv["modes"][preset]["children"]["tdp"]
    ok(s["min"] == t.MIN_TDP and s["max"] == t.MAX_TDP,
       f"{preset!r} slider is {t.MIN_TDP}..{t.MAX_TDP} W",
       f"(got {s['min']}..{s['max']})")

# Not ready -> absent entirely, rather than an orphan section.
ok(plugin(ready=False).settings() == {}, "contributes nothing until TDP is enabled")


# ==========================================================================
# 2. The mirror
# ==========================================================================
def mkconf(mode="balanced", auto="disabled"):
    presets = {
        p: {"tdp": d, "boost": True}
        for p, d in zip(t.PRESET_MODES, (45, 20, 60, 45))
    }
    return Config({
        "tdp": {"asus": {
            "tdp_v2": {"mode": mode, "custom": {"tdp": 45, "boost": True}, **presets},
            "z13_tdp_auto": {
                "mode": auto,
                "enabled": {"ac_preset": "z13_ac", "dc_preset": "z13_dc"},
            },
            "z13_tdp_status": "",
        }},
        "hhd": {"settings": {"tdp_ready": True}},
    })


p = plugin()
c = mkconf(mode="balanced")
p.prepare(c)
ok(c[t.CUSTOM_TDP].to(int) == 45, "a stock mode is left entirely alone")
ok(p.old_mirror is None, "no baseline is held while off a preset")

c = mkconf(mode="z13_dc")
p = plugin()
p.prepare(c)
ok(c[t.CUSTOM_TDP].to(int) == 20,
   f"selecting a preset mirrors its watts ({c[t.CUSTOM_TDP].to(int)})")
ok(c[t.CUSTOM_BOOST].to(bool) is True, "and its boost flag")

# The oscillation guard. adjustor writes custom.tdp back when its clamp
# fires; if we rewrote unconditionally the two would ping-pong every tick and
# the CPU's power limits would flap.
c[t.CUSTOM_TDP] = 33
p.prepare(c)
ok(c[t.CUSTOM_TDP].to(int) == 33,
   f"an unchanged preset does NOT rewrite over adjustor ({c[t.CUSTOM_TDP].to(int)})")

# But a real edit to the slot must land.
c["tdp.asus.tdp_v2.z13_dc.tdp"] = 9
p.prepare(c)
ok(c[t.CUSTOM_TDP].to(int) == 9, f"editing the slot re-mirrors ({c[t.CUSTOM_TDP].to(int)})")

# Leaving and returning to the SAME preset must re-apply, so the baseline has
# to be dropped on the way out.
c[t.MODE_PATH] = "balanced"
p.prepare(c)
c[t.CUSTOM_TDP] = 45
c[t.MODE_PATH] = "z13_dc"
p.prepare(c)
ok(c[t.CUSTOM_TDP].to(int) == 9, "returning to the same preset re-applies it")

# Out-of-range slots are clamped by us too, so adjustor's clamp never fires.
c["tdp.asus.tdp_v2.z13_dc.tdp"] = 900
p.prepare(c)
ok(c[t.CUSTOM_TDP].to(int) == t.MAX_TDP,
   f"an over-range slot is clamped to {t.MAX_TDP} ({c[t.CUSTOM_TDP].to(int)})")


# ==========================================================================
# 3. Auto-switch: edge-triggered, first tick adopts
# ==========================================================================
def with_ac(val):
    t._on_ac = lambda: val


p = plugin()
c = mkconf(mode="z13_game", auto="enabled")
with_ac(True)
p.prepare(c)
ok(c[t.MODE_PATH].to(str) == "z13_game",
   f"first tick adopts the power source and moves nothing ({c[t.MODE_PATH].to(str)})")

p.prepare(c)
ok(c[t.MODE_PATH].to(str) == "z13_game", "no transition, no change")

with_ac(False)
p.prepare(c)
ok(c[t.MODE_PATH].to(str) == "z13_dc",
   f"unplugging switches to the battery preset ({c[t.MODE_PATH].to(str)})")
ok(c[t.CUSTOM_TDP].to(int) == 20, "and its watts are mirrored in the same tick")

# The whole answer to "don't fight the user": a hand-picked mode after a
# transition stays picked until the NEXT transition.
c[t.MODE_PATH] = "z13_game"
p.prepare(c)
p.prepare(c)
ok(c[t.MODE_PATH].to(str) == "z13_game",
   f"a mode picked by hand afterwards sticks ({c[t.MODE_PATH].to(str)})")

with_ac(True)
p.prepare(c)
ok(c[t.MODE_PATH].to(str) == "z13_ac", "the next transition does take over again")

# Unreadable power source must never guess.
c[t.MODE_PATH] = "z13_game"
with_ac(None)
p.prepare(c)
ok(c[t.MODE_PATH].to(str) == "z13_game", "an unreadable charger changes nothing")

# Disabled policy re-arms, so turning it back on adopts rather than firing a
# stale edge from before it was switched off.
with_ac(True)
c2 = mkconf(mode="z13_game", auto="disabled")
p2 = plugin()
p2.prepare(c2)
ok(p2.old_ac is None, "a disabled policy holds no edge state")
with_ac(False)
p2.prepare(c2)
ok(c2[t.MODE_PATH].to(str) == "z13_game", "a disabled policy never switches")


# ==========================================================================
# 4. The missing-patch banner
# ==========================================================================
p = plugin()
c = mkconf()
t._adjustor_is_patched = lambda: False
p._patched = None
p.update(c)
ok(c[t.STATUS_PATH].to(str) == t.PATCH_WARNING,
   "an unpatched adjustor is reported, not failed silently")

p = plugin()
c = mkconf()
t._adjustor_is_patched = lambda: True
p._patched = None
p.update(c)
ok(c[t.STATUS_PATH].to(str) == "", "a patched adjustor shows nothing")

# Undeterminable must not claim a fault -- failing this check open is worse
# than a false banner only when we KNOW it is unpatched.
p = plugin()
c = mkconf()
t._adjustor_is_patched = lambda: None
p._patched = None
p.update(c)
ok(c[t.STATUS_PATH].to(str) == "", "an unanswerable check makes no claim")

# prepare() must never raise out, whatever the config looks like.
p = plugin()
try:
    p.prepare(Config({}))
    ok(True, "prepare() survives an empty config")
except Exception as e:
    ok(False, "prepare() survives an empty config", repr(e))

print("\n  RESULT: " + ("ALL PASS" if not fail else f"{fail} FAILURE(S)"))
sys.exit(1 if fail else 0)
