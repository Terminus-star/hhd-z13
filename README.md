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
