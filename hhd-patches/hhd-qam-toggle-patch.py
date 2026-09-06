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

DISABLE ADJUSTOR'S NATIVE CPU CONTROL (z13-cpu-disable-native)
    adjustor ships inside the hhd package and loads as a second plugin in the
    same process. It writes CPU boost, EPP and -- the dangerous one --
    scaling_min_freq/scaling_max_freq across every cpufreq policy. hhd-z13 owns
    all three, so two independent writers race the same sysfs files.

    The frequency writer is worse than a race. It sets max = cpuinfo_max_freq,
    so an hhd start while boost happens to be off re-bakes the nominal-clock
    ceiling (the "3000 MHz that comes back" bug). And hhd_z13's PSTATE_SETTLE_S
    gate does not restrain it -- that gates hhd_z13's own update(), not another
    plugin -- so it can land mid-teardown while an amd_pstate mode switch is
    still rebuilding the policies.

    Two files, because the supports_* flags are NOT sufficient on their own:
      drivers/gpu/__init__.py  supports_boost / supports_nonlinear /
                               supports_epp -> False, which also removes the
                               now-dead controls from the QAM via upstream's
                               existing else-branches.
      fuse/gpu.py              set_frequency_scaling() returns immediately.
                               Three call sites in the balanced/performance/
                               power arms are outside every supports_* guard,
                               so flags alone leave them live the moment
                               adjustor is taken off manual mode.

    --check verifies each edit by its own sentinel and reports PARTIAL, not
    PATCHED, when only some are present. That case is not hypothetical: boost
    and epp were once applied by hand while supports_nonlinear was missed, and
    a mark-only check showed green over a file still writing cpufreq.

IDEMPOTENT + REVERSIBLE
    Safe to re-run (that is how the pacman hook uses it). Keeps a .z13-orig
    backup of each file on first patch; --revert restores them. adjustor is
    part of the hhd package, so the hook's `Target = hhd` covers it too.

    python3 hhd-qam-toggle-patch.py            # apply (idempotent)
    python3 hhd-qam-toggle-patch.py --check    # report state, change nothing
    python3 hhd-qam-toggle-patch.py --revert   # restore originals
"""
import os, shutil, sys, glob

MARK = "z13-qam-toggle"
MARK_VT = "z13-vt-gate"
MARK_ONESHOT = "z13-oneshot"
MARK_CPU = "z13-cpu-disable-native"
MARK_AC = "z13-ac-backport"
# Deliberately NOT folded into MARK_CPU. That mark means "adjustor stops
# writing this hardware because hhd-z13 owns it now"; this one is the exact
# opposite -- adjustor keeps every PPT write and is handed MORE work, because
# v0.4.0 hands TDP ownership back to it rather than growing a second writer.
# Sharing the mark would make the patcher's own --check labels lie.
MARK_TDP = "z13-tdp-presets"


def find_overlay_dir():
    hits = sorted(glob.glob("/usr/lib/python3*/site-packages/hhd/plugins/overlay"))
    if not hits:
        sys.exit("ERROR: hhd overlay package not found (is hhd installed?)")
    return hits[-1]


def find_adjustor_gpu():
    """Locate adjustor's CPU/GPU driver.

    adjustor ships alongside hhd but as its own top-level package, so this is
    NOT under find_overlay_dir(). Returns a full file path, not a directory.
    """
    hits = sorted(
        glob.glob("/usr/lib/python3*/site-packages/adjustor/drivers/gpu/__init__.py")
    )
    if not hits:
        sys.exit("ERROR: adjustor gpu driver not found (is adjustor installed?)")
    return hits[-1]


def find_adjustor_fuse():
    """Locate the module that actually writes cpufreq. Full file path."""
    hits = sorted(glob.glob("/usr/lib/python3*/site-packages/adjustor/fuse/gpu.py"))
    if not hits:
        sys.exit("ERROR: adjustor/fuse/gpu.py not found (is adjustor installed?)")
    return hits[-1]


def find_adjustor_asus():
    """adjustor's ASUS TDP driver -- the only PPT writer we keep. Full path.

    Same package and the same pacman target as the gpu/fuse files above:
    adjustor ships inside the `hhd` package, which 99-z13-hhd-qam-toggle.hook
    already lists, so this file is re-patched on an hhd upgrade for free.
    """
    hits = sorted(
        glob.glob("/usr/lib/python3*/site-packages/adjustor/drivers/asus/__init__.py")
    )
    if not hits:
        sys.exit("ERROR: adjustor asus driver not found (is adjustor installed?)")
    return hits[-1]


def find_hhd_utils():
    """hhd/utils.py, which owns the AC-status read. Full file path."""
    hits = sorted(glob.glob("/usr/lib/python3*/site-packages/hhd/utils.py"))
    if not hits:
        sys.exit("ERROR: hhd/utils.py not found (is hhd installed?)")
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
        '                        # THREE states, because the two backends differ and\n'
        '                        # because LAST_STATUS can go stale:\n'
        '                        #\n'
        '                        #  1. nothing running -> OPEN. Without this the button\n'
        '                        #     DEADLOCKS. LAST_STATUS holds the last status the\n'
        '                        #     UI reported and is never reset when the overlay\n'
        '                        #     dies, so it reads "open" forever and every press\n'
        '                        #     becomes a no-op -- in BOTH sessions, since it is a\n'
        '                        #     module-level global.\n'
        '                        #\n'
        '                        #  2. gamescope -> trust LAST_STATUS. loop_manage_overlay\n'
        '                        #     parses "stat:<status>" from the UI, and the UI\n'
        '                        #     process stays RESIDENT while the QAM is hidden, so\n'
        '                        #     liveness says nothing about visibility. Send\n'
        '                        #     cmd:close -- do NOT terminate, that kills the whole\n'
        '                        #     injected overlay and hhd then has to relaunch it.\n'
        '                        #\n'
        '                        #  3. desktop -> loop_manage_desktop only handles "grab:"\n'
        '                        #     lines and NEVER parses stat:, so LAST_STATUS stays\n'
        '                        #     "closed" there. The process lifetime IS the window\n'
        '                        #     lifetime. Terminate rather than sending cmd:close,\n'
        '                        #     because OverlayService.update() calls\n'
        '                        #     _open_overlay()/_open_de() BEFORE writing the\n'
        '                        #     command, so a close races its own reopen (measured\n'
        '                        #     ~5s and 3 presses before the UI went away).\n'
        '                        #\n'
        '                        # is_gamescope_running() is the vt-gated version, so it\n'
        '                        # means "gamescope on the ACTIVE VT" -- exactly the\n'
        '                        # condition under which hhd chose the gamescope path.\n'
        '                        # started_de cannot be used: it is set True once and\n'
        '                        # never reset.\n'
        '                        from .base import LAST_STATUS\n'
        '                        _p = getattr(self.ovf, "proc", None)\n'
        '                        _alive = bool(_p is not None and _p.poll() is None)\n'
        '                        _gs = is_gamescope_running()\n'
        '                        if not _alive:\n'
        '                            _open = False\n'
        '                        elif _gs:\n'
        '                            _open = LAST_STATUS.get("v", "closed") != "closed"\n'
        '                        else:\n'
        '                            _open = True\n'
        '                        if not _open:\n'
        '                            cmd = "open_qam"\n'
        '                        elif _gs:\n'
        '                            cmd = "close"\n'
        '                        else:\n'
        '                            cmd = None\n'
        '                            try:\n'
        '                                if _p is not None and _p.poll() is None:\n'
        '                                    _p.terminate()\n'
        '                            except Exception:\n'
        '                                pass\n'
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


# Independent edits, all carrying MARK_CPU. Each is guarded by its OWN
# sentinel so a partially-applied file converges instead of reporting "already
# patched" and skipping the rest -- which is exactly how this landed the first
# time: boost and epp were done by hand and nonlinear was missed, leaving
# adjustor still writing cpufreq behind a green check.
#
#   (label, anchor line, text appended after it, sentinel proving it applied)
ADJUSTOR_GPU_PATCHES = (
    (
        "boost",
        "        self.supports_boost = status.cpu_boost is not None\n",
        """\
        # {mark}: hhd-z13 owns CPU boost. adjustor's native path is not
        # proven here -- every attempt logs "Failed to set CPU boost:" with
        # no usable error. Forcing False also deletes cpu_boost from the QAM
        # via the existing else-branch below, so the control does not appear
        # at all rather than appearing and doing nothing.
        self.supports_boost = False  # was: status.cpu_boost is not None
""",
        "self.supports_boost = False  # was:",
    ),
    (
        "nonlinear",
        "        self.supports_nonlinear = can_use_nonlinear()\n",
        """\
        # {mark}: the frequency floor/ceiling writer, and the dangerous one.
        # Left enabled it writes scaling_min_freq and scaling_max_freq across
        # every cpufreq policy on each apply:
        #   AGPU INFO Setting CPU frequency scaling to [0.625 GHz, 3.000 GHz]
        # Two problems. (a) adjustor is a separate plugin in the same hhd
        # process, so hhd_z13's PSTATE_SETTLE_S gate does not apply to it --
        # it can write mid-teardown while an amd_pstate mode switch is still
        # rebuilding the policies. (b) it sets max = cpuinfo_max_freq, which
        # re-bakes the nominal-clock ceiling on every hhd start whenever
        # boost happens to be off at that moment.
        self.supports_nonlinear = False  # was: can_use_nonlinear()
""",
        "self.supports_nonlinear = False  # was:",
    ),
    (
        "epp",
        "        self.supports_epp = status.epp_avail is not None\n",
        """\
        # {mark}: EPP writes DO work here, but hhd-z13 owns EPP now so that
        # two independent writers do not race the same sysfs file.
        self.supports_epp = False  # was: status.epp_avail is not None
""",
        "self.supports_epp = False  # was:",
    ),
)

# The supports_* flags above are not sufficient on their own. Three call sites
# in the balanced/performance/power preset arms call set_frequency_scaling()
# OUTSIDE every supports_* guard, so they survive the flags being false and
# would resume unguarded cpufreq writes the moment adjustor leaves manual mode.
# Neuter the single definition instead, where no call site can miss it.
ADJUSTOR_FUSE_PATCHES = (
    (
        "freq-writer",
        "def set_frequency_scaling(nonlinear: bool):\n",
        """\
    # {mark}: hard stop. hhd-z13 owns scaling_min_freq/scaling_max_freq
    # outright, and this function writes BOTH across every cpufreq policy
    # (64 values over 32 policies here). It is reachable from preset arms
    # that no supports_* flag gates, and adjustor is a separate plugin in
    # the same hhd process, so hhd_z13's PSTATE_SETTLE_S gate does not hold
    # it back -- it can land mid-teardown while an amd_pstate mode switch
    # is still rebuilding the policies.
    logger.debug("set_frequency_scaling: suppressed ({mark})")
    return
""",
        "set_frequency_scaling: suppressed",
    ),
)


# hhd-z13's TDP preset support, and an upstream clock bug it depends on.
#
# THE PRESETS THEMSELVES NEED NO PATCH.
#   An earlier version of this table widened adjustor's mode dispatch so extra
#   dropdown entries reached its custom apply path. That was dropped. Presets
#   are now stored VALUE SETS: selecting one writes adjustor's own
#   tdp.asus.tdp_v2.custom.* keys and selects Custom -- the identical path a
#   user takes by hand.
#
#   Reusing the stock path instead of widening it is what makes this robust.
#   There is no new mode for KDE to eject: unplugging makes KDE change its
#   power profile, which adjustor maps onto a stock mode and FORCE-writes over
#   the config. Against a preset mode that silently replaced the user's
#   choice. Against Custom it only flips the visible LABEL to Silent, because
#   the quiet branch writes a fan curve and never touches PPT -- so the
#   wattage survives, which is the behaviour stock Custom has always had.
#
# ENTRY 1 -- per-preset PL2/PL3.
#   hhd-z13 computes all three power limits itself, from this machine's own
#   firmware figures, and needs somewhere to write them.
#
#   Those figures, read from
#   /sys/class/firmware-attributes/asus-armoury/attributes/:
#
#       attribute       default_value   max_value
#       ppt_pl1_spl          60             80
#       ppt_pl2_sppt         75             92
#       ppt_pl3_fppt         86             93
#
#   So the firmware's own shipped relationship between the tiers is
#   60 / 75 / 86 W -- PL2 at 1.25x the sustained limit, PL3 at 1.433x -- and
#   hhd-z13 reproduces those proportions at whatever wattage a preset asks
#   for, capped at 92 / 93 W.
#
#   Upstream's own derivation is built around a fixed 35 W ceiling. This
#   machine sustains up to 65 W, so from 35 W upward that ceiling sits below
#   the sustained value itself and
#   min(max(steady, ceiling), steady * ratio) collapses to steady -- all three
#   limits equal and the boost flag inert, across most of the range this
#   machine actually runs in:
#
#       PL1        upstream       from this panel's figures
#        9 W  ->   10 / 12         11 / 13
#       20 W  ->   24 / 28         25 / 29
#       45 W  ->   45 / 45         56 / 64
#       65 W  ->   65 / 65         81 / 93
#
#   A preset publishes its three values on two config keys; both non-zero
#   selects them. Absent or zero, upstream behaviour is untouched -- which is
#   what the stock Custom mode always gets, since it never writes them.
#
# ENTRIES 2-3 -- an upstream bug, and a hard prerequisite for retiring
# z13-hibernate's tdp-restore.sh.
#   update() computes `curr = time.perf_counter()` and drains the debounce
#   with `self.queue_tdp < curr`, but notify() arms it with `time.time()`.
#   perf_counter is seconds since boot (~8e3 here); time() is the Unix epoch
#   (~1.79e9). An epoch deadline is never less than curr, so the resume and
#   AC/DC re-applies have NEVER fired -- they log their intent and stop.
#   Verified in the journal: "Power adapter status switched to 'ac',
#   resetting TDP." with no write after it.
#
#   It compounds: queue_tdp then stays truthy forever, and the AC/DC arm is
#   guarded on `not self.queue_tdp`, so that stops arming too until some
#   user-driven change in update() overwrites it with a perf_counter value.
#
#   This is the strongest upstreaming candidate found so far -- two characters
#   each, affecting every ASUS device adjustor supports, with a symptom users
#   report as "TDP doesn't come back after sleep".
ADJUSTOR_ASUS_PATCHES = (
    (
        "ppt-override",
        "                self.queue_tdp = None\n                if boost:\n",
        """\
                self.queue_tdp = None
                # {mark}: the three power limits, computed by hhd-z13 from
                # this machine's own firmware figures (60/75/86 W shipped,
                # 92/93 W maxima) rather than from the derived pair below,
                # whose fixed 35 W ceiling sits under this machine's sustained
                # range and makes the boost flag inert from 35 W upward.
                #
                # Read from a file rather than the config tree because these
                # are derived values, not controls: anything declared in the
                # settings tree renders in the QAM, and a second editable copy
                # of a limit is a second source of truth. hhd-z13 publishes
                # them here and shows the LIVE values back as read-only
                # telemetry instead.
                #
                # Both must be non-zero to take effect, so the stock Custom
                # mode -- with no preset applied and no file -- keeps upstream
                # behaviour exactly. Ordering (PL1 <= PL2 <= PL3) is enforced
                # by the producer, because the firmware rejects a lower tier
                # that exceeds a higher one rather than clamping it.
                try:
                    import json as _z13_json
                    with open("/etc/hhd/z13-tdp-presets.json") as _z13_fd:
                        _z13_a = _z13_json.load(_z13_fd).get("applied") or {{}}
                    _z13_s = int(_z13_a.get("sppt", 0))
                    _z13_f = int(_z13_a.get("fppt", 0))
                except Exception:
                    _z13_s = _z13_f = 0
                if _z13_s and _z13_f:
                    time.sleep(TDP_DELAY)
                    set_tdp("fast", FTDP_FN, _z13_f)
                    time.sleep(TDP_DELAY)
                    set_tdp("slow", STDP_FN, _z13_s)
                    time.sleep(TDP_DELAY)
                    set_tdp("steady", CTDP_FN, steady)
                elif boost:
""",
        '_z13_a = _z13_json.load(_z13_fd)',
        "replace",
    ),
    (
        "wakeup-clock",
        "                self.queue_tdp = time.time() + SLEEP_DELAY\n",
        """\
                # {mark}: perf_counter, not time(). update() drains this queue
                # with `self.queue_tdp < curr` where curr is
                # time.perf_counter() -- seconds since boot. time.time() is
                # the Unix epoch, so the deadline was never less than curr and
                # this re-apply has never fired.
                self.queue_tdp = time.perf_counter() + SLEEP_DELAY
""",
        "self.queue_tdp = time.perf_counter() + SLEEP_DELAY",
        "replace",
    ),
    (
        "acdc-clock",
        "                self.queue_tdp = time.time() + APPLY_DELAY\n",
        """\
                # {mark}: perf_counter, not time() -- same bug as the wakeup
                # arm above, same fix. This one also self-blocked: the guard
                # on this branch is `not self.queue_tdp`, and a never-draining
                # epoch deadline left it permanently truthy.
                self.queue_tdp = time.perf_counter() + APPLY_DELAY
""",
        "self.queue_tdp = time.perf_counter() + APPLY_DELAY",
        "replace",
    ),
)


# Backport of hhd's own AC-status fix, so this machine gets it on 4.1.12.
#
# 4.1.12 resolves the AC ADAPTER's `online` file (contents "0"/"1") but then
# compares its contents against "Discharging" -- a string that lives in
# BAT0/status, a different device's file. The comparison is never equal, so
# get_ac_status() returns True unconditionally and info["ac"] has been True on
# every machine since the function was introduced. Verified here: AC0/online
# reads 0 with the battery discharging while hhd logs "AC status is: True".
#
# Wrong from its first commit (2cb50e8, 2024-10-17) until d609ada (2026-08-07,
# "move ac/dc handling to main"), i.e. ~22 months, and v4.1.12 predates the
# fix. The replacement below is upstream's own implementation copied verbatim
# -- deliberately NOT a local variant -- so that upgrading to a release which
# contains d609ada is a clean no-op rather than a conflict, and so nothing
# here has to be rewritten if it is ever contributed back.
#
# hhd_z13 does NOT rely on this: _on_ac() reads sysfs itself and stays the
# authority until the fix is confirmed working. This only repairs the signal
# for anything else that might read it.
UTILS_PATCHES = (
    (
        "ac-status",
        # The WHOLE try/except is the anchor, not just the buggy line: d609ada
        # also added error logging to the except arm, and matching upstream
        # exactly is the entire point of doing this as a backport.
        '    try:\n'
        '        with open(fn) as f:\n'
        '            return f.read().strip() != "Discharging"\n'
        '    except Exception as e:\n'
        '        return None\n',
        """\
    # {mark}: upstream d609ada, verbatim. The replaced line compared this
    # file's contents against "Discharging", which belongs to BAT0/status --
    # a different device -- so it never matched and always returned True.
    try:
        with open(fn) as f:
            status = f.read().strip()
        if status == "1":
            return True
        if status == "0":
            return False
        logger.error(f"Invalid AC status '{{status}}' read from '{{fn}}'.")
        return None
    except Exception as e:
        logger.error(f"Could not read AC status file '{{fn}}', error:\\n{{e}}")
        return None
""",
        "z13-ac-backport",
        "replace",
    ),
)


def apply_marked(p, table, mark=None):
    """Apply a table of independently-sentinelled edits to one file.

    Entries are (label, anchor, repl, sentinel) which INSERTS repl after the
    anchor, or (label, anchor, repl, sentinel, "replace") which substitutes
    repl FOR the anchor. Insert is the default because most of these disable a
    line by adding an override below it, where the original stays as a record
    of what upstream does. Replace exists for the cases where leaving the
    original in place would still execute it -- a `return` being the obvious
    one, since anything appended after it is unreachable.
    """
    s = read(p)
    applied, already = [], []
    for entry in table:
        label, anchor, repl, sentinel = entry[:4]
        mode = entry[4] if len(entry) > 4 else "insert"
        if sentinel in s:
            already.append(label)
            continue
        if anchor not in s:
            return None, f"anchor missing for {label} (upstream internals changed)"
        body = repl.format(mark=mark or MARK_CPU)
        s = s.replace(anchor, body if mode == "replace" else anchor + body, 1)
        applied.append(label)
    if not applied:
        return False, f"already patched ({', '.join(already)})"
    write(p, s)
    msg = "patched " + ", ".join(applied)
    if already:
        msg += f" (already: {', '.join(already)})"
    return True, msg


def marked_state(p, table):
    """Report PATCHED / PARTIAL / unpatched for --check.

    PARTIAL is called out loudly on purpose: a half-patched file still writes
    cpufreq, and a mark-only check would have shown it as done.
    """
    s = read(p)
    # entry[:4] because entries may carry a 5th "replace" mode field
    missing = [e[0] for e in table if e[3] not in s]
    if not missing:
        return "PATCHED"
    if len(missing) == len(table):
        return "unpatched"
    return f"PARTIAL (missing: {', '.join(missing)})"


def revert(d, adj):
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

    # adjustor is a different package, so the loop above does not reach it
    for a in adj:
        rel = os.path.relpath(a, os.path.dirname(os.path.dirname(os.path.dirname(a))))
        b = a + ".z13-orig"
        if os.path.exists(b):
            shutil.copy2(b, a)
            os.unlink(b)
            print(f"  reverted {rel}")
            n += 1
        else:
            print(f"  no backup for {rel}")

    for pkg in (d, *(os.path.dirname(a) for a in adj)):
        for c in glob.glob(os.path.join(pkg, "__pycache__", "*.pyc")):
            os.unlink(c)
    return n


def main():
    d = find_overlay_dir()
    a_gpu = find_adjustor_gpu()
    a_fuse = find_adjustor_fuse()
    a_utils = find_hhd_utils()
    a_asus = find_adjustor_asus()
    # Membership here is what gives a file backup(), revert() and the
    # pycache purge for free -- all three already iterate this tuple.
    adj = (a_gpu, a_fuse, a_utils, a_asus)
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
        # Deliberately NOT a plain `MARK_CPU in s` test: the mark was present
        # for boost and epp while supports_nonlinear was still live, i.e. a
        # green check over a file that had not stopped writing cpufreq.
        for disp, path, table in (
            ("adjustor/gpu", a_gpu, ADJUSTOR_GPU_PATCHES),
            ("adjustor/fuse", a_fuse, ADJUSTOR_FUSE_PATCHES),
        ):
            print(f"  {disp:14s} {'cpu-native':12s} {marked_state(path, table)}")
        print(f"  {'hhd/utils.py':14s} {'ac-backport':12s} {marked_state(a_utils, UTILS_PATCHES)}")
        # Deliberately not folded into the loop above: that loop hardcodes
        # the label 'cpu-native', and this table is a different mark.
        print(f"  {'adjustor/asus':14s} {'tdp-presets':12s} {marked_state(a_asus, ADJUSTOR_ASUS_PATCHES)}")
        return

    if mode == "--revert":
        revert(d, adj)
        return

    if os.geteuid() != 0:
        sys.exit("ERROR: needs root to modify the installed hhd package")

    for fn in ("base.py", "__init__.py", "x11.py"):
        backup(os.path.join(d, fn))
    for a in adj:
        backup(a)

    results = []
    for name, fn, arg in (
        ("base.py/toggle", patch_base, d),
        ("base.py/oneshot", patch_base_oneshot, d),
        ("__init__.py", patch_init, d),
        ("x11.py/vt-gate", patch_x11, d),
        ("adjustor/gpu", lambda p: apply_marked(p, ADJUSTOR_GPU_PATCHES), a_gpu),
        ("adjustor/fuse", lambda p: apply_marked(p, ADJUSTOR_FUSE_PATCHES), a_fuse),
        ("hhd/utils.py", lambda p: apply_marked(p, UTILS_PATCHES, MARK_AC), a_utils),
        ("adjustor/asus", lambda p: apply_marked(p, ADJUSTOR_ASUS_PATCHES, MARK_TDP), a_asus),
    ):
        ok, msg = fn(arg)
        results.append((name, ok, msg))
        print(f"  {name:16s} {msg}")
        if ok is None:
            sys.exit(f"ERROR: {name}: {msg}. Not patching further; hhd left usable.")

    # drop stale bytecode so the edits take effect
    for pkg in (d, *(os.path.dirname(a) for a in adj)):
        for c in glob.glob(os.path.join(pkg, "__pycache__", "*.pyc")):
            os.unlink(c)
    if any(ok for _, ok, _ in results):
        print("  -> restart hhd:  sudo systemctl restart hhd@$USER")


if __name__ == "__main__":
    main()
