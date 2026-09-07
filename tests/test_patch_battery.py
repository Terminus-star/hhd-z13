"""Tests for the battery wake-recharge patch entry.

Run: python3 tests/test_patch_battery.py   (no root, no writes)

Dry-runs the substitution against the REAL installed battery driver (or its
pristine .z13-orig backup) and throws the result away. Nothing is written.

WHAT THIS GUARDS
    charge_control_end_threshold was set to 80%, and after a HIBERNATE the
    battery charged past it.

    adjustor's battery driver writes the threshold in exactly two situations:
    once at open() (process startup), and whenever the CONFIGURED value
    changes. Neither fires on a resume -- hhd is not restarted by a
    hibernate, so open() does not run again, and the config value itself does
    not change across a hibernate, so the change-detection branch sees no
    difference and does nothing.

    This machine's hibernate mode is `shutdown` (a real power-off), which
    power-cycles the EC. If that resets the threshold, the driver has no way
    to notice: its cached value still matches the config, so as far as it
    knows nothing needs correcting.

    The fix adds a notify() that drops the cached value on the wakeup event,
    so update()'s own existing debounced apply path (unchanged) re-writes it.
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
sys.argv = ["x", "--check"]
spec.loader.exec_module(m)

try:
    path = m.find_adjustor_battery()
except SystemExit:
    print("  SKIP  adjustor battery driver not installed")
    raise SystemExit(0)

src = open(path).read()
label, anchor, body, sentinel, mode = m.ADJUSTOR_BATTERY_PATCHES[0]

# Always exercise the table against a PRISTINE driver -- never against
# whatever happens to be installed, which may already carry this patch or an
# older body of it. See test_patch_aura.py for why this matters.
backup = path + ".z13-orig"
if os.path.exists(backup) and m.MARK_BATTERY not in open(backup).read():
    src = open(backup).read()
    print(f"  note: testing the table against {os.path.basename(backup)}")
elif m.MARK_BATTERY in src:
    print("  SKIP  driver is patched and no pristine backup exists")
    raise SystemExit(0)

ok(src.count(anchor) == 1,
   f"the anchor is unique in the pristine driver ({src.count(anchor)})")

out = src.replace(anchor, body.format(mark=m.MARK_BATTERY))
ok(out != src, "the substitution applies")
ok(m.MARK_BATTERY in out, "and leaves the mark, so --check can see it")
ok(sentinel in out, "and the sentinel apply_marked tests for")

try:
    ast.parse(out)
    ok(True, "the patched driver is valid Python")
except SyntaxError as e:
    ok(False, f"the patched driver is valid Python ({e})")

# notify() must precede update() as a distinct method, not merge into it.
i_notify = out.find("def notify(self, events):")
i_update = out.find("def update(self, conf: Config):")
ok(i_notify != -1 and i_update != -1 and i_notify < i_update,
   "notify() is a separate method, defined before update()")
ok(out.count("def notify(self, events):") == 1,
   "exactly one notify(), not a doubled body")
ok(out.count("def update(self, conf: Config):") == 1,
   "and update() itself is not duplicated")

# The mechanism: invalidate the cache, do not write hardware directly. The
# actual write stays inside update()'s existing debounced path, unchanged.
ok("self.charge_limit_prev = None" in out,
   "the cached charge limit is invalidated on wakeup")
ok("self.charge_bypass_prev = None" in out,
   "and the cached bypass value, for the same reason")
ok('set_charge_limit(' not in body,
   "notify() does not call the writer directly -- update() still owns writes")

# Re-applying must not double the body. The real guard is apply_marked's own
# sentinel check (it never re-applies an entry whose sentinel is already
# present), NOT string idempotence -- this patch's anchor text is deliberately
# repeated verbatim in its own body's tail (the untouched update() header), so
# a raw second .replace() would refind that anchor and double the method. Test
# apply_marked() itself, which is what actually runs in production.
import tempfile

with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as tf:
    tf.write(out)
    tmp_path = tf.name
try:
    ok_result, msg = m.apply_marked(tmp_path, m.ADJUSTOR_BATTERY_PATCHES, m.MARK_BATTERY)
    ok(ok_result is False and "already patched" in (msg or ""),
       f"apply_marked refuses a second application ({ok_result!r}, {msg!r})")
    ok(open(tmp_path).read() == out,
       "and leaves the already-patched file untouched")
finally:
    os.unlink(tmp_path)

ok(m.MARK_BATTERY == "z13-battery-wake",
   f"the mark is its own, not reused from another entry ({m.MARK_BATTERY})")

print()
print("  RESULT: " + ("ALL PASS" if not fail else f"{fail} FAILURE(S)"))
raise SystemExit(1 if fail else 0)
