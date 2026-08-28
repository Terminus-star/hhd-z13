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

SESSION-AWARE GAMESCOPE DETECTION (z13-vt-gate)
    hhd asks "does a gamescope process exist?" (lsof -c gamescope-wl) when it
    needs to ask "is the ACTIVE session gamescope?". A gamescope session does
    not end when you VT-switch away from it -- it stays logind-'online' and
    keeps /tmp/.X11-unix/X0 open. So inside a desktop session hhd took the
    gamescope X11 path, searched gamescope's X display for a UI window that
    hhd-ui-wayland had correctly created on the desktop's Wayland compositor,
    never found it, and killed + relaunched it every ~18s: the flashing
    overlay. This gates detection on the active VT.

ONE-SHOT AUTOMATIC LAUNCH (z13-oneshot)
    Upstream retries the automatic launch every OVERLAY_CHECK_INTERVAL forever,
    which is what turned the above into a loop. Now the first failed automatic
    attempt latches; an explicit QAM press retries exactly once, and leaving
    gamescope re-arms it.

IDEMPOTENT + REVERSIBLE
    Safe to re-run (that is how the pacman hook uses it). Keeps a .z13-orig
    backup of each file on first patch; --revert restores them.

    python3 hhd-qam-toggle-patch.py            # apply (idempotent)
    python3 hhd-qam-toggle-patch.py --check    # report state, change nothing
    python3 hhd-qam-toggle-patch.py --revert   # restore originals
"""
import os, shutil, sys, glob

MARK = "z13-qam-toggle"
MARK_VT = "z13-vt-gate"
MARK_ONESHOT = "z13-oneshot"


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
        '                        from .base import LAST_STATUS\n'
        '                        _p = getattr(self.ovf, "proc", None)\n'
        '                        _open = LAST_STATUS.get("v", "closed") != "closed"\n'
        '                        if not _open:\n'
        '                            _open = bool(_p is not None and _p.poll() is None)\n'
        '                        if _open:\n'
        '                              # Do NOT send cmd:close. OverlayService.update()\n'
        '                              # calls _open_overlay()/_open_de() BEFORE writing the\n'
        '                              # command, so a close races its own reopen: measured\n'
        '                              # ~5s and 3 presses before the UI actually went away.\n'
        '                              # Terminate instead. loop_manage_desktop sees\n'
        '                              # proc.poll() != None, returns, and its finally block\n'
        '                              # does kill()/wait() -- so hhd cleans up its own state\n'
        '                              # and the next press relaunches normally.\n'
        '                            cmd = None\n'
        '                            try:\n'
        '                                if _p is not None and _p.poll() is None:\n'
        '                                    _p.terminate()\n'
        '                            except Exception:\n'
        '                                pass\n'
        '                        else:\n'
        '                            cmd = "open_qam"\n'
    )
    s = s.replace(anchor, repl, 1)
    write(p, s)
    return True, "patched"


# --------------------------------------------------------------------------
# z13-vt-gate / z13-oneshot
#
# Anchors are byte-exact slices of upstream hhd. If any of them stops
# matching, the corresponding patch refuses rather than guessing -- hhd stays
# usable and the pacman hook prints the failure.
# --------------------------------------------------------------------------

A_GSD = (
    'def get_gamescope_displays():\n'
    '    """Returns X11 UNIX sockets from gamescope opened under /tmp"""\n'
    '    files = subprocess.run(["lsof", "-c", "gamescope-wl", "-Fn"], capture_output=True)\n'
    '    out = []\n'
    '    for ln in files.stdout.splitlines():\n'
    '        if len(ln) < 1:\n'
    '            continue\n'
    '        ln = ln[1:]\n'
    '\n'
    '        if not ln.startswith(X11_DIR):\n'
    '            continue\n'
    '\n'
    '        fn = ln.split(b" ")[0]\n'
    '        disp = ":" + fn[len(X11_DIR) + 1 :].decode()\n'
    '        out.append((disp, os.stat(fn).st_uid))\n'
    '    return out\n'
)

NEW_GSD = (
    'def _z13_active_vt():\n'
    '    """Foreground VT number, or None when it cannot be determined."""\n'
    '    try:\n'
    '        with open("/sys/class/tty/tty0/active") as f:\n'
    '            v = f.read().strip()\n'
    '        if v.startswith("tty") and v[3:].isdigit():\n'
    '            return int(v[3:])\n'
    '    except Exception:\n'
    '        pass\n'
    '    return None\n'
    '\n'
    '\n'
    'def _z13_pid_vt(pid):\n'
    '    """VT a process was started on, read from XDG_VTNR in its environ."""\n'
    '    try:\n'
    '        with open("/proc/%s/environ" % pid, "rb") as f:\n'
    '            for kv in f.read().split(b"\\0"):\n'
    '                if kv.startswith(b"XDG_VTNR="):\n'
    '                    return int(kv[len(b"XDG_VTNR=") :])\n'
    '    except Exception:\n'
    '        pass\n'
    '    return None\n'
    '\n'
    '\n'
    'def _z13_gamescope_is_foreground(pid):\n'
    '    """z13-vt-gate: False only when gamescope is PROVABLY on a background VT.\n'
    '\n'
    '    A gamescope session does not end when you switch away from it. It stays\n'
    "    logind-'online' on its own VT and keeps /tmp/.X11-unix/X0 open, so the\n"
    '    upstream question -- "does a gamescope process exist?" -- keeps answering\n'
    '    yes from inside a desktop session. hhd then took the gamescope X11 path,\n'
    "    searched gamescope's X display for a UI window that the wrapper had\n"
    "    correctly created on the desktop's Wayland compositor, never found it, and\n"
    '    killed + relaunched it roughly every 18 seconds.\n'
    '\n'
    '    Fails OPEN on purpose: when either VT number is unreadable we keep\n'
    '    upstream behaviour, because a broken overlay inside gamescope is worse\n'
    '    than losing the desktop fallback.\n'
    '    """\n'
    '    active = _z13_active_vt()\n'
    '    if active is None:\n'
    '        return True\n'
    '    vt = _z13_pid_vt(pid)\n'
    '    if vt is None:\n'
    '        return True\n'
    '    return vt == active\n'
    '\n'
    '\n'
    'def get_gamescope_displays():\n'
    '    """Returns X11 UNIX sockets from gamescope opened under /tmp\n'
    '\n'
    '    z13-vt-gate: restricted to gamescope instances on the *active* VT.\n'
    '    """\n'
    '    files = subprocess.run(["lsof", "-c", "gamescope-wl", "-Fpn"], capture_output=True)\n'
    '    out = []\n'
    '    pid = None\n'
    '    for ln in files.stdout.splitlines():\n'
    '        if len(ln) < 1:\n'
    '            continue\n'
    '        tag, ln = ln[:1], ln[1:]\n'
    '\n'
    "        # -Fpn emits 'p<pid>' once, then the 'n<name>' rows for that process.\n"
    '        if tag == b"p":\n'
    '            pid = ln.decode(errors="replace").strip()\n'
    '            continue\n'
    '\n'
    '        if not ln.startswith(X11_DIR):\n'
    '            continue\n'
    '\n'
    '        if pid is not None and not _z13_gamescope_is_foreground(pid):\n'
    '            continue\n'
    '\n'
    '        fn = ln.split(b" ")[0]\n'
    '        disp = ":" + fn[len(X11_DIR) + 1 :].decode()\n'
    '        out.append((disp, os.stat(fn).st_uid))\n'
    '    return out\n'
)

A_LAUNCH = (
    '    def launch_overlay(self):\n'
    '        if not self.installed:\n'
    '            return\n'
    '        curr = time.perf_counter()\n'
    '        if self.last_check and curr - self.last_check < OVERLAY_CHECK_INTERVAL:\n'
    '            return\n'
    '        self.last_check = curr\n'
    '\n'
    '        launched = self._open_overlay(requested=False)\n'
    '        if launched and not self.is_healthy():\n'
    '            self.started = False\n'
)

NEW_LAUNCH = (
    '    def launch_overlay(self):\n'
    '        if not self.installed:\n'
    '            return\n'
    '        # z13-oneshot: stop the endless relaunch cycle.\n'
    '        # Upstream retries the automatic launch every OVERLAY_CHECK_INTERVAL\n'
    '        # seconds forever, so a launch that *cannot* succeed becomes a\n'
    '        # spawn/kill loop (the flashing overlay). Latch after the first\n'
    '        # failed automatic attempt. An explicit request via update() clears\n'
    '        # the latch and retries once; leaving gamescope re-arms it.\n'
    '        if getattr(self, "z13_auto_failed", False):\n'
    '            return\n'
    '        curr = time.perf_counter()\n'
    '        if self.last_check and curr - self.last_check < OVERLAY_CHECK_INTERVAL:\n'
    '            return\n'
    '        self.last_check = curr\n'
    '\n'
    '        launched = self._open_overlay(requested=False)\n'
    '        if launched and not self.is_healthy():\n'
    '            self.started = False\n'
    '            self.z13_auto_failed = True\n'
    '            logger.error(\n'
    '                "Overlay did not come up; not retrying automatically. "\n'
    '                "Press the QAM button to try again."\n'
    '            )\n'
)

A_DISPLAYS = (
    '        displays = get_gamescope_displays()\n'
    '        if not displays:\n'
    '            if requested:\n'
)

NEW_DISPLAYS = (
    '        displays = get_gamescope_displays()\n'
    '        if not displays:\n'
    '            # z13-oneshot: no gamescope on the active VT -- re-arm the\n'
    '            # automatic launcher so entering gamescope works again.\n'
    '            self.z13_auto_failed = False\n'
    '            if requested:\n'
)

A_UPDATE = (
    '        try:\n'
    '            ret = self._open_overlay()\n'
    '            if not ret:\n'
    '                self._open_de()\n'
)

NEW_UPDATE = (
    '        try:\n'
    '            # z13-oneshot: an explicit user request clears the latch, so\n'
    '            # the button always gets one fresh attempt.\n'
    '            self.z13_auto_failed = False\n'
    '            ret = self._open_overlay()\n'
    '            if not ret:\n'
    '                self._open_de()\n'
)


def patch_x11(d):
    """Gate gamescope detection on the active VT."""
    p = os.path.join(d, "x11.py")
    s = read(p)
    if MARK_VT in s:
        return False, "already patched"
    if A_GSD not in s:
        return None, "anchor missing in x11.py (hhd internals changed)"
    write(p, s.replace(A_GSD, NEW_GSD, 1))
    return True, "patched"


def patch_base_oneshot(d):
    """Make the automatic overlay launch one-shot instead of an endless retry."""
    p = os.path.join(d, "base.py")
    s = read(p)
    if MARK_ONESHOT in s:
        return False, "already patched"
    for name, a in (
        ("launch_overlay", A_LAUNCH),
        ("_open_overlay", A_DISPLAYS),
        ("update", A_UPDATE),
    ):
        if a not in s:
            return None, f"anchor missing in base.py:{name} (hhd internals changed)"
    s = s.replace(A_LAUNCH, NEW_LAUNCH, 1)
    s = s.replace(A_DISPLAYS, NEW_DISPLAYS, 1)
    s = s.replace(A_UPDATE, NEW_UPDATE, 1)
    write(p, s)
    return True, "patched"


def revert(d):
    n = 0
    for fn in ("base.py", "__init__.py", "x11.py"):
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
        for fn, mark, label in (
            ("base.py", MARK, "qam-toggle"),
            ("base.py", MARK_ONESHOT, "oneshot"),
            ("__init__.py", MARK, "qam-toggle"),
            ("x11.py", MARK_VT, "vt-gate"),
        ):
            s = read(os.path.join(d, fn))
            print(f"  {fn:14s} {label:12s} {'PATCHED' if mark in s else 'unpatched'}")
        return

    if mode == "--revert":
        revert(d)
        return

    if os.geteuid() != 0:
        sys.exit("ERROR: needs root to modify the installed hhd package")

    for fn in ("base.py", "__init__.py", "x11.py"):
        backup(os.path.join(d, fn))

    results = []
    for name, fn in (
        ("base.py/toggle", patch_base),
        ("base.py/oneshot", patch_base_oneshot),
        ("__init__.py", patch_init),
        ("x11.py/vt-gate", patch_x11),
    ):
        ok, msg = fn(d)
        results.append((name, ok, msg))
        print(f"  {name:16s} {msg}")
        if ok is None:
            sys.exit(f"ERROR: {name}: {msg}. Not patching further; hhd left usable.")

    # drop stale bytecode so the edits take effect
    for c in glob.glob(os.path.join(d, "__pycache__", "*.pyc")):
        os.unlink(c)
    if any(ok for _, ok, _ in results):
        print("  -> restart hhd:  sudo systemctl restart hhd@$USER")


if __name__ == "__main__":
    main()
