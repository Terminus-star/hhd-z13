"""Static checks on the hibernate shell stack.

Run: python3 tests/test_hibernate_shell.py   (no root, no writes, no hardware)

WHY THIS EXISTS
    The ramp rework moved a colour table out of stop_and_record_busy() into a
    shared Z13_RAMP, and renamed the loop index. One kmsg line was missed and
    kept referencing ${colors[$cidx]}.

    gate-hook.sh runs `set -euo pipefail`, and under `set -u` an unset variable
    is FATAL to the whole shell -- it is not caught by the `if ! ...` the gate
    wraps the call in. So a leftover reference in a log line aborted every
    hibernate before the first quiesce attempt finished, with no red LED, no
    console message and no fallback: the abort handling never ran either.

    A syntax check (bash -n) cannot see this, because the reference is only
    invalid at runtime. These are the cheapest checks that can.

The hibernate stack lives in a sibling repo, so every check skips cleanly when
it is not present.
"""

import os
import re
import sys

SRC = "/home/termis/Projects/z13-hibernate-plus/src"
FILES = ["common.sh", "gate-hook.sh", "hibernate-hook.sh"]

fail = 0


def ok(cond, msg, detail=""):
    global fail
    print(("  PASS " if cond else "  FAIL ") + msg + ("  " + detail if detail else ""))
    if not cond:
        fail += 1


if not os.path.isdir(SRC):
    print(f"  SKIP  hibernate stack not present at {SRC}")
    raise SystemExit(0)

texts = {}
for f in FILES:
    p = os.path.join(SRC, f)
    if not os.path.exists(p):
        print(f"  SKIP  {f} missing")
        continue
    texts[f] = open(p).read()

joined = "\n".join(texts.values())

# ── 1. every array reference names an array that is actually assigned ──────
# This is the exact shape of the bug: ${colors[$cidx]} outliving `colors`.
assigned = set(re.findall(r"^\s*(?:local\s+|declare\s+-a\s+)?([A-Za-z_][A-Za-z0-9_]*)=\(",
                          joined, re.M))
referenced = {}
for f, t in texts.items():
    for m in re.finditer(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\[", t):
        referenced.setdefault(m.group(1), set()).add(f)

unknown = {n: fs for n, fs in referenced.items() if n not in assigned}
# BASH_* and friends are shell builtins, not ours.
unknown = {n: fs for n, fs in unknown.items() if not n.startswith(("BASH", "FUNCNAME", "PIPESTATUS"))}
ok(not unknown,
   "every ${array[...]} reference names an array that is assigned",
   f"dangling: {unknown}" if unknown else "")

# ── 2. the ramp helpers the hooks call are defined in common.sh ────────────
defined = set(re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{", joined, re.M))
for fn in ("ramp_apply", "ramp_last", "ramp_count"):
    ok(fn in defined, f"{fn}() is defined")

for f in ("gate-hook.sh", "hibernate-hook.sh"):
    if f not in texts:
        continue
    called = set(re.findall(r"^\s*(ramp_[a-z_]+)\b", texts[f], re.M))
    missing = called - defined - {"ramp_advance"}   # defined locally in the hook
    ok(not missing, f"{f} calls only ramp helpers that exist",
       f"missing: {missing}" if missing else "")

# ── 3. the terminal index is reserved for the hook, not the gate ──────────
# The quiesce must stop one short of the terminal colour, or the gate would
# finish the journey while the hook still has four seconds of work to show.
if "common.sh" in texts:
    ok("ncolors=$(( $(ramp_count) - 1 ))" in texts["common.sh"],
       "the quiesce loop reserves the terminal colour for the hook")

# ── 4. the ramp table and its state path are single-sourced ───────────────
if "common.sh" in texts:
    ok(texts["common.sh"].count("Z13_RAMP=(") == 1,
       "exactly one ramp table exists")
    for f in ("gate-hook.sh", "hibernate-hook.sh"):
        if f in texts:
            ok("Z13_RAMP=(" not in texts[f],
               f"{f} does not carry its own copy of the table")

# ── 5. nothing writes the LED through the two-call helper pair on the ─────
#      hibernate critical path: led_color() sends `apply --color` with no
#      --brightness, and that flag DEFAULTS TO HIGH, so a led_brightness set
#      immediately before it is silently discarded.
if "hibernate-hook.sh" in texts:
    t = texts["hibernate-hook.sh"]
    pair = re.search(r"led_brightness\s+\w+\s*\n\s*led_color", t)
    ok(pair is None,
       "the hook does not set brightness before colour (colour would override it)")

# ── 6. the backlight is restored, not overwritten ─────────────────────────
# restore_screen used to write a hardcoded 40% of max on EVERY resume. The
# intent was rescuing a black panel, but it ran whether or not there was
# anything to rescue, so a deliberate 15-20% became 40% after every wake.
if "common.sh" in texts:
    t = texts["common.sh"]
    ok("snapshot_backlight()" in t, "snapshot_backlight() is defined")
    ok('[ "$cur" -gt 0 ] && continue' in t,
       "a surviving brightness is left alone")
    # The 40% fallback may remain, but only INSIDE the dark-panel branch.
    i_guard = t.find('[ "$cur" -gt 0 ] && continue')
    i_40 = t.find("* 40 / 100")
    ok(i_guard != -1 and i_40 != -1 and i_guard < i_40,
       "the 40% fallback sits behind the dark-panel guard")
    ok(t.count("* 40 / 100") == 1,
       f"and appears exactly once ({t.count('* 40 / 100')})")

for f, label in (("hibernate-hook.sh", "hibernate"),):
    if f in texts:
        ok("snapshot_backlight" in texts[f],
           f"the {label} path snapshots the backlight before sleeping")

print()
print("  RESULT: " + ("ALL PASS" if not fail else f"{fail} FAILURE(S)"))
raise SystemExit(1 if fail else 0)
