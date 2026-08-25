#!/usr/bin/env python3
"""Make hhd's Armoury-button binding TOGGLE the QAM instead of only opening it.

WHY
    hhd maps `shortcuts.custom.armoury: hhd_qam` to an unconditional
    "open_qam". On a KDE Wayland desktop hhd has no layer-shell backend
    (plugins/overlay/x11.py probes for a *gamescope* display and otherwise
    falls back to launching hhd-ui as a plain XWayland Electron window), so a
    second press re-focuses/minimises the window rather than dismissing it.

    hhd already has everything needed: a "close" command and a Status the UI
    reports back as "stat:<status>" on stdout. It just never consults it for
    this binding. This patch records the last reported status and makes
    hhd_qam pick close-vs-open from it.

IDEMPOTENT + REVERSIBLE
    Safe to re-run (that is how the pacman hook uses it). Keeps a .z13-orig
    backup of each file on first patch; --revert restores them.

    python3 hhd-qam-toggle-patch.py            # apply (idempotent)
    python3 hhd-qam-toggle-patch.py --check    # report state, change nothing
    python3 hhd-qam-toggle-patch.py --revert   # restore originals
"""
import os, shutil, sys, glob

MARK = "z13-qam-toggle"


def find_overlay_dir():
    hits = sorted(glob.glob("/usr/lib/python3*/site-packages/hhd/plugins/overlay"))
    if not hits:
        sys.exit("ERROR: hhd overlay package not found (is hhd installed?)")
    return hits[-1]


def read(p):
    with open(p) as f:
        return f.read()


def write(p, s):
    # Syntax-check in memory. py_compile() was wrong here: it refuses cfile=
    # /dev/null ("non-regular file"), and its exception is not PyCompileError,
    # so it escaped the handler and leaked the temp file.
    try:
        compile(s, p, "exec")
    except SyntaxError as e:
        sys.exit(f"ERROR: patched {p} does not compile, aborting:\n{e}")
    tmp = p + ".z13-tmp"
    try:
        with open(tmp, "w") as f:
            f.write(s)
        shutil.copymode(p, tmp)
        os.replace(tmp, p)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def backup(p):
    b = p + ".z13-orig"
    if not os.path.exists(b):
        shutil.copy2(p, b)


def patch_base(d):
    p = os.path.join(d, "base.py")
    s = read(p)
    if MARK in s:
        return False, "already patched"

    anchor = '                    status = cast(Status, cmd[5:])\n'
    if anchor not in s:
        return None, "anchor missing in base.py (hhd internals changed)"

    # 1. module-level holder. A dict avoids needing `global` inside the loop.
    imp = s.index("\n\n", s.index("import"))
    s = (s[:imp] + f'\n\n# {MARK}: last status the UI reported, so the armoury\n'
         '# binding can decide between "close" and "open_qam".\n'
         'LAST_STATUS = {"v": "closed"}\n' + s[imp:])

    # 2. record it where the UI status is parsed
    s = s.replace(anchor, anchor + f'                    LAST_STATUS["v"] = status  # {MARK}\n', 1)
    write(p, s)
    return True, "patched"


def patch_init(d):
    p = os.path.join(d, "__init__.py")
    s = read(p)
    if MARK in s:
        return False, "already patched"

    anchor = '                    case "hhd_qam":\n                        cmd = "open_qam"\n'
    if anchor not in s:
        return None, "anchor missing in __init__.py (hhd internals changed)"

    repl = (
        '                    case "hhd_qam":\n'
        f'                        # {MARK}: toggle instead of open-only.\n'
        '                        # Two signals, because the two backends differ:\n'
        '                        #  * gamescope path (loop_manage_overlay) parses\n'
        '                        #    "stat:<status>" from the UI -> LAST_STATUS.\n'
        '                        #  * desktop path (loop_manage_desktop) only ever\n'
        '                        #    handles "grab:" lines and NEVER parses stat:,\n'
        '                        #    so LAST_STATUS is permanently "closed" there.\n'
        '                        #    Fall back to process liveness: on that path the\n'
        '                        #    hhd-ui process lifetime IS the window lifetime\n'
        '                        #    (the loop kills it in its finally: block).\n'
        '                        # DEBOUNCE. This button emits several events per\n'
        '                        # physical press (observed 5 gestures ~250ms apart),\n'
        '                        # and closing takes ~1s to complete, so an undebounced\n'
        '                        # toggle flip-flops into a net no-op. The same reason\n'
        '                        # z13ctl-plus carries a button-debounce branch.\n'
        '                        import time as _t\n'
        '                        _now = _t.monotonic()\n'
        '                        if _now - getattr(self, "_z13_qam_ts", 0.0) < 0.8:\n'
        '                            cmd = None\n'
        '                        else:\n'
        '                          self._z13_qam_ts = _now\n'
        '                          from .base import LAST_STATUS\n'
        '                          _open = LAST_STATUS.get("v", "closed") != "closed"\n'
        '                          if not _open:\n'
        '                              _p = getattr(self.ovf, "proc", None)\n'
        '                              _open = bool(_p is not None and _p.poll() is None)\n'
        '                          cmd = "close" if _open else "open_qam"\n'
    )
    s = s.replace(anchor, repl, 1)
    write(p, s)
    return True, "patched"


def revert(d):
    n = 0
    for fn in ("base.py", "__init__.py"):
        p = os.path.join(d, fn)
        b = p + ".z13-orig"
        if os.path.exists(b):
            shutil.copy2(b, p)
            os.unlink(b)
            print(f"  reverted {fn}")
            n += 1
        else:
            print(f"  no backup for {fn}")
    for c in glob.glob(os.path.join(d, "__pycache__", "*.pyc")):
        os.unlink(c)
    return n


def main():
    d = find_overlay_dir()
    mode = sys.argv[1] if len(sys.argv) > 1 else "--apply"

    if mode == "--check":
        for fn in ("base.py", "__init__.py"):
            s = read(os.path.join(d, fn))
            print(f"  {fn:14s} {'PATCHED' if MARK in s else 'unpatched'}")
        return

    if mode == "--revert":
        revert(d)
        return

    if os.geteuid() != 0:
        sys.exit("ERROR: needs root to modify the installed hhd package")

    for fn in ("base.py", "__init__.py"):
        backup(os.path.join(d, fn))

    results = []
    for name, fn in (("base.py", patch_base), ("__init__.py", patch_init)):
        ok, msg = fn(d)
        results.append((name, ok, msg))
        print(f"  {name:14s} {msg}")
        if ok is None:
            sys.exit(f"ERROR: {name}: {msg}. Not patching further; hhd left usable.")

    # drop stale bytecode so the edits take effect
    for c in glob.glob(os.path.join(d, "__pycache__", "*.pyc")):
        os.unlink(c)
    if any(ok for _, ok, _ in results):
        print("  -> restart hhd:  sudo systemctl restart hhd@$USER")


if __name__ == "__main__":
    main()
