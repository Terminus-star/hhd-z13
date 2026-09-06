"""z13_display -- internal panel refresh rate (60/180 Hz) for the ASUS ROG
Flow Z13, via kscreen-doctor.

This is the one control the original z13ctl-plus/z13gui-plus->hhd migration
left with no equivalent (see hhd-patches/plan.md Part 1): hhd's own
display plugin has the hint "Brightness (and framerate TBD)" and is not
registered -- refresh rate is a genuine, unfinished upstream gap, not
something hhd already does differently.

WHY THIS RUNS AS THE DESKTOP USER, NOT ROOT
    kscreen-doctor talks to the KDE Wayland session's compositor (KWin) over
    its Wayland socket. hhd runs as root (systemd `ExecStart=hhd --user
    termis`) and has no session of its own to reach that socket from -- the
    exact problem hhd's own overlay code solves for launching its UI
    (hhd/plugins/overlay/overlay.py's launch_overlay_de(), which drops
    privileges via subprocess's user=/group= kwargs). This module copies
    that precedent: _run_as_user() drops to the uid/gid of a LIVE-discovered
    session (not a static config value), matching how hhd's own
    find_x11_display() discovers the real display owner at call time rather
    than trusting a fixed uid.

    NOT via /proc/<pid>/environ, though -- an earlier version of this file
    scanned for a kwin_wayland process and read its environ for
    WAYLAND_DISPLAY/XDG_VTNR, which turned out to be doubly wrong, confirmed
    live on this exact machine (2026-09-03): (1) reading another process's
    /proc/<pid>/environ needs CAP_SYS_PTRACE, which kernel.yama.ptrace_scope
    (=1 here, "restricted") gates even for the SAME uid between unrelated
    processes -- root has CAP_SYS_PTRACE and should bypass this, but the
    real, root-run daemon still reported "unavailable" in production, not
    just this non-root debugging session; and (2) more fundamentally,
    WAYLAND_DISPLAY/XDG_VTNR are not necessarily even IN kwin_wayland's own
    environment to begin with -- KWin *creates* the Wayland socket (visible
    on its cmdline as `--socket wayland-0`, confirmed against this
    machine's real process) rather than inheriting it as an env var the way
    a *client* of the compositor would. Both problems disappear by not
    reading environ at all: `loginctl show-session` gets the VT number
    (VTNr) and owning uid (User) straight from systemd-logind, which is
    read-only public session metadata, not a ptrace-gated resource; and
    `/proc/<pid>/cmdline` (unlike environ) carries no ptrace_scope
    restriction, so parsing kwin_wayland's `--socket` argument for
    WAYLAND_DISPLAY needs no elevated privilege at all -- confirmed by
    running this file's own discovery successfully as a plain, non-root
    user on this machine.

WHY "FOREGROUND VT" MATTERS
    A gamescope session does not end when you switch away from it -- it
    stays running on its own VT. So "is there a KDE Wayland session at all"
    is not enough to know kscreen-doctor will actually reach anything
    useful; if gamescope currently owns the foreground VT, KWin's Wayland
    socket exists but nothing is compositing to the physical panel through
    it, and kscreen-doctor calls would appear to succeed while doing
    nothing visible -- or hang. _active_vt() is a self-contained port of the
    same read hhd/plugins/overlay/x11.py's _z13_active_vt() already uses for
    exactly this problem on this branch (see that file's z13-vt-gate patch)
    -- not a dependency on it, since this package must not require patching
    hhd itself.

WHY MODE-ID RESOLUTION IS LIVE, NOT HARDCODED
    Ported from ~/Projects/z13-refresh-toggle.sh (the standalone script this
    replaces): kscreen-doctor mode IDs for 2560x1600@180 are NOT stable
    across sessions or hotplug -- observed on this machine as id 24 with an
    external display attached and id 1 without. Every apply re-resolves the
    id by parsing `kscreen-doctor -o` and matching RESOLUTION@RATE by
    content, exactly like the script does; only the panel's own output
    (eDP-1) is ever named, so an external display's state is left alone.

WHY THE RATE CAN FOLLOW THE POWER SOURCE
    z13ctl-plus used to carry the refresh rate inside its AC/battery presets
    ("On Battery: quiet, 60 Hz" / "Plugged In: balanced, 180 Hz"). That
    policy had to be disabled (`z13ctl-plus preset policy --disable`)
    because it fought hhd over TDP -- two daemons writing the same power
    knobs -- and disabling it took the refresh rate down with it, leaving
    the panel unmanaged on AC/battery transitions. The "auto" option here
    restores only the half that was worth keeping: the panel follows the
    power source, and nothing in this file goes anywhere near TDP.

    It is deliberately the same shape as the CPU Boost control in
    plugin.py -- a three-way auto/explicit `multiple`, a resolve helper
    (_resolve_rate, mirroring _resolve_boost), a write decision made
    against the RESOLVED value so an AC transition is a change even though
    the setting did not move, and no reconciliation while in "auto"
    because there the power source, not the panel, is the authority.
"""
import json
import os
import pwd
import re
import subprocess
import time

from hhd.plugins import HHDPlugin, load_relative_yaml
from hhd.plugins.conf import Config

from . import gamescope
# One source of truth for "are we on mains": the CPU plugin's AC read, reused
# rather than copied, so the two auto-controls in this package can never
# disagree about the power source within a tick.
from .plugin import _on_ac

OUTPUT = "eDP-1"
RESOLUTION = "2560x1600"
KSCREEN_TIMEOUT_S = 5
# How often to re-read state that nothing here changed, to catch changes made
# from KDE settings or another tool. Every read spawns kscreen-doctor, so this
# is a process every interval for the life of the daemon -- at 3s that was two
# processes every few seconds and a steady drip into hhd's journal. Catching an
# external change within ~15s is plenty for a control nobody is watching.
RECONCILE_INTERVAL_S = 15.0

# One kscreen-doctor -j serves every reader within a tick. Without this the
# refresh and VRR reads each spawned their own, doubling the cost for data that
# came from the same snapshot.
KSCREEN_CACHE_S = 1.0
_kscreen_cache = {"t": 0.0, "data": None}

# The highest rate the panel is offered at. At this rate the framerate bridge
# lifts gamescope's cap entirely rather than pinning it to a number.
TOP_RATE = "180"
# What "auto" drops to off mains -- the rate z13ctl-plus's battery preset used.
BATTERY_RATE = "60"
# The rates the control offers explicitly. Anything else in the setting (i.e.
# "auto") is a policy, not a rate, and has to be resolved before it is used.
RATES = (BATTERY_RATE, TOP_RATE)

# kscreen-doctor accepts `output.<name>.vrrpolicy.<never|automatic|always>`.
# It is absent from --help but works; confirmed by setting each in turn and
# reading the value back. The numeric form kscreen reports back is NOT in the
# same order as the words, which is an easy thing to get backwards:
#     0 = never, 1 = always, 2 = automatic
VRR_BY_NUM = {0: "never", 1: "always", 2: "automatic"}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_MODES_RE = re.compile(re.escape(RESOLUTION) + r"@")


def _active_vt():
    """Foreground VT number, or None when it cannot be determined. Same
    read hhd/plugins/overlay/x11.py's _z13_active_vt() uses."""
    try:
        with open("/sys/class/tty/tty0/active") as f:
            v = f.read().strip()
        if v.startswith("tty") and v[3:].isdigit():
            return int(v[3:])
    except Exception:
        pass
    return None


def _session_props(session_id):
    """Query `loginctl show-session` for a handful of properties. Public
    session metadata via systemd-logind -- no ptrace/root involved."""
    try:
        r = subprocess.run(
            ["loginctl", "show-session", session_id,
             "-p", "Type", "-p", "Class", "-p", "User", "-p", "VTNr"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return {}
    except Exception:
        return {}
    props = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            props[k] = v
    return props


def _wayland_display_for(uid):
    """Find WAYLAND_DISPLAY by reading a kwin_wayland process owned by `uid`
    -- its `--socket <name>` cmdline argument, not its environ (see module
    docstring for why cmdline, not environ). Returns None if no such
    process is found."""
    try:
        pids = os.listdir("/proc")
    except Exception:
        return None
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm") as f:
                if f.read().strip() != "kwin_wayland":
                    continue
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                args = f.read().split(b"\0")
        except Exception:
            continue
        for i, a in enumerate(args):
            if a == b"--socket" and i + 1 < len(args):
                return args[i + 1].decode(errors="replace")
    return None


def _find_session():
    """Find the active graphical Wayland user session, live -- not a
    cached or configured uid, so this stays correct if the desktop session
    is restarted. Returns (uid, gid, vtnr, wayland_display, runtime_dir),
    or None if no such session exists at all (e.g. a pure gamescope boot
    with no desktop session started)."""
    try:
        r = subprocess.run(["loginctl", "list-sessions", "--no-legend"],
                            capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return None
        session_ids = [ln.split()[0] for ln in r.stdout.splitlines() if ln.split()]
    except Exception:
        return None

    for sid in session_ids:
        props = _session_props(sid)
        if props.get("Type") != "wayland" or props.get("Class") != "user":
            continue
        try:
            uid = int(props["User"])
            vtnr = int(props["VTNr"])
        except (KeyError, ValueError):
            continue

        wayland_display = _wayland_display_for(uid)
        if not wayland_display:
            continue
        try:
            gid = pwd.getpwuid(uid).pw_gid
        except Exception:
            continue

        return uid, gid, vtnr, wayland_display, f"/run/user/{uid}"
    return None


def check_availability():
    """Returns (available, reason, session, backend, display).

    `backend` is "kscreen" when KDE is compositing to the panel, or
    "gamescope" when a gamescope session owns the foreground VT and its own
    refresh control is reachable instead (see gamescope.py). `display` is the
    X display for the gamescope backend, else None.

    Fails OPEN on an unreadable active VT (mirrors
    _z13_gamescope_is_foreground()'s own fail-open reasoning): a broken
    check should not itself hide a working control."""
    session = _find_session()
    if session is None:
        return False, "No active KDE Wayland session was found (via loginctl).", None, None, None
    _, _, vtnr, _, _ = session
    active = _active_vt()
    if active is None:
        return True, None, session, "kscreen", None
    if active != vtnr:
        # gamescope owns the screen. kscreen-doctor cannot reach anything
        # useful here, but gamescope publishes its own refresh control -- and
        # this is precisely the session where the QAM is the only UI there is,
        # so falling back to it keeps the slider alive rather than greying out.
        display = gamescope.find_display(session)
        if display is not None:
            return True, None, session, "gamescope", display
        return (
            False,
            f"The desktop session is on VT{vtnr}, but VT{active} is in the "
            "foreground (most likely gamescope), and no gamescope display "
            "could be found to control instead.",
            session,
            None,
            None,
        )
    return True, None, session, "kscreen", None


def read_vrr(session):
    """KDE's adaptive-sync policy for OUTPUT as one of never/automatic/always,
    or None on any failure."""
    o = _output_json(session)
    if o is None:
        return None
    return VRR_BY_NUM.get(o.get("vrrPolicy"))


def set_vrr(session, policy):
    """Set the adaptive-sync policy. Returns True on a verified change --
    kscreen-doctor exits 0 for an unparsed argument in some builds, so the
    value is read back rather than trusted."""
    if policy not in VRR_BY_NUM.values():
        return False
    r = _run_as_user(
        session, ["kscreen-doctor", f"output.{OUTPUT}.vrrpolicy.{policy}"]
    )
    if r is None or r.returncode != 0:
        return False
    _kscreen_cache["data"] = None  # never verify a write against a stale read
    return read_vrr(session) == policy


def bridge_fps_limit(session, rate):
    """Follow a panel refresh change with gamescope's framerate cap.

    Only meaningful for a NESTED gamescope -- one running as a client of KWin
    rather than owning its own VT. There, changing the panel rate genuinely
    changes what the display does, but gamescope's own vblank was fixed by -r
    at launch and cannot follow (measured: GAMESCOPE_DYNAMIC_REFRESH is stored
    and ignored off the DRM backend). So the game keeps rendering at the old
    rate into a display that is no longer showing it.

    gamescope's framerate limiter is a separate control that DOES apply
    nested, so dropping the panel to 60 can at least stop the game rendering
    above it. At the top rate the cap is lifted (0 = disabled) rather than
    pinned, so nothing is left throttling the session afterwards.

    Best-effort and non-fatal by design: if no gamescope is running, or the
    write does not verify, the refresh change itself still stands. Returns
    True only when a cap was actually applied and verified."""
    display = gamescope.find_display(session)
    if display is None:
        return False
    try:
        limit = 0 if str(rate) == TOP_RATE else int(rate)
    except (TypeError, ValueError):
        return False
    return gamescope.set_fps_limit(session, display, limit)


def _resolve_rate(mode, manual, backend):
    """Map the Auto/Manual gate onto an actual refresh rate.

    Manual is authoritative. Auto follows the power source. Returns None --
    meaning leave the panel alone -- when the backend is not kscreen (nothing
    reachable there) or the power source cannot be determined."""
    if backend != "kscreen":
        return None
    if mode == "manual":
        return str(manual) if str(manual) in RATES else None
    ac = _on_ac()
    if ac is None:
        return None
    return TOP_RATE if ac else BATTERY_RATE

def _run_as_user(session, args, timeout=KSCREEN_TIMEOUT_S):
    """subprocess.run dropped to the desktop session's uid/gid, mirroring
    hhd's own launch_overlay_de() precedent. Never raises -- a QAM control
    must not be able to crash hhd's main loop no matter what the subprocess
    or the privilege drop does."""
    uid, gid, _, wayland_display, runtime_dir = session
    try:
        home = pwd.getpwuid(uid).pw_dir
    except Exception:
        home = f"/home/{uid}"
    env = {
        "HOME": home,
        "WAYLAND_DISPLAY": wayland_display,
        "XDG_RUNTIME_DIR": runtime_dir,
        "XDG_SESSION_TYPE": "wayland",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        # Without a UTF-8 locale Qt prints a four-line "Detected locale \"C\"
        # ... switched to C.UTF-8" warning on EVERY kscreen-doctor call. This
        # env is minimal by design, so the children inherited plain "C" and
        # those four lines became ~95% of hhd's journal -- 1768 of 1858 lines
        # in one session -- rotating the real evidence out of retention. That
        # is not cosmetic: it is why the overlay-failure history went missing.
        #
        # C.UTF-8 rather than the user's locale on purpose: it keeps C's
        # collation and number formatting, so `kscreen-doctor -j` output stays
        # byte-predictable for the parsers below (no locale-dependent decimal
        # separator), and only the encoding changes.
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    try:
        return subprocess.run(
            args,
            env=env,
            user=uid,
            group=gid,
            capture_output=True,
            timeout=timeout,
            text=True,
        )
    except Exception:
        return None


def _modes_line(text):
    """The `Modes:` line for OUTPUT only, from `kscreen-doctor -o` text."""
    found = False
    for ln in text.splitlines():
        if not found:
            if re.search(r"Output:.*" + re.escape(OUTPUT), ln):
                found = True
            continue
        if "Modes:" in ln:
            return ln
    return None


def _mode_id_for(modes_line, rate):
    """Resolve RESOLUTION@rate -> numeric mode id, by content, not
    position -- ids are NOT stable across sessions/hotplug (see module
    docstring)."""
    if modes_line is None:
        return None
    m = re.search(
        r"(\d+):" + re.escape(RESOLUTION) + r"@" + re.escape(rate) + r"(?:\.\d+)?[*!]*",
        modes_line,
    )
    return m.group(1) if m else None


def _current_rate_from_line(modes_line):
    """The rate of the mode marked '*' (the active one), as e.g. "180"."""
    if modes_line is None:
        return None
    m = re.search(r"\d+:" + re.escape(RESOLUTION) + r"@([\d.]+)\*", modes_line)
    if not m:
        return None
    try:
        return str(int(float(m.group(1))))
    except ValueError:
        return None


def _kscreen_json(session):
    """Parsed `kscreen-doctor -j`, reused across readers within KSCREEN_CACHE_S
    so one tick costs one process rather than one per property."""
    now = time.monotonic()
    if (
        _kscreen_cache["data"] is not None
        and now - _kscreen_cache["t"] < KSCREEN_CACHE_S
    ):
        return _kscreen_cache["data"]
    r = _run_as_user(session, ["kscreen-doctor", "-j"])
    if r is None or r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except Exception:
        return None
    _kscreen_cache["t"] = now
    _kscreen_cache["data"] = data
    return data


def _output_json(session):
    data = _kscreen_json(session)
    if not data:
        return None
    for o in data.get("outputs", []):
        if o.get("name") == OUTPUT:
            return o
    return None


def _kscreen_output(session):
    r = _run_as_user(session, ["kscreen-doctor", "-o"])
    if r is None or r.returncode != 0:
        return None
    return _ANSI_RE.sub("", r.stdout)


def read_rate(session, backend="kscreen", display=None):
    """Current refresh rate, or None on any failure."""
    if backend == "gamescope":
        return gamescope.read_refresh(session, display)
    o = _output_json(session)
    if o is None:
        return None
    cur = o.get("currentModeId")
    for m in o.get("modes", []):
        if m.get("id") == cur and m.get("refreshRate"):
            return str(round(m["refreshRate"]))
    return None


def set_rate(session, rate, backend="kscreen", display=None):
    """Switch to `rate` Hz. Returns True on a verified success."""
    if backend == "gamescope":
        return gamescope.set_refresh(session, display, rate)
    text = _kscreen_output(session)
    if text is None:
        return False
    mode_id = _mode_id_for(_modes_line(text), rate)
    if mode_id is None:
        return False
    r = _run_as_user(session, ["kscreen-doctor", f"output.{OUTPUT}.mode.{mode_id}"])
    return r is not None and r.returncode == 0


class Z13DisplayPlugin(HHDPlugin):
    def __init__(self) -> None:
        self.name = "z13_display"
        self.priority = 10
        self.log = "z13v"

        # Context is stored for completeness/diagnostics (see open() below)
        # but the actual privilege drop uses whatever uid check_availability()
        # discovers LIVE from the running kwin_wayland process -- see the
        # module docstring's "WHY THIS RUNS AS THE DESKTOP USER" section for
        # why a live discovery is preferred over trusting a fixed uid.
        self.context = None

        # None on purpose: forces the very first tick to silently ADOPT
        # whatever the panel is already running at, rather than treating
        # "no prior agreement" as a change to push out -- unlike epp/boost
        # in plugin.py, a refresh-rate switch is a visible flicker, not a
        # cheap sysfs write, so hhd starting up must never itself trigger one.
        self.old_rate = None
        self.old_status = None
        self.old_backend = None
        self.old_vrr = None

        self._last_reconcile = 0.0

    def open(self, emit, context) -> None:
        self.context = context

    def settings(self):
        # Same third-level nesting requirement as plugin.py's z13_cpu --
        # see that file's settings() for the full explanation.
        sets = load_relative_yaml("./display_settings.yml")
        # Nested INSIDE hhd's own "display" container (plugins/overlay/
        # gamemode.yml, shown under the General section), so the refresh
        # control sits with the other display settings rather than beside them.
        #
        # There is no display SECTION to use instead: hhd had "system" and
        # "utilities" sections until early 2025 and removed both. hhd's own
        # display plugin still returns {"system": {"display": ...}} and is
        # orphaned for exactly that reason -- its section was deleted out from
        # under it. Not a precedent to copy.
        #
        # Same merge contract as plugin.py: "type": "container" is required for
        # merge_reduce() to union children, and "title" is omitted so hhd's own
        # "Display" survives rather than being overwritten by ours.
        # FLATTENED -- see plugin.py's settings() for why: hide-title only
        # applies at the first level inside a section, so a nested wrapper
        # rendered "Display" and then "Z13 Display" beneath it. The controls
        # become direct siblings of hhd's own touchscreen toggles instead.
        children = sets["z13_display"]["children"]
        for c in children.values():
            tags = c.setdefault("tags", [])
            if "non-essential" not in tags:
                tags.append("non-essential")
        return {"gamemode": {"display": {"type": "container", "children": children}}}

    def update(self, conf: Config):
        available, reason, session, backend, display = check_availability()
        new_status = "available" if available else "unavailable"

        if new_status != self.old_status or backend != self.old_backend:
            self.old_status = new_status
            self.old_backend = backend
            conf["gamemode.display.status.mode"] = new_status
            if not available:
                conf["gamemode.display.status.unavailable.reason"] = (
                    reason or "No usable KDE Wayland session detected."
                )
            # A transition either way invalidates any prior agreement on the
            # rate: on the next tick this falls through to the "adopt
            # silently" branch below rather than comparing against a value
            # read while a different (or no) session was active.
            self.old_rate = None
            self.old_vrr = None

        if not available:
            return

        # Resolved, not raw, exactly like plugin.py's boost: under "auto" this
        # follows the power source, so plugging the charger in or out changes
        # the target WITHOUT the setting itself changing -- which is the whole
        # point of the option, and the reason the comparison below is against
        # the resolved rate rather than the config value.
        rate_mode = conf["gamemode.display.status.available.refresh_rate.mode"].to(str)
        rate_manual = conf[
            "gamemode.display.status.available.refresh_rate.manual.rate"
        ].to(str)
        new_rate = _resolve_rate(rate_mode, rate_manual, backend)

        if self.old_rate is None:
            # First tick after becoming available: ADOPT, never apply. Even in
            # "auto" -- hhd starting up must not flicker the panel before it
            # has established what the panel is already doing. If auto and the
            # power source then disagree with that, the next tick applies it.
            live = read_rate(session, backend, display)
            if live is not None:
                self.old_rate = live
                # Only ever write a rate back into an EXPLICIT setting. Doing
                # it under "auto" would silently demote the user's policy to
                # whatever the panel happened to be showing at startup.
                if rate_mode == "manual":
                    conf[
                        "gamemode.display.status.available.refresh_rate.manual.rate"
                    ] = live
            return

        if new_rate is not None and new_rate != self.old_rate:
            self.old_rate = new_rate
            set_rate(session, new_rate, backend, display)
            # A nested gamescope cannot follow the panel; bring its framerate
            # cap along instead. No-op when nothing nested is running. This
            # sits on the shared write path on purpose, so an automatic
            # AC-transition switch carries the cap along just like a manual
            # one does -- the game does not care which of the two moved it.
            if backend == "kscreen":
                bridge_fps_limit(session, new_rate)
            return

        now = time.monotonic()
        if now - self._last_reconcile < RECONCILE_INTERVAL_S:
            return
        self._last_reconcile = now

        # Reconcile an explicit choice only. Under "auto" the power source is
        # the authority (same reasoning as the boost control's reconcile
        # guard): pulling a live reading back into the config would overwrite
        # the policy with a momentary state, and re-asserting the resolved
        # rate every 15s would mean fighting KDE's own display settings for
        # anyone who deliberately changed the rate mid-session. Auto stays
        # quiet until the charger actually moves.
        if rate_mode == "manual":
            live = read_rate(session, backend, display)
            if live is not None and live != self.old_rate:
                self.old_rate = live
                conf[
                    "gamemode.display.status.available.refresh_rate.manual.rate"
                ] = live
                # Someone changed the rate outside hhd (KDE display settings, a
                # script). The bridge applies the same either way.
                if backend == "kscreen":
                    bridge_fps_limit(session, live)

        # --- adaptive sync -------------------------------------------------
        # kscreen-doctor only reaches KDE, so this is meaningless on the
        # gamescope backend, where gamescope owns VRR itself.
        if backend != "kscreen":
            return

        new_vrr = conf["gamemode.display.status.available.vrr"].to(str)

        if self.old_vrr is None:
            live_vrr = read_vrr(session)
            if live_vrr is not None:
                self.old_vrr = live_vrr
                conf["gamemode.display.status.available.vrr"] = live_vrr
            return

        if new_vrr != self.old_vrr:
            self.old_vrr = new_vrr
            set_vrr(session, new_vrr)
            return

        live_vrr = read_vrr(session)
        if live_vrr is not None and live_vrr != self.old_vrr:
            self.old_vrr = live_vrr
            conf["gamemode.display.status.available.vrr"] = live_vrr
