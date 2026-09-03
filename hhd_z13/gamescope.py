"""z13_display gamescope backend -- refresh rate when gamescope, not KWin,
owns the panel.

WHY THIS EXISTS
    display.py's kscreen-doctor path needs the KDE Wayland session to be the
    FOREGROUND session. A standalone gamescope session runs on its own VT and
    keeps KWin alive but backgrounded, so kscreen-doctor would appear to
    succeed while changing nothing visible -- which is why check_availability()
    gates on the VT at all. That gate is correct, but its consequence was that
    the QAM's own refresh control went dead in exactly the session where the
    QAM is the only control surface there is.

    gamescope exposes its own refresh control as X properties on its XWayland
    root window, which is what Steam's performance sidebar drives. This module
    talks to those directly, so the slider keeps working on the gamescope VT.

WHY DISPLAY DISCOVERY IS BY ATOM, NOT ENVIRON
    Same constraint display.py documents at length: reading another process's
    /proc/<pid>/environ needs CAP_SYS_PTRACE and is gated by
    kernel.yama.ptrace_scope, and gamescope's XWayland DISPLAY is not reliably
    in any environment we can read anyway. Instead every socket in
    /tmp/.X11-unix is probed for the GAMESCOPE_PID property: gamescope sets it
    on its own root window and nothing else does. Confirmed on this machine --
    :0 (KDE's XWayland) reports "no such atom", :1 (gamescope's) reports the
    pid. The pid is then checked against /proc so a stale socket from a dead
    session is never adopted.

WHAT CANNOT BE DONE, AND WHY THE WRITE IS VERIFIED
    Setting GAMESCOPE_DYNAMIC_REFRESH is a *request*. gamescope stores the
    value regardless, and only acts on it when it owns a real display mode --
    i.e. on the DRM backend a standalone session uses. Measured on the
    headless backend (2026-09-03):

        started with -r 180        REFRESH_RATE_FEEDBACK = 180
        wrote DYNAMIC_REFRESH=60   DYNAMIC_REFRESH       = 60   (accepted)
                                   REFRESH_RATE_FEEDBACK = 180  (unchanged)

    A nested session is likewise not DRM, and its vblank is fixed by -r at
    launch -- measured on the nested-wayland backend with the host compositor
    at 180Hz, gamescope reports 60 without -r and 180 with it, so it neither
    inherits the host rate nor tracks changes to it. So set_refresh() never
    trusts the write: it re-reads the feedback property and reports success
    only if the value actually moved. A control
    that silently does nothing is the failure mode this whole plugin was
    written to avoid.
"""
import os
import re
import subprocess

_PID_RE = re.compile(r"GAMESCOPE_PID\(CARDINAL\)\s*=\s*(\d+)")
_RATE_RE = re.compile(r"GAMESCOPE_DISPLAY_REFRESH_RATE_FEEDBACK\(CARDINAL\)\s*=\s*(\d+)")
_FPS_RE = re.compile(r"GAMESCOPE_FPS_LIMIT\(CARDINAL\)\s*=\s*(\d+)")

XPROP_TIMEOUT_S = 4


def _xprop(session, display, args):
    """Run xprop on `display`, dropped to the desktop user. Returns stdout or
    None. Never raises: a QAM control must not be able to crash hhd's loop."""
    import pwd

    uid, gid, _, _, runtime_dir = session
    try:
        home = pwd.getpwuid(uid).pw_dir
    except Exception:
        home = f"/home/{uid}"
    env = {
        "HOME": home,
        "DISPLAY": display,
        "XDG_RUNTIME_DIR": runtime_dir,
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    }
    try:
        r = subprocess.run(
            ["xprop", "-root"] + args,
            env=env, user=uid, group=gid,
            capture_output=True, timeout=XPROP_TIMEOUT_S, text=True,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return r.stdout


def find_display(session):
    """The X display gamescope's XWayland is serving, or None.

    xprop prints "no such atom on any window." to stdout and still exits 0
    when the property is absent, so presence is decided by the value pattern
    matching -- not by the call succeeding or the output being non-empty."""
    try:
        socks = os.listdir("/tmp/.X11-unix")
    except Exception:
        return None
    for s in sorted(socks):
        if not s.startswith("X") or not s[1:].isdigit():
            continue
        display = ":" + s[1:]
        out = _xprop(session, display, ["GAMESCOPE_PID"])
        if not out:
            continue
        m = _PID_RE.search(out)
        if not m:
            continue
        # Do not adopt a socket left behind by a dead session.
        if not os.path.isdir(f"/proc/{m.group(1)}"):
            continue
        return display
    return None


def read_refresh(session, display):
    """gamescope's current output refresh as e.g. "180", or None."""
    out = _xprop(session, display, ["GAMESCOPE_DISPLAY_REFRESH_RATE_FEEDBACK"])
    if not out:
        return None
    m = _RATE_RE.search(out)
    return m.group(1) if m else None


def set_refresh(session, display, rate):
    """Ask gamescope for `rate` Hz. True only on a VERIFIED change -- see the
    module docstring on why the write alone proves nothing."""
    before = read_refresh(session, display)
    if before == str(rate):
        return True
    out = _xprop(
        session, display,
        ["-f", "GAMESCOPE_DYNAMIC_REFRESH", "32c",
         "-set", "GAMESCOPE_DYNAMIC_REFRESH", str(rate)],
    )
    if out is None:
        return False
    return read_refresh(session, display) == str(rate)


def read_fps_limit(session, display):
    """gamescope's current framerate cap as an int, or None. 0 means no cap
    (gamescope's own --framerate-limit documents 0 as disabled)."""
    out = _xprop(session, display, ["GAMESCOPE_FPS_LIMIT"])
    if not out:
        return None
    m = _FPS_RE.search(out)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def set_fps_limit(session, display, limit):
    """Cap the game at `limit` fps, or lift the cap with 0.

    This is a DIFFERENT control from the refresh rate, and unlike
    GAMESCOPE_DYNAMIC_REFRESH it is gamescope's own limiter rather than a
    request for a display mode -- so it applies on any backend, including the
    nested one where a refresh change cannot. Measured nested with -r 180:
    writing GAMESCOPE_FPS_LIMIT=60 reads back 60 while
    REFRESH_RATE_FEEDBACK stays 180, i.e. the game is capped without the
    compositor's vblank moving.

    Verified like set_refresh(): the value is read back rather than assumed."""
    limit = int(limit)
    if read_fps_limit(session, display) == limit:
        return True
    out = _xprop(
        session, display,
        ["-f", "GAMESCOPE_FPS_LIMIT", "32c",
         "-set", "GAMESCOPE_FPS_LIMIT", str(limit)],
    )
    if out is None:
        return False
    return read_fps_limit(session, display) == limit
