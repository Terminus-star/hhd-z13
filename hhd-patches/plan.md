# Plan 1 — hhd overlay and plugin work

> **Project 1 of 2.** Independent of
> [Plan 2 — hibernate GPU gate](../plan-hibernate-gpu-gate.md); they share no
> files and can be done in either order.
>
> Copied from the session plan file, 2026-08-28. Sits beside the patches it
> concerns. Superseded priorities are marked inline — read Part 0 first.

---

## Where everything lives

### This directory (`~/Projects/hhd-patches`)

| Path | What it is |
|---|---|
| `README.md` | traps, root-cause write-ups, the HANDOFF section |
| `plan.md` | this file |
| `install.sh` | installs everything below; `--revert` undoes it. Reverts then re-applies, so it upgrades cleanly |
| `hhd-qam-toggle-patch.py` | the patcher — four guarded edits, `--check` / `--revert` |
| `hhd-ui-wayland` | Electron launcher; picks X11 vs Wayland from hhd's env |
| `10-wayland-overlay.conf` | systemd drop-in setting `HHD_OVERLAY` |
| `99-z13-hhd-qam-toggle.hook` | pacman hook re-applying the patch after an hhd upgrade |
| `kwin-hhd-place/` | KWin placement script |

### Installed locations

| Installed path | From |
|---|---|
| `/usr/local/lib/z13/hhd-qam-toggle-patch.py` | `hhd-qam-toggle-patch.py` — **this is what the pacman hook runs**, not the repo copy |
| `/usr/local/bin/hhd-ui-wayland` | `hhd-ui-wayland` |
| `/etc/pacman.d/hooks/99-z13-hhd-qam-toggle.hook` | `99-z13-hhd-qam-toggle.hook` |
| `/etc/systemd/system/hhd@.service.d/10-wayland-overlay.conf` | `10-wayland-overlay.conf` |

### Upstream hhd files the patcher edits

`/usr/lib/python3*/site-packages/hhd/plugins/overlay/` —
`base.py` (`z13-qam-toggle`, `z13-oneshot`), `__init__.py` (`z13-qam-toggle`),
`x11.py` (`z13-vt-gate`). Pristine copies are kept beside each as `*.z13-orig`;
`overlay.py` is read but never patched.

### Cross-repo docs

| Path | What it holds |
|---|---|
| `~/Projects/README.md` | authoritative top-level change log |
| `~/Projects/MAP.md` | directory map of every project |
| `~/Projects/documentation.md` | NTFS drivers, mount tooling, misc reference |
| `~/Projects/plan-hibernate-gpu-gate.md` | **Plan 2**, the `z13-hibernate-plus` work |
| `~/Projects/z13-hibernate-plus/` | separate git repo (gitlink here); sleep/hibernate stack |

### Live diagnostics

```bash
python3 /usr/local/lib/z13/hhd-qam-toggle-patch.py --check   # 4 rows, all PATCHED
journalctl -u hhd@$USER -n 100 --no-pager | grep OVRL
pgrep -a gamescope                    # triage: a stray session breaks the overlay
xwininfo -display :0 -root -children  # spot decoy windows (see README trap)
```

---

## Part 0 — session-aware gamescope detection — **IMPLEMENTED 2026-08-28**

**Found 2026-08-28 by a separate agent; full write-up in `README.md` under
"HANDOFF".** This outranked everything below, because it broke the overlay in
*both* sessions, whereas the rest of this plan adds features.

**Status: fixed.** `hhd-qam-toggle-patch.py` now carries two further guarded
edits — `z13-vt-gate` (session-aware detection) and `z13-oneshot` (no endless
relaunch). See "What shipped" below.

### The defect

hhd answers *"am I in gamescope?"* with a **process** check, not a session one:

```python
# plugins/overlay/x11.py
def get_gamescope_displays():
    files = subprocess.run(["lsof", "-c", "gamescope-wl", "-Fn"], ...)
```

Switching VTs from gamescope back to Plasma **does not end the gamescope
session** — it stays `online` on its own VT and keeps holding
`/tmp/.X11-unix/X0` and `X1`. So in Plasma hhd still believes gamescope is
active, takes the X11 overlay path, searches gamescope's X display for
WM_CLASS `dev.hhd.hhd-ui`, never finds it (the wrapper correctly launched a
*Wayland* client on Plasma), and kills/relaunches on a ~10 s loop — the
"transparent window that keeps reappearing".

### Immediate workaround (superseded by the fix)

```bash
loginctl terminate-session <the Desktop=gamescope one>
pgrep -a gamescope        # MUST be empty before the next line
systemctl restart hhd@termis
```

`pgrep -a gamescope` is the one-line triage whenever the overlay misbehaves in
Plasma — the symptom looks nothing like the cause.

### What shipped

Two additional edits in `hhd-qam-toggle-patch.py`, each with its own mark, its
own anchors, and the same refuse-rather-than-guess behaviour as the toggle.

**`z13-vt-gate` — `plugins/overlay/x11.py`**

`get_gamescope_displays()` now switches `lsof` to `-Fpn` so it knows *which
process* owns each X socket, reads that process's `XDG_VTNR` from
`/proc/<pid>/environ`, and compares it against the foreground VT in
`/sys/class/tty/tty0/active`. A gamescope instance on a background VT is
skipped.

Chosen over `loginctl show-session`: no subprocess, no logind round-trip, and
it works even though gamescope lives in a **user service**
(`gamescope-session.service`) rather than a session scope — so the obvious
cgroup-to-session mapping would **not** have worked. `XDG_VTNR` is present in
gamescope's environ; the cgroup path is not session-tagged.

It **fails open**: if either VT number is unreadable, upstream behaviour is
kept, because a broken overlay inside gamescope is worse than losing the
desktop fallback. The one-shot latch below covers that case instead.

Gating this one function is enough — `is_gamescope_running()`,
`_open_overlay()` and `monitor_gamescope()` all route through it.

**`z13-oneshot` — `plugins/overlay/base.py`**

`launch_overlay()` latches after the first failed *automatic* attempt instead
of retrying every `OVERLAY_CHECK_INTERVAL` (5 s) forever. The latch clears on
an explicit QAM press (`update()`), and re-arms when gamescope is no longer on
the active VT — so entering gamescope works normally again.

Deliberately belt-and-braces: the vt-gate stops this loop from starting, the
latch stops *any* future unlaunchable overlay from becoming a spawn/kill cycle.

### Verified

| Condition | `get_gamescope_displays()` | Path taken |
|---|---|---|
| gamescope on VT2, active VT3 | `[]` | desktop |
| active VT is gamescope's | `[':0', ':1']` | gamescope |
| VT unreadable | `[':0', ':1']` | upstream fallback |
| no `XDG_VTNR` | `[':0', ':1']` | upstream fallback |

Patcher tested against copies of both pristine-upstream and
already-toggle-patched files: all three files compile, every mark lands, and a
second run is a no-op.

### DO NOT re-attempt the wrapper socket glob

Extending `hhd-ui-wayland`'s glob to match `gamescope-<digits>` was tried on
2026-08-28 and **made things worse**; reverted the same day. On gamescope the
X11 fallback is *correct*, because hhd needs an **X11 window** to find — a
native Wayland client on `gamescope-0` guarantees `find_hhd` can never succeed.
`UI Window not found` was already being logged *before* that change, which is
what proved the wrapper was never at fault.

### Open, needs measurement

* **Live confirmation** — the logic is verified against real `/proc` and real
  VT state, but hhd has not yet been restarted onto the patched files (needs
  root).
* Terminating the stray session is no longer needed, so it was never verified.
  The fix makes hhd ignore a backgrounded gamescope rather than requiring you
  to kill it.
* Which of gamescope's two Xwayland displays (`:0`/`:1`) hhd picks. Logged only
  at `logger.debug`, and `setup_logger` hardcodes `level=logging.INFO`
  (`hhd/logging.py`), so `HHD_DEBUG=1` does **not** expose it — surfacing it
  needs its own patch.

---

## Part 1 onwards — the original plugin plan

## Context

**The observation that prompted this is correct.** Patching hhd's Python in
place is a fork in all but name — and arguably *worse* than a fork:

| | a real fork | what we have now |
|---|---|---|
| Versioned | yes, in git | no — an unversioned mutation of installed files |
| Diffable against upstream | yes | only via `.z13-orig` backups |
| Rebasable on upstream changes | yes | no — it re-applies blindly via a pacman hook |
| Fails visibly when upstream moves | merge conflict | patcher refuses, feature silently disappears |

So the red line was crossed when we edited `plugins/overlay/__init__.py`. The
goal of this plan is to **get back across it**, not to go further.

**The good news:** hhd is genuinely extensible. It discovers plugins via
standard entry points —

```python
for autodetect in entry_points(group="hhd.plugins"):     # hhd/__main__.py:235
```

— and plugins subclass `HHDPlugin` (`hhd/plugins/plugin.py:123`), returning a
`settings()` schema that **draws QAM controls**. A separate package can add
features with **zero changes to hhd**, surviving upgrades with no hook.

Upstream has also scaffolded `hhd/plugins/display/settings.yml` with the hint
*"Brightness (and framerate TBD)"* — and it is **not registered** in
`entry_points.txt`. Refresh rate is a known, unfinished upstream gap, which
makes it a plausible contribution rather than a permanent private divergence.

---

## Part 1 — What hhd is missing vs z13ctl-plus/z13gui+

Derived by comparing `z13ctl-plus --help` against hhd's settings tree.

| Capability | hhd | z13ctl-plus | Plugin-able? |
|---|---|---|---|
| **Refresh rate 60/180 Hz** | **no** (*"framerate TBD"*) | yes | **yes — the priority** |
| **Panel overdrive** | **no** | `paneloverdrive` | yes (asus-armoury attr) |
| **Boot POST sound** | **no** | `bootsound` | yes (asus-armoury attr) |
| **Display posture / rotation** | **no** | `display` | yes, but KDE-coupled |
| **Named presets + AC/DC policy** | **no** | `preset` | yes, but overlaps hhd's own TDP model |
| Undervolt (Curve Optimizer) | no | `undervolt` | **moot** — `ryzen_smu` absent on this machine |
| RGB / aura | yes | `apply`/`off`/`brightness` | — |
| TDP (PPT) | yes | `tdp` | — |
| Fan curve | yes | `fancurve` | — |
| Battery charge limit | yes | `batterylimit` | — |
| Performance profile | yes | `profile` | — |
| Status readout | yes | `status` | — |

**Refresh rate is the only gap that actually costs something day to day** — it
is the reason z13ctl-plus was adopted over hhd in the first place, and it is
unavailable inside gamescope where the QAM is the only control surface.

---

## Part 2 — What our patches actually are

Measured against the `.z13-orig` backups:

| Change | Files touched | Scale | Verdict |
|---|---|---|---|
| **QAM toggle** | `plugins/overlay/base.py`, `plugins/overlay/__init__.py` | **6 + 33 lines** (mostly comments) | **The only real patch. This is the fork.** |
| Wayland wrapper | none — uses `$HHD_OVERLAY` | — | **Legitimate.** A documented extension point hhd checks *before* its built-in search path |
| systemd drop-in | none — `hhd@.service.d/` | — | **Legitimate.** Standard systemd override |
| KWin placement script | none — `~/.local/share/kwin/scripts/` | — | **Legitimate.** Compositor-side, unrelated to hhd |

So three of the four pieces never touch hhd's code. **One does**, and it is the
one to eliminate.

---

## Part 3 — Recommendation

**Do not fork. Do not rewrite z13gui.** Three moves, in order of value:

### 1. Write `hhd-z13`, a plugin package (removes the *reason* to fork)

A separate Python package declaring:

```toml
[project.entry-points."hhd.plugins"]
z13 = "hhd_z13:autodetect"
```

Provides an `HHDPlugin` subclass whose `settings()` returns a schema for the
missing controls. Start with **refresh rate only**; add overdrive/bootsound once
the shape is proven.

Reuses existing, working logic:
* `~/Projects/z13-refresh-toggle.sh` — already resolves mode IDs **by content**,
  because they are unstable across sessions/hotplug (2560x1600@180 was id 24
  with an external display, id 1 without). Port that, do not re-derive it.
* `z13ctl-plus paneloverdrive --set` / `bootsound` — shell out rather than
  reimplement the asus-armoury writes.

### 2. Keep the toggle patch — DECIDED, with eyes open

**Decision: keep the local patch. Do not upstream it for now.**

This means the toggle stays an unversioned edit to
`plugins/overlay/{base,__init__}.py`, re-applied by the pacman hook. That is a
deliberate, accepted trade, so the cost should be stated plainly rather than
quietly carried:

* It **will silently stop working** if upstream refactors `loop_manage_desktop`
  or the `case "hhd_qam"` dispatch. The patcher refuses rather than corrupting
  anything — but the *symptom* is the button quietly reverting to open-only,
  not an error you will notice.
* **Mitigation:** after any `hhd` upgrade, check
  `python3 ~/Projects/hhd-patches/hhd-qam-toggle-patch.py --check`. Both files
  should read `PATCHED`. The pacman hook prints its result during the
  transaction, so a failure is visible *if you are watching the upgrade*.
* The plugin work in step 1 is **independent of this** — it removes the
  feature-gap reason to diverge, which is the larger half of the problem, and
  it does so with zero hhd edits.

Revisit upstreaming if an hhd upgrade ever breaks the patch: at that point the
patch has to be rewritten anyway, and a PR costs no more than a re-derive.

### Why not rewrite z13gui

z13gui-plus's defect was **not GTK** — it was misusing TTM (`bulk_move` entries
with mismatched `dma_resv`, `ttm_resource.c:235`). A different toolkit might
dodge it incidentally, but you would rebuild an entire UI to regain what hhd's
QAM already provides, **and still have no gamescope overlay** — that comes from
hhd's gamescope integration, not from the toolkit.

---

## Part 4 — The hard constraint to solve first

**hhd runs as root; refresh rate is a user-session operation.**

`z13-refresh-toggle.sh` uses `kscreen-doctor`, which needs the user's Wayland
session — root cannot reach it. And **inside gamescope there is no KScreen at
all**, so a KDE-only implementation would render a control that silently does
nothing in the one place the QAM matters most.

| Approach | Works in KDE | Works in gamescope | Cost |
|---|---|---|---|
| **Drop to the user session (`runuser` + `kscreen-doctor`)** | **yes** | **no** | **low** |
| DRM mode-setting directly | yes | **yes** | high — must not fight the compositor |
| Shell out to `z13ctl-plus display` | yes | no | low, but keeps the z13ctl-plus dependency |

**Decision: ship the user-session approach first, revisit DRM later.**

hhd already solves exactly this for the overlay itself —
`launch_overlay_de()` drops to the user's `uid`/`gid` — so **there is a
precedent in the codebase to copy** rather than invent.

**Known consequence, accepted:** the refresh control will appear in the QAM but
be **inert inside gamescope**, which is the one place the QAM is the sole
control surface. Verification step 6 exists to confirm exactly that, so the
limitation is measured rather than assumed.

To avoid a control that silently does nothing, the plugin should **detect the
absence of a usable KScreen session and disable/annotate the control** instead
of failing quietly — a visible "unavailable here" beats a switch that does
nothing when flipped.

---

## Part 5 — Files

| Path | Change |
|---|---|
| `~/Projects/hhd-z13/` | **new** package: `pyproject.toml`, `hhd_z13/__init__.py` (`autodetect`), `hhd_z13/settings.yml` |
| `~/Projects/hhd-patches/plan.md` | **new** — this plan, as the user requested, so it sits beside the patches it supersedes |
| `~/Projects/hhd-patches/README.md` | record that the toggle patch is a *deliberately retained* local edit, with the post-upgrade `--check` habit |
| `~/Projects/README.md` | record the plugin route and the fork reasoning |

Nothing under `/usr/lib/python3.*/site-packages/hhd/` is touched by the plugin.
Full path inventory is in **Where everything lives** at the top of this file.

---

## Verification

1. `pip install --user -e ~/Projects/hhd-z13` then
   `python3 -c "from importlib.metadata import entry_points; print([e.name for e in entry_points(group='hhd.plugins')])"`
   -> must list `z13` **without** any change to hhd.
2. `sudo systemctl restart hhd@termis` -> `journalctl -u hhd@termis` shows the
   plugin loading, no traceback.
3. Press the Armoury button -> the new control appears in the QAM.
4. Change refresh in the QAM -> `~/Projects/z13-refresh-toggle.sh status`
   confirms the panel actually switched.
5. **Upgrade simulation:** `sudo pacman -S hhd` (reinstall) -> plugin still
   loads with **no hook involvement**, proving the feature no longer depends on
   patching. In the same transaction, confirm the hook re-applied the toggle:
   `python3 ~/Projects/hhd-patches/hhd-qam-toggle-patch.py --check` -> both
   files `PATCHED`.
6. Launch gamescope, open the QAM -> **expected: the refresh control is present
   but inert** (no KScreen there). Confirm it is *visibly* marked unavailable
   rather than silently doing nothing. This is the measurement that decides
   whether DRM work is worth starting.
