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
