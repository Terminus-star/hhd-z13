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

## Gamescope session: the socket is `gamescope-0`, not `wayland-*`

Symptom: in the standalone gamescope session the Armoury button does nothing.
hhd is working correctly — the journal shows the gesture and the command:

```
OVRL INFO  Gesture: custom_armoury, section: custom, key: armoury
OVRL INFO  Executing overlay command: 'open_qam'
OVRL INFO  Found overlay executable '/usr/local/bin/hhd-ui-wayland'
OVRL INFO  Overlay launched.
OVRL ERROR Overlay thread died
```

Cause: a gamescope session has **no `wayland-*` socket at all**. Its runtime
dir holds `gamescope-0` (plus `gamescope-0-ei`, `gamescope-stats`,
`gamescope-environment`). The wrapper globbed only `wayland-*`, found nothing,
and fell through to the X11 fallback — so it never got a compositor, exactly
the situation the fallback was meant to be a last resort for.

The wrapper now looks for `gamescope-<digits>` when no `wayland-*` exists,
matching the number strictly so the EIS socket (`gamescope-0-ei`) and the
non-socket `gamescope-stats` / `gamescope-environment` entries are skipped. A
real `wayland-*` still wins when both are present, since under Plasma that is
the session compositor and any `gamescope-*` would be a nested instance.

Verified against the live runtime dir and three synthetic ones:

| runtime dir contains | picked |
|---|---|
| gamescope-0, -ei, .lock, -stats, -environment | `gamescope-0` |
| wayland-0 + lock | `wayland-0` |
| wayland-1 + gamescope-0 | `wayland-1` |

Note `--class=dev.hhd.hhd-ui` is inert in a gamescope session — there is no
KWin and therefore no rule to match — but harmless.
