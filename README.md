# hhd-patches

Fixes for Handheld Daemon on KDE Wayland. `install.sh` applies or reverts all
of it; `install.sh --revert` restores pristine `.z13-orig` backups.

**Full rationale — including the four failed toggle attempts, the measured
resume ordering, and the traps — lives in `~/Projects/README.md`
("hhd OVERLAY WORK"). This file is only an index.**

| File | Purpose |
|---|---|
| `hhd-qam-toggle-patch.py` | Makes the Armoury button **toggle** the QAM instead of only opening it. Idempotent; verifies the patched file compiles; refuses to run if its anchors moved after an hhd refactor. |
| `99-z13-hhd-qam-toggle.hook` | pacman hook re-applying the patch after any `hhd` upgrade, which would otherwise silently revert it. |
| `hhd-ui-wayland` | Electron wrapper: native Wayland (sharp, not blurry XWayland) + `--class` so the KWin rule matches. |
| `10-wayland-overlay.conf` | systemd drop-in pointing hhd at that wrapper via `HHD_OVERLAY`. |
| `kwin-hhd-place/` | KWin script placing the overlay from live screen geometry, so it follows scale/resolution changes. |
| `kwinrulesrc.hhd-rule.bak` | Snapshot of the working KWin rule (`~/.config` is not in a repo). |
| `install.sh` | Applies or reverts everything. Reverts before applying, so an updated patch replaces an older one. |

## Three things that will bite you

1. **`mount`-style helper shadowing:** `hhd` picks the overlay binary from
   `$HHD_OVERLAY` *before* its built-in search path — that is why the wrapper
   works without patching hhd-ui.
2. **hhd strips the environment.** `launch_overlay_de()` builds a minimal env
   (`HOME`, `XAUTHORITY`, `DISPLAY`, `HHD_MANAGED`) from scratch, so anything
   set on hhd's service never reaches the UI. The wrapper reconstructs what it
   needs itself.
3. **Do NOT re-add `DBUS_SESSION_BUS_ADDRESS`** to the wrapper. Measured: it
   makes startup *slower* (0.56 s -> 0.87 s). The `dbus/bus.cc:405` errors are
   cosmetic.

## ROOT CAUSE: a leftover gamescope session breaks the overlay in KDE, 2026-08-28

**This is the actual cause of the dead Armoury button, in both sessions.**

`hhd` decides whether it is "in gamescope" purely by looking for a running
process holding an X11 socket:

```python
# plugins/overlay/x11.py
def get_gamescope_displays():
    """Returns X11 UNIX sockets from gamescope opened under /tmp"""
    files = subprocess.run(["lsof", "-c", "gamescope-wl", "-Fn"], ...)
```

Switching from the gamescope session to Plasma **does not end it**. It stays
alive on its own VT:

```
$ loginctl list-sessions
12  1000 termis seat0 21775 user tty2 no -      <- Desktop=gamescope, State=online

$ pgrep -a gamescope
21779 /bin/bash /usr/bin/start-gamescope-session
21891 gamescope --generate-drm-mode fixed --xwayland-count 2 ... -e -R ...

$ lsof -c gamescope-wl -Fn | grep X11-unix
/tmp/.X11-unix/X0
/tmp/.X11-unix/X1
```

So while sitting in Plasma, hhd still believes gamescope is active, takes the
**X11 overlay path** aimed at gamescope's display, and never reaches the
compatibility-QAM path that Plasma needs. Meanwhile `hhd-ui-wayland` correctly
launches the UI as a **Wayland** client on the Plasma compositor. hhd then
searches the wrong display for WM_CLASS `dev.hhd.hhd-ui`, fails, kills the UI
and relaunches:

```
OVRL INFO   Overlay launched.
OVRL ERROR  UI Window not found, exitting overlay.   (~10s later)
OVRL ERROR  Overlay thread died
```

**That loop is the "transparent window that keeps reappearing" in KDE** — each
cycle is a fresh Electron window flashing up before hhd kills it.

### Fix

End the orphaned session, then restart hhd:

```bash
loginctl terminate-session 12      # the Desktop=gamescope one
systemctl restart hhd@termis
```

Confirm it is really gone before restarting hhd — `pgrep -a gamescope` must be
empty, otherwise hhd will take the same wrong path again.

### Why this matters beyond today

Any time the gamescope session has been used and left running, hhd's overlay
will misbehave in Plasma, with a symptom that looks nothing like its cause.
`pgrep -a gamescope` is the one-line check.

It also explains the earlier confusion: the gamescope-socket "fix" was chasing
a wrapper that was behaving correctly the whole time.

---

## Gamescope session: the overlay must stay on X11 (reverted change)

**Recorded so the same wrong turn is not taken again.**

A gamescope session has no `wayland-*` socket — its compositor socket is
`gamescope-0`. The wrapper's glob therefore matches nothing there and falls
through to the X11 fallback, which *looks* like the bug behind a dead Armoury
button. On 2026-08-28 the glob was extended to match `gamescope-<digits>`.
**That made it worse and was reverted the same day.**

Why it was wrong: hhd finds the UI by searching an **X display** for WM_CLASS
`dev.hhd.hhd-ui`.

```
plugins/overlay/x11.py   find_hhd()  -> find_win(disp, ["dev.hhd.hhd-ui"])
plugins/overlay/base.py  if not hhd: "UI Window not found, exitting overlay."
```

In a gamescope session hhd selects a gamescope X display — gamescope runs two
Xwayland servers, `:0` and `:1` — and hands the wrapper `DISPLAY=`, expecting an
X11 window back. Point Electron at `gamescope-0` as a native Wayland client and
there is no X window at all, so `find_hhd` can never succeed.

**On gamescope the X11 fallback is correct, not a failure mode.**

### Still unresolved

`UI Window not found` was already being logged *before* that change, so the
gamescope overlay has a separate problem. What is known:

* The button works: `Gesture: custom_armoury` -> `Executing overlay command:
  'open_qam'` -> `Overlay launched`.
* hhd then fails to find the window within `STARTUP_MAX_DELAY` and exits, on a
  loop.
* hhd's display selection itself succeeds — it gets past `get_overlay_display`
  to "Attempting to launch overlay". Which of `:0`/`:1` it picked is only
  logged at debug level, so that is the next thing to capture.

Next step is hhd debug logging, to see the chosen `DISPLAY` and whether
Electron ever creates a window on it.
