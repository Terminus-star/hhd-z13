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

# Once the patch is installed the anchor is GONE -- that is success, not
# failure. So the two states are checked differently: an unpatched file must
# still accept the substitution, and a patched one is verified on its result.
# The earlier version of this test asserted the anchor unconditionally and
# started failing the moment the patch it tests was actually applied.
patched_already = m.MARK_AURA in src

if patched_already:
    print("  note: installed driver is already patched; verifying the result")
    ok(src.count(anchor) == 0,
       "the anchor is consumed once the patch is applied")
    out = src
else:
    ok(src.count(anchor) == 1,
       f"the anchor is unique in the unpatched driver ({src.count(anchor)})")
    out = src.replace(anchor, body.format(mark=m.MARK_AURA))
    ok(out != src, "the substitution applies")

    # Re-applying must not double the body. apply_marked skips on the
    # sentinel, but the table itself should be idempotent under replace().
    ok(out.replace(anchor, body.format(mark=m.MARK_AURA)) == out,
       "a second application changes nothing")
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

# init=True is the mechanism: it is what routes the device back through
# update()'s startup path, which rewrites the full per-device config.
ok('dev["init"] = True' in out, "devices are marked dirty via init")
ok('if not dev["disabled"]' in out,
   "and a disabled device is left alone rather than re-initialised")

# The patcher must report it, or a silent failure looks like success.
ok(m.MARK_AURA == "z13-aura-wake",
   f"the mark is its own, not reused from another entry ({m.MARK_AURA})")

print()
print("  RESULT: " + ("ALL PASS" if not fail else f"{fail} FAILURE(S)"))
raise SystemExit(1 if fail else 0)
