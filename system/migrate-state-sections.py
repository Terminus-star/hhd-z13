#!/usr/bin/env python3
"""Move hhd-z13's stored settings under the containers they now mount into.

    tdp.z13_cpu.*           ->  tdp.amd_energy.z13_cpu.*
    gamemode.z13_display.*  ->  gamemode.display.z13_display.*

Also handles the older orphan shape, if a machine skipped a step:

    z13_cpu.root.*          ->  tdp.amd_energy.z13_cpu.*
    z13_display.root.*      ->  gamemode.display.z13_display.*

WHY THIS IS NEEDED
    Config paths follow the mount point. The plugins now nest inside
    adjustor's "Processor Settings" container and hhd's own "Display"
    container, so stored values one level up are orphaned by the change --
    hhd finds nothing at the new paths and writes the YAML defaults over them.

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
BAK = STATE + ".z13-container"

# (key to move, destination section, destination container)
MOVES = [
    ("z13_cpu", "tdp", "amd_energy"),
    ("z13_display", "gamemode", "display"),
]


def block(lines, start, indent):
    """End index of the block opened at `start` by a key at `indent`."""
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        if len(ln) - len(ln.lstrip()) <= indent:
            return j
    return len(lines)


def find_key(lines, span, key, indent):
    """Index of `<indent>key:` within span, or None."""
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

    for key, section, container in MOVES:
        sec_at = find_key(lines, (0, len(lines)), section, 0)
        if sec_at is None:
            print(f"  ERROR: section '{section}:' not present, aborting")
            return 1
        sec_span = (sec_at, block(lines, sec_at, 0))

        # already nested?
        cont_at = find_key(lines, sec_span, container, 2)
        if cont_at is not None:
            cont_span = (cont_at, block(lines, cont_at, 2))
            if find_key(lines, cont_span, key, 4) is not None:
                print(f"  {section}.{container}.{key}: already migrated")
                continue

        # source: <section>.<key> at indent 2, or the old top-level orphan
        src_at = find_key(lines, sec_span, key, 2)
        if src_at is not None:
            src_span = (src_at, block(lines, src_at, 2))
            body = lines[src_at + 1 : src_span[1]]
        else:
            src_at = find_key(lines, (0, len(lines)), key, 0)
            if src_at is None:
                print(f"  {key}: nothing stored, skipping")
                continue
            src_span = (src_at, block(lines, src_at, 0))
            body = [l for l in lines[src_at + 1 : src_span[1]] if l.strip()]
            if not body or body[0].strip() != "root:":
                print(f"  ERROR: {key}: expected 'root:', aborting")
                return 1
            body = body[1:]

        # Children shift right by 2 to sit under a key that is itself one level
        # deeper. Getting this wrong is silent in both directions: too little
        # indent makes the children siblings of their own key (parses as
        # `<key>: null`, losing every value), too much buries them below where
        # the plugin reads.
        moved = ["    " + key + ":"] + ["  " + l if l.strip() else l for l in body]

        if cont_at is None:
            # hhd only writes containers that hold stored values, so the
            # destination may legitimately not exist yet -- create it.
            moved = ["  " + container + ":"] + moved
            anchor = sec_at
            print(f"  {section}.{key}  ->  {section}.{container}.{key}"
                  f"   (creating '{container}:', {len(body)} lines)")
        else:
            anchor = cont_at
            print(f"  {section}.{key}  ->  {section}.{container}.{key}"
                  f"   ({len(body)} lines)")

        drop.update(range(*src_span))
        inserts.setdefault(anchor, []).extend(moved)
        did.append(key)

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
