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

---

# HANDOFF: overlay dead in Plasma — hhd's gamescope detection is process-based

**For whoever maintains this directory.** Written 2026-08-28 by a different
agent that did *not* write these patches. Nothing here has been fixed — the
diagnosis is complete, the fix is a design decision that belongs to you.

## Symptom

The Armoury button does nothing, in **both** the gamescope session and Plasma.
In Plasma a transparent Electron window flashes up every ~10 s and disappears.

The button itself is fine. The journal shows the full command path working:

```
OVRL INFO   Gesture: custom_armoury, section: custom, key: armoury
OVRL INFO   Executing overlay command: 'open_qam'
OVRL INFO   Found overlay executable '/usr/local/bin/hhd-ui-wayland'
OVRL INFO   Overlay launched.
OVRL ERROR  UI Window not found, exitting overlay.      <- ~10 s later
OVRL INFO   Stopping overlay process.
OVRL ERROR  Overlay thread died
```

That loop **is** the transparent window: a fresh Electron process each cycle,
killed before it paints.

## Root cause

`hhd` decides whether it is "in gamescope" from a **process** check, not from
the current session:

```python
# plugins/overlay/x11.py
def get_gamescope_displays():
    """Returns X11 UNIX sockets from gamescope opened under /tmp"""
    files = subprocess.run(["lsof", "-c", "gamescope-wl", "-Fn"], ...)
```

**Switching from the gamescope session to Plasma does not end it.** It stays
online on its own VT, and its gamescope process keeps holding the X11 sockets:

```
$ loginctl list-sessions
12  1000 termis seat0 21775 user tty2 no -       # Desktop=gamescope, State=online

$ pgrep -a gamescope
21779 /bin/bash /usr/bin/start-gamescope-session
21891 gamescope --generate-drm-mode fixed --xwayland-count 2 ... -e -R ...

$ lsof -c gamescope-wl -Fn | grep X11-unix
/tmp/.X11-unix/X0
/tmp/.X11-unix/X1
```

So in Plasma, hhd still believes gamescope is active. It takes the **X11
overlay path** aimed at gamescope's display and never reaches the
compatibility-QAM path Plasma needs. Meanwhile `hhd-ui-wayland` does its job
and launches the UI as a **Wayland** client on the Plasma compositor. hhd then
searches gamescope's X display for WM_CLASS `dev.hhd.hhd-ui`
(`x11.py: find_hhd`), never finds it, and `base.py` bails:

```python
# plugins/overlay/base.py
if not hhd:
    logger.error(f"UI Window not found, exitting overlay.")
    break
```

Immediate workaround, confirmed to be the trigger:

```bash
loginctl terminate-session 12    # the Desktop=gamescope one
pgrep -a gamescope               # must be empty
systemctl restart hhd@termis
```

`pgrep -a gamescope` is the one-line check whenever the overlay misbehaves in
Plasma — the symptom looks nothing like the cause.

## `hhd-ui-wayland` is NOT at fault — do not "fix" the socket glob

It was suspected, changed, and the change **made things worse**. Recorded so it
is not retried:

A gamescope session has no `wayland-*` socket, only `gamescope-0`, so the
wrapper's glob matches nothing there and falls through to its X11 fallback.
That looks like the bug. It is not — on gamescope the X11 fallback is
**correct**, because hhd needs an X11 window to find. Pointing Electron at
`gamescope-0` as a native Wayland client guarantees there is no X window at all
and `find_hhd` can never succeed. Reverted the same day; the reasoning is in the
wrapper's comments.

Note also that `UI Window not found` was already being logged *before* that
change (13:24:37 vs the change at 13:38), which is what proved the wrapper was
never the problem.

## The actual design question, which is yours to decide

Killing the stray session fixes today. It will recur every time the gamescope
session is used and left running — which is normal, since switching VTs does not
end a session.

The mismatch is that hhd asks **"does a gamescope process exist anywhere?"**
when the question it needs answered is **"is the session I am about to draw
into a gamescope one?"**

Options, roughly in increasing order of intrusiveness:

1. **Leave hhd alone; make it a documented operating rule** — end the gamescope
   session before returning to Plasma. Zero code, but it will be forgotten.
2. **Extend `hhd-qam-toggle-patch.py`** — it already patches
   `plugins/overlay/*.py` idempotently with `.z13-orig` backups and a pacman
   hook, so the mechanism exists. Make `get_gamescope_displays()` (or
   `is_gamescope_running()`) return empty when the *active* session is not
   gamescope. `loginctl show-session $XDG_SESSION_ID -p Desktop`, or comparing
   the gamescope X socket's session against the active seat, would both do it.
   This is the targeted fix and fits the existing tooling.
3. **Upstream it.** The check is arguably wrong for anyone who uses both
   sessions, not just this machine.

Not attempted here because it changes overlay routing, which is squarely the
area these patches own — and the previous attempt to help in that area by
someone without that context made things worse.

## Verified / not verified

Verified:

* The stray session exists and holds the X11 sockets (`lsof` output above).
* The button, gesture and `open_qam` command path all work.
* hhd's display *selection* succeeds — it reaches "Attempting to launch
  overlay", i.e. past `get_overlay_display`.
* The failure is specifically `find_hhd` returning nothing.
* The wrapper revert is installed and matches the repo copy.

Not verified:

* Whether killing the stray session actually restores the overlay — the fix was
  left for you rather than applied.
* Which of gamescope's two Xwayland displays (`:0`/`:1`) hhd selected. It is
  logged only at `logger.debug`, and `setup_logger` hardcodes
  `level=logging.INFO` (`hhd/logging.py`), so `HHD_DEBUG=1` does **not** expose
  it. Raising that level needs a patch too.

