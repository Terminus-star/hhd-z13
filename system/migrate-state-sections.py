#!/usr/bin/env python3
"""Move hhd-z13's stored settings to wherever the plugins currently mount.

Target (current): the controls are FLATTENED into the parent container, so
stored values live directly beside the parent's own keys.

    tdp.amd_energy.z13_cpu.*          ->  tdp.amd_energy.*
    gamemode.display.z13_display.*    ->  gamemode.display.*

WHY THIS IS NEEDED
    Config paths follow the mount point. The wrapper container was removed
    because hhd only honours the hide-title tag at the FIRST level inside a
    section -- nested one deeper it is transmitted and ignored, so the QAM
    rendered "Processor Settings" and then "Z13 CPU" underneath it.

    Dropping the wrapper orphans everything stored under it. Most of those
    values are the "default" sentinel and would be harmless to lose, but not
    all: pstate.active.epp is stored as balance_performance while the schema
    default is balance_power, so without this hhd would write the default and
    change the live EPP on the next start.

WHY LINE-BASED RATHER THAN yaml.safe_load/dump
    state.yml carries a large auto-generated comment tree documenting every
    setting path. A YAML round-trip discards all of it. This moves the exact
    lines instead, so everything it does not touch is preserved byte for byte.

    ./migrate-state-sections.py            # dry run
    sudo ./migrate-state-sections.py --apply
"""
import shutil
import sys

STATE = "/etc/hhd/state.yml"
BAK = STATE + ".z13-preflatten"

# (wrapper key to dissolve, section, container it sits inside)
UNWRAP = [
    ("z13_cpu", "tdp", "amd_energy"),
    ("z13_display", "gamemode", "display"),
]


def indent_of(ln):
    return len(ln) - len(ln.lstrip())


def block_end(lines, start, indent):
    """First line after `start` that closes a block opened at `indent`."""
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        if indent_of(ln) <= indent:
            return j
    return len(lines)


def find_at(lines, span, key, indent):
    want = " " * indent + key + ":"
    for i in range(*span):
        if lines[i].rstrip() == want:
            return i
    return None


def main():
    apply = "--apply" in sys.argv
    with open(STATE) as f:
        lines = f.read().split("\n")

    drop, inserts, did = set(), {}, []

    for key, section, container in UNWRAP:
        sec = find_at(lines, (0, len(lines)), section, 0)
        if sec is None:
            print(f"  ERROR: section '{section}:' missing, aborting")
            return 1
        sec_span = (sec, block_end(lines, sec, 0))

        cont = find_at(lines, sec_span, container, 2)
        if cont is None:
            print(f"  {section}.{container}: not present, skipping {key}")
            continue
        cont_span = (cont, block_end(lines, cont, 2))

        wrap = find_at(lines, cont_span, key, 4)
        if wrap is None:
            print(f"  {section}.{container}.{key}: already flattened")
            continue
        wrap_span = (wrap, block_end(lines, wrap, 4))

        # Children sit at indent 6 under the wrapper and must end up at 4,
        # level with the container's own keys. Dedent by exactly 2 -- too
        # little and they stay inside a key that no longer exists, too much
        # and they escape the container entirely.
        body = [l for l in lines[wrap[0] if False else wrap + 1 : wrap_span[1]] if l.strip()]
        moved = [l[2:] if l.startswith("  ") else l for l in body]

        drop.update(range(*wrap_span))
        inserts.setdefault(cont, []).extend(moved)
        did.append(key)
        print(f"  {section}.{container}.{key}.*  ->  {section}.{container}.*"
              f"   ({len(moved)} lines)")

    if not did:
        print("  nothing to do")
        return 0

    out = []
    for i, ln in enumerate(lines):
        if i in drop:
            continue
        out.append(ln)
        if i in inserts:
            out.extend(inserts[i])

    if not apply:
        print("\n  DRY RUN. Re-run with --apply (as root) to write.")
        print(f"  result would be {len(out)} lines (was {len(lines)})")
        return 0

    shutil.copy2(STATE, BAK)
    with open(STATE, "w") as f:
        f.write("\n".join(out))
    print(f"\n  written. backup at {BAK}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
