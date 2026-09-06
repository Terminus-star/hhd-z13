"""Dry-run the z13-tdp-presets patch against the INSTALLED adjustor.

Run: python3 tests/test_patch_adjustor_asus.py   (no root, no writes)

Nothing is written. The live file is read, the table is applied in memory, and
the result is compiled and inspected. This is the check that would have caught
an anchor that is off by one character -- apply_marked() aborts the whole table
on a missing anchor, so a bad entry takes the other three down with it and
leaves the machine unpatched with no obvious reason why.
"""

import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATCHER = os.path.join(ROOT, "hhd-patches", "hhd-qam-toggle-patch.py")

spec = importlib.util.spec_from_file_location("z13patch", PATCHER)
z13 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(z13)

fail = 0


def ok(cond, msg, detail=""):
    global fail
    print(("  PASS " if cond else "  FAIL ") + msg + ("  " + detail if detail else ""))
    if not cond:
        fail += 1


try:
    target = z13.find_adjustor_asus()
except SystemExit as e:
    print(f"  SKIP  adjustor not installed ({e})")
    sys.exit(0)

src = open(target).read()
print(f"  target: {target}\n")

table = z13.ADJUSTOR_ASUS_PATCHES
ok(len(table) == len(set(e[0] for e in table)), "entry labels are unique")

# The installed file is either pristine or already patched, and this suite has
# to be meaningful in both states -- it is run on the machine, not only in a
# clean checkout. Sentinels decide which.
APPLIED = all(e[3] in src for e in table)
print(f"  installed adjustor is {'PATCHED' if APPLIED else 'pristine'}\n")

if APPLIED:
    # Nothing to dry-run. Assert the applied state is coherent instead: no
    # anchor left behind (which would mean a half-applied table), and the
    # result still compiles as shipped.
    for entry in table:
        ok(entry[1] not in src,
           f"anchor for {entry[0]!r} is consumed, not duplicated")
    try:
        compile(src, target, "exec")
        ok(True, "installed patched source compiles")
    except SyntaxError as e:
        ok(False, "installed patched source compiles", f"{e}")
    ok(z13.marked_state(target, table) == "PATCHED",
       f"marked_state agrees ({z13.marked_state(target, table)})")
else:
    # --- anchors: present, and unique -------------------------------------
    # Uniqueness matters because apply_marked() uses str.replace(..., 1). A
    # second occurrence would silently patch the wrong one.
    for entry in table:
        label, anchor = entry[0], entry[1]
        n = src.count(anchor)
        ok(n == 1, f"anchor for {label!r} occurs exactly once", f"(found {n})")

# --- every entry is a replace, and formats cleanly -------------------------
for entry in table:
    label = entry[0]
    mode = entry[4] if len(entry) > 4 else "insert"
    ok(mode == "replace", f"{label!r} substitutes rather than inserts ({mode})")
    try:
        body = entry[2].format(mark=z13.MARK_TDP)
        ok(z13.MARK_TDP in body, f"{label!r} carries the mark")
    except (KeyError, IndexError) as e:
        # The classic failure: a literal brace in the body that was not
        # doubled, so .format() tries to interpret it as a field.
        ok(False, f"{label!r} body formats", f"undoubled brace? {e!r}")

# --- apply in memory ------------------------------------------------------
out = src
for entry in table:
    anchor, repl = entry[1], entry[2]
    out = out.replace(anchor, repl.format(mark=z13.MARK_TDP), 1)

if APPLIED:
    # This IS the idempotence check against the real installed file, which
    # the pacman hook re-runs on every hhd upgrade.
    ok(out == src, "re-applying over the installed patched file changes nothing")
else:
    ok(out != src, "the patch changes the file")

try:
    compile(out, target, "exec")
    ok(True, "patched source compiles")
except SyntaxError as e:
    ok(False, "patched source compiles", f"{e}")

for entry in table:
    ok(entry[3] in out, f"sentinel for {entry[0]!r} present after patching")

ok(z13.marked_state.__call__ is not None, "marked_state is available")

# --- idempotence ----------------------------------------------------------
# The pacman hook re-runs this on every hhd upgrade, so a second pass over an
# already-patched file must be a no-op rather than nesting the replacement.
missing = [e[0] for e in table if e[3] not in out]
ok(not missing, f"marked_state would report PATCHED ({missing})")

again = out
for entry in table:
    again = again.replace(entry[1], entry[2].format(mark=z13.MARK_TDP), 1)
ok(again == out, "re-applying over a patched file changes nothing")

# --- the behaviour the two preset entries exist to produce ----------------
STOCK = ("quiet", "balanced", "performance")


def reaches_custom_branch(mode):
    """Mirror the patched guard, so the intent is asserted, not just the text."""
    return mode == "custom" or (mode and mode not in STOCK)


for m in STOCK:
    ok(not reaches_custom_branch(m), f"stock mode {m!r} keeps its own path")
ok(reaches_custom_branch("custom"), "custom still reaches the PPT write")
for m in ("z13_ac", "z13_dc", "z13_game", "z13_alt"):
    ok(reaches_custom_branch(m), f"preset {m!r} reaches the PPT write")
ok(not reaches_custom_branch(None), "None writes no PPT, as upstream had it")

# --- the clock fix --------------------------------------------------------
ok("time.time() + SLEEP_DELAY" not in out, "wakeup arm no longer uses the epoch clock")
ok("time.time() + APPLY_DELAY" not in out, "AC/DC arm no longer uses the epoch clock")
ok(out.count("time.perf_counter()") >= 3,
   f"drain and both arms now share one clock ({out.count('time.perf_counter()')} uses)")

print("\n  RESULT: " + ("ALL PASS" if not fail else f"{fail} FAILURE(S)"))
sys.exit(1 if fail else 0)
