"""Tests for the direct Aura RGB port.

Run: python3 tests/test_aura.py    (no root needed for the packet tests)

The strongest check here is a byte-for-byte diff of our generated packets
against z13ctl-plus's own --dry-run output. That tool is the reference
implementation this module was ported from, and it has been driving these LEDs
in production on this machine, so agreement is real evidence rather than a
restatement of our own assumptions.

Those comparison cases SKIP once z13ctl-plus is uninstalled -- which is the
point of the port -- so the hand-written expectations below stay as the
permanent regression net. They were captured from the reference while it was
still present.
"""

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hhd_z13 import aura

fail = 0


def ok(cond, msg, detail=""):
    global fail
    print(("  PASS " if cond else "  FAIL ") + msg + ("  " + detail if detail else ""))
    if not cond:
        fail += 1


def packets(**kw):
    """Our packet list for a given apply(), as lowercase hex, no padding."""
    a = aura.Aura(dry_run=True)
    # dry_run finds no fds, so force a device list for pure packet generation
    if not a.names:
        a.names = ["keyboard", "lightbar"]
    a.init()
    a.set_power(True)
    a.set_brightness(aura.BRIGHTNESS[kw.get("brightness", "high")])
    rgb = aura._rgb(kw.get("color", "ff0000"))
    rgb2 = aura._rgb(kw.get("color2", "000000"))
    mode = aura.MODES[kw.get("mode", "static")]
    speed = aura.SPEEDS[kw.get("speed", "normal")]
    for z in aura.ZONES:
        a.set_mode(z, mode, rgb, rgb2, speed)
    return a.sent


# ── 1. the packet sequence is exactly what the protocol requires ──────────
p = packets(color="0a0014", brightness="low")
ok(len(p) == 12, f"an apply is 12 packets ({len(p)})")
ok(p[0] == "5db9", "init 1 is the wake packet")
ok(p[1] == "5d4153555320546563682e496e632e",
   "init 2 is ']ASUS Tech.Inc.' with no extra report ID")
ok(p[2] == "5d052031001a", "init 3 is the mode-config header")
ok(p[3] == "5dc00301", "init 4 is the Z13 lightbar enable")
ok(p[4] == "5dbd01ff1fffffff", "power-on sets all four domain bytes")
ok(p[5] == "5dbac5c401", "brightness low is level 1 under report ID 0x5d")
ok(p[6] == "5db300000a0014eb0000000000", "zone 0 carries the colour")
ok(p[7] == "5db5000000" and p[8] == "5db4",
   "every SetMode is followed by SET then APPLY")
ok(p[9].startswith("5db301"), "zone 1 is sent as its own packet")

# ── 2. the randFlag rule, which is a genuine trap ─────────────────────────
# 0xFF hands colour choice to the DEVICE. So pure black is not "off" -- it is
# "pick something". Anything wanting dark has to use brightness off.
black = packets(color="000000")[6]
ok(black[18:20] == "ff",
   f"all-zero colour sets randFlag 0xff (device picks) ({black})")
normal = packets(color="0a0014")[6]
ok(normal[18:20] == "00", f"a real colour sets randFlag 0x00 ({normal})")
breathe = packets(color="ff0000", mode="breathe")[6]
ok(breathe[18:20] == "01", f"breathe sets randFlag 0x01 for dual colour ({breathe})")

# ── 3. mode and speed bytes ───────────────────────────────────────────────
ok(aura.MODES["cycle"] == 0x02 and aura.MODES["rainbow"] == 0x03,
   "cycle=0x02 and rainbow=0x03, matching g-helper and z13ctl-plus")
ok(packets(speed="slow")[6][14:16] == "e1", "slow speed is 0xe1")
ok(packets(speed="fast")[6][14:16] == "f5", "fast speed is 0xf5")

# ── 4. input validation never raises out of apply() ───────────────────────
# apply() is called from a plugin loop that DROPS a plugin which raises, and
# from a sleep hook that must not abort a hibernate over an LED.
for bad in ({"color": "zzz"}, {"color": "12345"}, {"mode": "nope"},
            {"brightness": "very"}, {"color": None}):
    try:
        r = aura.apply(dry_run=True, **bad)
        ok(r is False, f"apply({bad}) returns False rather than raising")
    except Exception as e:
        ok(False, f"apply({bad}) raised {type(e).__name__}")

# ── 5. path parsing -- the bug that made every device look absent ─────────
ok(aura._hidraw_path("/sys/class/hidraw/hidraw0/device/uevent") == "/dev/hidraw0",
   "the class directory 'hidraw' is not mistaken for a device node")
ok(aura._hidraw_path("/sys/class/hidraw/hidraw12/device/uevent") == "/dev/hidraw12",
   "multi-digit device numbers parse")

# ── 6. byte-for-byte against the reference implementation ─────────────────
ref = shutil.which("z13ctl-plus")
if not ref:
    print("  SKIP  z13ctl-plus not installed (reference comparison)")
else:
    cases = [
        {"color": "0a0014", "brightness": "low"},
        {"color": "ff0000", "brightness": "high"},
        {"color": "1e002e", "brightness": "medium"},
        {"color": "00476b", "brightness": "low", "speed": "slow"},
        {"color": "ff6000", "brightness": "low", "mode": "static"},
    ]
    for c in cases:
        argv = [ref, "apply", "--dry-run", "--color", c["color"],
                "--brightness", c["brightness"]]
        if "speed" in c:
            argv += ["--speed", c["speed"]]
        if "mode" in c:
            argv += ["--mode", c["mode"]]
        try:
            out = subprocess.run(argv, capture_output=True, text=True,
                                 timeout=10).stdout
        except Exception as e:
            ok(False, f"reference run failed: {e}")
            continue
        # Their output is zero-padded to 64 bytes; strip the padding to compare
        # payloads. Take the hex blob at the end of each labelled line.
        theirs = []
        for line in out.splitlines():
            tok = line.strip().split()
            if tok and all(ch in "0123456789ABCDEFabcdef" for ch in tok[-1]) \
               and len(tok[-1]) == 128:
                theirs.append(tok[-1].lower().rstrip("0"))
        ours = [x.rstrip("0") for x in packets(**c)]
        ok(ours == theirs,
           f"packets match the reference for {c['color']}/{c['brightness']}",
           "" if ours == theirs else f"\n     ours={ours}\n     ref ={theirs}")

print()
print("  RESULT: " + ("ALL PASS" if not fail else f"{fail} FAILURE(S)"))
raise SystemExit(1 if fail else 0)
