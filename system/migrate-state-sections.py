#!/usr/bin/env python3
"""Move hhd-z13's stored settings under the sections they now mount into.

    z13_cpu.root.*      ->  tdp.z13_cpu.*
    z13_display.root.*  ->  gamemode.z13_display.*

WHY THIS IS NEEDED
    The plugins used to return {"z13_cpu": {"root": ...}}, making "z13_cpu" a
    top-level key. hhd's section list is fixed (hhd/sections.yml, ten keys), so
    that key had nowhere to be filed and rendered as an orphan section. The
    plugins now mount under "tdp" and "gamemode" like every other driver does.

    Config paths follow the mount point, so the stored values are orphaned by
    that change. Without this migration hhd finds nothing at the new paths and
    writes the YAML defaults instead -- and settings.yml defaults
    pstate.mode to "guided" while this machine has "active" stored. The result
    would be amd_pstate silently switching mode on the next hhd start: a live
    hardware change caused by a UI reorganisation.

WHY LINE-BASED RATHER THAN yaml.safe_load/dump
    state.yml carries a large auto-generated comment tree documenting every
    setting path. A YAML round-trip discards all of it. This moves the exact
    lines instead, so everything it does not touch is preserved byte for byte.

    ./migrate-state-sections.py            # dry run, prints the diff
    sudo ./migrate-state-sections.py --apply
"""
import shutil
import sys

STATE = "/etc/hhd/state.yml"
BAK = STATE + ".z13-presection"

MOVES = [("z13_cpu", "tdp"), ("z13_display", "gamemode")]


def top_level_blocks(lines):
    """-> {key: (start, end)} for every top-level `key:` line."""
    out, cur, start = {}, None, 0
    for i, ln in enumerate(lines):
        if ln and not ln[0].isspace() and ln.rstrip().endswith(":") and "#" not in ln.split(":")[0]:
            if cur:
                out[cur] = (start, i)
            cur, start = ln.split(":")[0], i
        elif ln.startswith("#") and cur:
            # the trailing comment tree ends the last block
            out[cur] = (start, i)
            cur = None
    if cur:
        out[cur] = (start, len(lines))
    return out


def main():
    apply = "--apply" in sys.argv
    with open(STATE) as f:
        lines = f.read().split("\n")

    blocks = top_level_blocks(lines)
    todo = [(src, dst) for src, dst in MOVES if src in blocks]
    if not todo:
        print("  nothing to migrate -- no orphan sections found")
        return 0
    for _, dst in todo:
        if dst not in blocks:
            print(f"  ERROR: destination section '{dst}:' not present, aborting")
            return 1

    # Build the moved payloads first, then splice, so indices stay valid.
    payload = {}
    for src, dst in todo:
        s, e = blocks[src]
        body = [l for l in lines[s + 1 : e] if l.strip()]
        if not body or body[0].strip() != "root:":
            print(f"  ERROR: {src}: does not start with 'root:', aborting")
            return 1
        # Only the KEY LINE moves; the children keep their indentation.
        #   z13_cpu:      (0)  ->    z13_cpu:   (2, under tdp:)
        #     root:       (2)  ->    [dropped]
        #       pstate:   (4)  ->      pstate:  (4, unchanged)
        # Dedenting the children would put them level with the new key and
        # make them siblings rather than its contents -- which parses as
        # `z13_cpu: null` and silently loses every stored value.
        moved = [f"  {src}:"] + body[1:]
        payload[src] = moved
        print(f"  {src}.root.*  ->  {dst}.{src}.*   ({len(moved) - 1} lines)")

    out = []
    for i, ln in enumerate(lines):
        skip = False
        for src, _ in todo:
            s, e = blocks[src]
            if s <= i < e:
                skip = True
        if skip:
            continue
        out.append(ln)
        for src, dst in todo:
            if i == blocks[dst][0]:
                out.extend(payload[src])

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
