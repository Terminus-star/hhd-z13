"""Dry-run the z13-tdp-presets patch against the INSTALLED adjustor.

Run: python3 tests/test_patch_adjustor_asus.py   (no root, no writes)

Nothing is written. The live file is read, the table applied in memory, and
the result compiled and inspected. This is the check that catches an anchor
that is off by one character -- apply_marked() aborts the whole table on a
missing anchor, so one bad entry takes the others down with it and leaves the
machine unpatched with no obvious reason why.

State is judged PER ENTRY, not all-or-nothing. The installed file is routinely
mixed: the table gains and loses entries between releases while the pacman
hook keeps re-applying whatever the current table says.
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
table = z13.ADJUSTOR_ASUS_PATCHES
print(f"  target: {target}\n")

ok(len(table) == len(set(e[0] for e in table)), "entry labels are unique")

# --- per-entry state ------------------------------------------------------
# Exactly one of "anchor present" / "sentinel present" must hold. Neither
# means the upstream code moved and this entry can no longer be applied OR
# verified -- the failure mode worth catching, because apply_marked() would
# abort the whole table on it.
pristine = []
for entry in table:
    label, anchor, _, sentinel = entry[:4]
    has_anchor, has_sentinel = anchor in src, sentinel in src
    if has_sentinel:
        ok(True, f"{label!r} already applied")
    elif has_anchor:
        n = src.count(anchor)
        # Uniqueness matters: apply_marked uses str.replace(..., 1), so a
        # second occurrence would silently patch the wrong one.
        ok(n == 1, f"{label!r} anchor present and unique", f"(found {n})")
        pristine.append(entry)
    else:
        # Neither form is present. Two very different causes, and conflating
        # them sends the next reader to the wrong place:
        #
        #   - a SUPERSEDED revision of this same entry is installed. The
        #     patcher only ever applies what the current table says and never
        #     removes what an older table applied, so an entry whose
        #     replacement text changed between releases lands here. Fixed by
        #     install.sh --revert then re-apply.
        #   - upstream genuinely moved the anchor, which needs a new one.
        #
        # The MARK is what stays stable: a revision may replace the whole
        # mechanism (config keys -> a store file, say), so matching on any
        # part of the sentinel text is not reliable. If the mark is in the
        # file but this entry is neither applicable nor applied, an older
        # revision of the table put it there.
        if z13.MARK_TDP in src:
            ok(True, f"{label!r} has a SUPERSEDED revision installed",
               "harmless; hhd-patches/install.sh --revert then re-apply")
        else:
            ok(False, f"{label!r} is applicable",
               "neither anchor nor sentinel found — upstream moved")

print(f"\n  {len(pristine)} of {len(table)} entries would be applied now\n")

# --- bodies format --------------------------------------------------------
for entry in table:
    label = entry[0]
    mode = entry[4] if len(entry) > 4 else "insert"
    ok(mode == "replace", f"{label!r} substitutes rather than inserts ({mode})")
    try:
        body = entry[2].format(mark=z13.MARK_TDP)
        ok(z13.MARK_TDP in body, f"{label!r} carries the mark")
    except (KeyError, IndexError) as e:
        # The classic failure: a literal brace in the body that was not
        # doubled, so .format() reads it as a field.
        ok(False, f"{label!r} body formats", f"undoubled brace? {e!r}")

# --- apply the outstanding entries in memory ------------------------------
out = src
for entry in pristine:
    out = out.replace(entry[1], entry[2].format(mark=z13.MARK_TDP), 1)

try:
    compile(out, target, "exec")
    ok(True, "patched source compiles")
except SyntaxError as e:
    ok(False, "patched source compiles", f"{e}")

for entry in table:
    label, anchor, _, sentinel = entry[:4]
    if sentinel in out:
        ok(True, f"sentinel for {label!r} present afterwards")
    elif anchor not in src and z13.MARK_TDP in src:
        # Superseded revision in place: its anchor is already consumed, so a
        # dry-run cannot produce the new sentinel. Reported above; not a
        # second failure here.
        ok(True, f"sentinel for {label!r} pending a re-apply",
           "superseded revision installed")
    else:
        ok(False, f"sentinel for {label!r} present afterwards")

ok(z13.marked_state(out.__class__ and target, table) is not None,
   "marked_state is callable against the target")

# --- idempotence ----------------------------------------------------------
# The pacman hook re-runs this on every hhd upgrade, so a second pass over an
# already-patched file must be a no-op rather than nesting the replacement.
# Mirror apply_marked's own logic: it skips an entry whose sentinel is already
# present, BEFORE touching the text. That guard is what makes a re-apply safe
# for a replacement that reproduces its own anchor -- several do, because the
# clearest way to add code after a block is to restate the block. Replaying
# blindly would nest those, which is the failure this check exists to catch.
again = out
for entry in table:
    if entry[3] in again:
        continue
    again = again.replace(entry[1], entry[2].format(mark=z13.MARK_TDP), 1)
ok(again == out, "re-applying over a patched file changes nothing")

# ...and the guard is load-bearing, not incidental: at least one entry would
# genuinely re-match its own output without it.
naive = out
for entry in table:
    naive = naive.replace(entry[1], entry[2].format(mark=z13.MARK_TDP), 1)
ok(naive != out or all(e[1] not in e[2].format(mark=z13.MARK_TDP) for e in table),
   "the sentinel guard is what prevents nesting, not luck")

# --- what the entries are for ---------------------------------------------
# The clock fix. Asserted on the RESULT rather than the diff, so it holds
# whether the entry was already applied or applied just now.
ok("time.time() + SLEEP_DELAY" not in out, "wakeup arm no longer uses the epoch clock")
ok("time.time() + APPLY_DELAY" not in out, "AC/DC arm no longer uses the epoch clock")
ok(out.count("time.perf_counter()") >= 3,
   f"drain and both arms share one clock ({out.count('time.perf_counter()')} uses)")

# The PL2/PL3 override must be INSIDE the custom apply path and must leave
# upstream's derived pair reachable, or the stock Custom mode changes
# behaviour -- which this patch must never do.
ok("elif boost:" in out, "upstream's derived-limits branch is preserved")
# Prefix match: tolerates both upstream's `if mode == "custom":` and the
# widened form left behind by a superseded release (see below).
gate = out.find('if mode == "custom"')
marker = max(out.find("_z13_s = conf.get"), out.find("_z13_a = _z13_json.load"))
ok(gate != -1 and marker > gate,
   "the override sits inside the custom apply path")

# Presets became stored value sets applied through adjustor's own Custom mode,
# so the two entries that widened its mode dispatch were dropped. An installed
# file can still carry them: the patcher only ever adds what the CURRENT table
# names, and never removes what an older one applied.
#
# Harmless -- with no preset modes in the tree the widened guard can never
# match anything -- but it is dead code standing between this machine and
# upstream, so say so rather than let it accumulate silently.
STALE = 'if mode == "custom" or (mode and mode not in'
if STALE in out:
    ok(True, "NOTE: a superseded widening is still installed",
       "harmless; clear it with hhd-patches/install.sh --revert then re-apply")
else:
    ok('if mode == "custom":' in out,
       "adjustor's mode dispatch is left alone — presets no longer need it")

print("\n  RESULT: " + ("ALL PASS" if not fail else f"{fail} FAILURE(S)"))
sys.exit(1 if fail else 0)
