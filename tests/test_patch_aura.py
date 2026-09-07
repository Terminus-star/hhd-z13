"""Tests for the aura wake-reinit patch entry.

Run: python3 tests/test_patch_aura.py   (no root, no writes)

Dry-runs the substitution against the REAL installed aura driver and throws
the result away. Nothing is written, so this is safe to run on the machine
that is misbehaving.

WHAT THIS GUARDS
    The keyboard and lightbar came back from every resume showing one flat
    colour and stayed there. A writer that paints all zones alike, plus a
    change-gate:

      - Something writes the hardware across a resume. Originally the
        z13ctl-plus daemon restoring its saved lighting; now that it is gone,
        the sleep hooks' own completion signals, which are equally one colour
        for both zones. Removing the daemon did NOT remove the need for this.
      - hhd's aura driver holds the real per-device config but update() skips
        a device whose config has not changed. Sleep changes nothing, so hhd
        sends nothing and never corrects the hardware.

    hhd is not losing a race here -- it is not writing at all, so no amount of
    delay or retry would help. The device has to be marked dirty, which is
    what init=True does.

    Upstream has this fix commented out, below a `tdp_changes` early-return
    that is off by default. Position is therefore load-bearing and is asserted
    below: the block has to sit ABOVE that guard or it is unreachable.
"""

import ast
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATCHER = os.path.join(ROOT, "hhd-patches", "hhd-qam-toggle-patch.py")

fail = 0


def ok(cond, msg, detail=""):
    global fail
    print(("  PASS " if cond else "  FAIL ") + msg + ("  " + detail if detail else ""))
    if not cond:
        fail += 1


spec = importlib.util.spec_from_file_location("z13patch", PATCHER)
m = importlib.util.module_from_spec(spec)
sys.argv = ["x", "--check"]          # never let it take the --apply path
spec.loader.exec_module(m)

try:
    path = m.find_aura()
except SystemExit:
    print("  SKIP  hhd aura driver not installed")
    raise SystemExit(0)

src = open(path).read()
label, anchor, body, sentinel, mode = m.AURA_PATCHES[0]

# Always exercise the TABLE against a PRISTINE driver, never against whatever
# is installed. Two earlier versions of this test got that wrong in opposite
# directions: the first asserted the anchor unconditionally and began failing
# the moment the patch was applied, and the second tested the installed file,
# so it reported three failures when the installed patch was merely an OLDER
# body. Neither is a defect in the table, which is what this file is for.
#
# The patcher's own .z13-orig backup is the pristine copy, and it is written
# before the first patch, so it always exists on a patched machine.
backup = path + ".z13-orig"
if os.path.exists(backup) and m.MARK_AURA not in open(backup).read():
    src = open(backup).read()
    print(f"  note: testing the table against {os.path.basename(backup)}")
elif m.MARK_AURA in src:
    print("  SKIP  driver is patched and no pristine backup exists")
    raise SystemExit(0)

ok(src.count(anchor) == 1,
   f"the anchor is unique in the pristine driver ({src.count(anchor)})")
out = src.replace(anchor, body.format(mark=m.MARK_AURA))
ok(out != src, "the substitution applies")

# Re-applying must not double the body. apply_marked skips on the sentinel,
# but the table itself should be idempotent under replace().
ok(out.replace(anchor, body.format(mark=m.MARK_AURA)) == out,
   "a second application changes nothing")

# Whether the INSTALLED file matches the current table is a separate question
# from whether the table is correct, and it is worth reporting rather than
# asserting -- a stale patch is fixed by --revert then install.sh.
installed = open(path).read()
if m.MARK_AURA in installed and "self.queue_apply[dev[" not in installed:
    print("  note: the INSTALLED patch is an older body; "
          "run hhd-patches --revert then install.sh")
ok(m.MARK_AURA in out, "and leaves the mark, so --check can see it")
ok(sentinel in out, "and the sentinel apply_marked tests for")

try:
    ast.parse(out)
    ok(True, "the patched driver is valid Python")
except SyntaxError as e:
    ok(False, f"the patched driver is valid Python ({e})")

# The whole point of not simply uncommenting upstream's version.
i_wake = out.index('ev.get("event") == "wakeup"')
i_guard = out.index("if not self.tdp_changes:")
ok(i_wake < i_guard,
   "the wakeup block sits ABOVE the tdp_changes early-return")

ok(out.count('ev.get("event") == "wakeup"') == 1,
   "exactly one wakeup block, not a doubled body")

# queue_apply is the mechanism, and NOT init. Setting init fires the handshake
# immediately; after a hibernate the USB device has power-cycled and is still
# re-enumerating, so that write fails, update() marks the device disabled and
# drops it, and the settings rebuild that follows resets the TDP custom value
# to its default. One transient write cost an RGB section and a wattage.
ok("self.queue_apply[dev[" in out, "the resume re-apply is QUEUED")
ok('dev["init"] = True' not in out,
   "and does not force an immediate init handshake")
ok("time.perf_counter() + 4.0" in out,
   "with a delay long enough for the device to re-enumerate")
ok('if not dev["disabled"]' in out,
   "a disabled device is skipped here rather than written to")

# --- entry 2: a failed device must be able to come back -------------------
# update() disables a device on ANY write error and get_aura_devices() then
# skips it forever, because it only opens paths it does not already know.
labels = [e[0] for e in m.AURA_PATCHES]
ok("device-revive" in labels, f"the revive entry exists ({labels})")

rev = [e for e in m.AURA_PATCHES if e[0] == "device-revive"][0]
r_anchor, r_body, r_sentinel = rev[1], rev[2], rev[3]
ok(out.count(r_anchor) >= 1, "its anchor is present in the pristine driver")
if r_sentinel not in out:
    r_out = out.replace(r_anchor, r_body.format(mark=m.MARK_AURA), 1)
    ok(r_sentinel in r_out, "the revive substitution applies")
    import ast as _ast
    try:
        _ast.parse(r_out)
        ok(True, "and the result is valid Python")
    except SyntaxError as e:
        ok(False, f"and the result is valid Python ({e})")
    # It must drop the entry BEFORE rediscovery, or rediscovery skips it.
    ok(r_out.index("_z13_dead") < r_out.index("self.devices, updated ="),
       "the dead entry is dropped before get_aura_devices runs")

# The patcher must report it, or a silent failure looks like success.
ok(m.MARK_AURA == "z13-aura-wake",
   f"the mark is its own, not reused from another entry ({m.MARK_AURA})")

print()
print("  RESULT: " + ("ALL PASS" if not fail else f"{fail} FAILURE(S)"))
raise SystemExit(1 if fail else 0)
