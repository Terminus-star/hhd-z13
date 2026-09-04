# PLAN-NEXT — where the Z13 tooling actually is, and what to do next

> Written 2026-09-04. Successor to [`plan.md`](plan.md), which was written
> 2026-08-28 and is now roughly half executed, half overtaken, and wrong on
> three specific premises. Read Part 0 before trusting any path in `plan.md`.
>
> Everything below marked **measured** was read off this machine on 2026-09-04.
> Everything marked **given** was handed to this analysis and could not be
> verified locally (usually because the relevant git remote does not exist here).
> Everything marked **uncertain** is a guess and is labelled as one.

---

## Part 0 — Orientation, because the paths in `plan.md` are stale

`plan.md` says the patches live in `~/Projects/hhd-patches`. They do not, any more.

| What `plan.md` says | Where it actually is (measured) |
|---|---|
| `~/Projects/hhd-patches/` | `~/Projects/hhd-z13/hhd-patches/` — moved by `015a430` ("Reorganize: move hhd-patches content into hhd-patches/ subdirectory") and split out of the `Projects` superrepo by `6c185ee` |
| `~/Projects/hhd-z13/` is "new" | It exists, is an **independent git repo** (remotes `github:Terminus-star/hhd-z13`, `gitea:Terminustar/hhd-z13`), and is **not** tracked by `Projects` — `.gitmodules` lists only `z13-hibernate-plus` |

### The single most important structural fact

**The plugin code that is actually running on this machine is not in this repo.**

```
$ python3 -c "import hhd_z13; print(hhd_z13.__file__)"
/home/termis/Projects/.claude/worktrees/hhd-gamescope-vt-gate/hhd-z13/hhd_z13/__init__.py
```

An editable install (`/usr/lib/python3.14/site-packages/__editable__.hhd_z13-0.1.0.pth`,
root-owned, dated 3 Sep 19:44) points hhd at a **git worktree** on the branch
`worktree-hhd-gamescope-vt-gate` of the `Projects` superrepo. That branch is
**not merged into `Projects/master`** (`git merge-base --is-ancestor` says no),
and the worktree lives under `.claude/worktrees/`, a directory that tooling is
entitled to delete.

The two copies have diverged (measured):

| File | `~/Projects/hhd-z13/` (this repo) | worktree branch (live) |
|---|---|---|
| `plugin.py` | 285 lines | **606 lines** |
| `settings.yml` | 132 lines | **209 lines** |
| `display.py` | 546 lines | 546 lines, but differs |
| `gamescope.py` | 176 lines | identical |

All twelve CPU commits — `cfefd4e`, `5180ca4`, `d9ae417`, `69c89de`, `29d075d`,
`36b09ce`, `219874d` and the earlier `af0a845`…`dce9103` — exist **only** on the
worktree branch. This repo's `master` captured the plugin at an earlier state.

**Nothing else in this plan matters until that is resolved.** See thread T1.

### Live state snapshot, 2026-09-04 (measured)

```
product_name          ROG Flow Z13 GZ302EA_GZ302EA     sys_vendor ASUSTeK COMPUTER INC.
hhd                   4.1.12-1, hhd@termis active
z13ctl-plus daemon    inactive
AC online             0  (on battery)
ppt_pl1_spl           45      ppt_pl2_sppt 52     ppt_pl3_fppt 71
platform_profile      custom
amd_pstate/status     active
scaling_governor      powersave
cpufreq/boost         0
cpuinfo_max_freq      3000000        scaling_max_freq 3000000
panel_overdrive       0              boot_sound       1
ryzen_smu             loaded; /sys/kernel/ryzen_smu_drv present (drv 0.1.7, codename 26)
patch --check         4/4 PATCHED (base.py qam-toggle, base.py oneshot,
                                   __init__.py qam-toggle, x11.py vt-gate)
```

Two things in there contradict the brief and should be re-checked before acting:

* **TDP is 45 W, not 9 W.** The brief states "hhd is now the sole TDP writer and
  currently holds it at 9W (quiet)". On battery, right now, PL1 reads 45. Either
  hhd's state has moved since, or the 9 W observation was from a different
  profile. Do not build on the 9 W number without re-reading the file.
* **The machine is sitting in the 3000 MHz clamped state right now** — `boost=0`
  and `cpuinfo_max_freq=3000000`. This is exactly the condition the "3000 MHz
  mystery" describes, live. See trap K1.

---

## Part 1 — Honest assessment against `plan.md`

### Delivered, and more than was planned

| `plan.md` item | Status |
|---|---|
| Part 3 step 1: write `hhd-z13`, a plugin package | **Done.** `z13_cpu = hhd_z13:autodetect` is discovered alongside hhd's own 16 entry points, with zero hhd changes for the plugin itself (measured via `entry_points(group='hhd.plugins')`) |
| Part 1: "Refresh rate is the only gap that actually costs something day to day" | **Done**, `34da5a2`, and it ports the mode-ID-by-content resolution from `z13-refresh-toggle.sh` as instructed rather than re-deriving it |
| Part 1: display posture, presets | **Not done.** Deliberately? Unclear — no commit records dropping them |

Work that went well beyond `plan.md`'s scope, all on the unmerged branch:

* **The whole CPU control surface** — `af0a845` onward. This was not in `plan.md`
  at all. It now covers amd_pstate mode (`69c89de`), EPP/governor gated behind
  the mode (`29d075d`), boost as an AC/battery-following three-way (`36b09ce`),
  perf mode, and manual floor/ceiling behind an Auto/Manual gate (`dce9103`).
* **Adaptive-sync / VRR policy** (`36ffdff`), via the undocumented
  `kscreen-doctor output.eDP-1.vrrpolicy.<never|automatic|always>`.
* **A gamescope backend for the refresh control** (`8c4dc20`) that talks to
  gamescope's X atoms directly, plus a panel-refresh → gamescope-fps-cap bridge
  (`967540d`).

### Overtaken — `plan.md` conclusions that are now obsolete

* **Part 4's accepted consequence is void.** `plan.md` decided to ship the
  user-session (`kscreen-doctor`) approach and accept that the refresh control
  would be "present but inert inside gamescope", with verification step 6
  existing to measure exactly that. `8c4dc20` made it *work* in gamescope via
  `GAMESCOPE_DYNAMIC_REFRESH` / `GAMESCOPE_XWAYLAND_REFRESH_RATE_FEEDBACK`.
  **Verification step 6 should be deleted, not run.**
* **Part 4's "DRM mode-setting directly — high cost" row is no longer the only
  gamescope option.** The atom path cost ~176 lines (`gamescope.py`) and did not
  have to fight the compositor.
* **"Shell out to z13ctl-plus rather than reimplement" is being unwound in
  practice.** `cfefd4e` already drives sysfs directly whenever
  `energy_performance_preference` is absent, because `z13ctl-plus cpu` fails on
  *every* subcommand outside `amd_pstate=active` (its read path starts with EPP).
  The z13ctl-plus dependency is now a partial, mode-conditional one.

### Regressed against `plan.md`'s own stated goal

`plan.md`'s Context section is explicit: *"the red line was crossed when we
edited `plugins/overlay/__init__.py`. The goal of this plan is to get back across
it, not to go further."*

**We went further.** Measured, there are now four patched upstream files, not two:

```
/usr/lib/python3.14/site-packages/hhd/plugins/overlay/base.py.z13-orig
/usr/lib/python3.14/site-packages/hhd/plugins/overlay/__init__.py.z13-orig
/usr/lib/python3.14/site-packages/hhd/plugins/overlay/x11.py.z13-orig
/usr/lib/python3.14/site-packages/adjustor/drivers/gpu/__init__.py.z13-orig
```

The fourth is `z13-cpu-disable-native` (`af0a845`), which forces
`supports_boost`/`supports_epp` false in adjustor so hhd's native CPU controls
stop fighting the plugin. It is defensible — `af0a845` measured hhd's native boost
as broken ("Failed to set CPU boost:" with no error text) and its frequency floor
as never rendering — but it is still a fifth unversioned mutation of installed
files, and `hhd-qam-toggle-patch.py --check` currently reports only **four** marks,
so the fifth is not covered by the same self-check habit `plan.md` recommends.

### Wrong premises, now corrected

**W1 — "Undervolt … moot: `ryzen_smu` absent on this machine."** (`plan.md` Part 1)
False as of today. Measured: the `ryzen_smu` module is loaded and
`/sys/kernel/ryzen_smu_drv/` exists with `drv_version` 0.1.7, `codename` 26,
`mp1_if_version` 10.100.2.0. Whether Curve Optimizer writes actually *take* on
Strix Halo is a separate and **unmeasured** question — the presence of the
mailbox is not evidence that the CO command works. But the reason `plan.md` gave
for dismissing undervolt no longer holds.

**W2 — the user-session discovery design in Part 4.** `plan.md` said to copy
hhd's `launch_overlay_de()` precedent and drop to the user's uid. The first
implementation did something adjacent — scanned `/proc` for `kwin_wayland` and
read its `/proc/<pid>/environ` — and it **failed in production**, not just in
testing. `b05d703` root-caused it twice over: `ptrace_scope=1` gates cross-process
`environ` reads even same-uid, *and* `WAYLAND_DISPLAY` is not in `kwin_wayland`'s
environment at all, because KWin **creates** the socket (`--socket wayland-0` on
its cmdline) rather than inheriting it. The working design is
`loginctl show-session` + `/proc/<pid>/cmdline`.

**W3 — the 3000 MHz ceiling, wrong twice.** `60c1d05` framed it as an
`amd_pstate=active` property ("scaling_max_freq already equals cpuinfo_max_freq;
nothing is capped"). `cfefd4e` refined it to "in active mode scaling_max_freq is
clamped to the nominal 3000 MHz while guided and passive expose 5187". Both are
wrong. `36b09ce` has the answer, with a measurement:

```
boost 1 -> 0   cpuinfo_max 5187 -> 3000, scaling_max 5187 -> 3000
boost 0 -> 1   cpuinfo_max 3000 -> 5187, scaling_max 3000 -> 5187  (recovers)
```

A cpufreq policy's maximum is fixed when the policy is **created**. Boost off at
*boot* bakes nominal in irreversibly for that policy; boost toggled at *runtime*
is reversible. It is not a mode property.

**W4 — "shell out to `z13ctl-plus paneloverdrive` / `bootsound` rather than
reimplement".** More work than writing it directly. Both are one sysfs
enumeration each under `asus-armoury`, and in z13ctl-plus itself they are
**~9 lines apiece** (`internal/cli/sysfs.go:125-143`). Measured, readable right now:

```
/sys/class/firmware-attributes/asus-armoury/attributes/panel_overdrive/current_value  = 0
/sys/class/firmware-attributes/asus-armoury/attributes/boot_sound/current_value       = 1
possible_values = 0;1   type = enumeration
```

**W5 — the controller-shortcut premise in the brief needs qualifying.** See T5.

---

## Part 2 — Open threads

### T1 — Reconcile the two `hhd_z13` copies and get the live install off the worktree
**Effort:** small (an hour), but touching a live root-installed package.
**Depends on:** nothing. **Blocks:** everything else in this document.

The running code is an editable install pointed at an unmerged branch inside
`.claude/worktrees/`. Deleting that worktree breaks hhd's plugin loading on the
next restart; `git worktree prune` or a tooling cleanup would do it silently.
This repo's `master` has the *older* plugin and the *newer* `hhd-patches/`.

Suggested shape (not yet validated): move the worktree branch's `hhd_z13/` into
this repo as a commit on `hhd-z13:master`, re-run
`sudo pip install --break-system-packages --force-reinstall -e ~/Projects/hhd-z13`,
confirm `hhd_z13.__file__` now points inside `~/Projects/hhd-z13`, restart
`hhd@termis`, and only then remove the worktree. Keep the branch until the new
install is confirmed working — the failure mode (`0da92ff`) is hhd not coming
back at all.

**Why it matters:** every other thread here writes code that would land in
whichever copy you happen to be looking at.

### T2 — Port `paneloverdrive` and `bootsound` into the plugin
**Effort:** very small (~40 lines of Python and two YAML blocks).
**Depends on:** T1.

Two `bool` controls in `settings.yml`, writing
`/sys/class/firmware-attributes/asus-armoury/attributes/{panel_overdrive,boot_sound}/current_value`.
hhd runs as root, so no shellout and no z13ctl-plus dependency. Follow the
established shape in `plugin.py`: adopt silently on the first tick, write only on
a change against `old_*`, and **read back to verify** — `69c89de` records that
these firmware-attribute files accept a write and keep the old value for anything
the firmware declines.

Read `possible_values` rather than hardcoding `0;1`; it is exposed and free.

**Why it matters:** it is the cheapest remaining item on `plan.md`'s Part 1 gap
table, and finishing it lets `bootsound` and `paneloverdrive` be dropped from the
z13ctl-plus dependency surface entirely, which feeds T4.

### T3 — Undervolt (Curve Optimizer)
**Effort:** large, and open-ended. **Depends on:** T1; and on a measurement
that has not been done.

Not a sysfs write. `internal/cli/smu.go` (142 lines, ~60 of them protocol) drives
a binary mailbox: write 24 bytes (6×u32 LE) to
`/sys/kernel/ryzen_smu_drv/smu_args`, write a 4-byte command id to
`mp1_smu_cmd`, read back a 4-byte response and the 24-byte reply. Value encoding
(`encodeCOValue`, `smuCmdMP1COALL`) and the safety clamp (`ValidateCOValues`,
0 to −40, matching G-Helper) live elsewhere in `internal/cli` and were not fully
traced.

**Do the measurement before the port.** The question is not "can Python write
those files" (it can) but "does the CO-ALL command do anything on Strix Halo".
`z13ctl-plus undervolt --get` against the loaded `ryzen_smu` answers it in one
command, and costs nothing. If the answer is no, this thread closes permanently
and `plan.md`'s dismissal turns out right for a different reason.

**Why it matters:** it is the only remaining z13ctl-plus capability with real
day-to-day value that the plugin cannot trivially absorb. It is also the only one
that can damage stability if it half-works.

**Risk note:** an undervolt that is applied but not verified is the worst kind of
instability — it presents as random crashes weeks later, with no log line
connecting them. Whatever is built here needs a read-back and a "reset on boot"
story before it is left running unattended.

### T4 — The fork/upstream decision on `z13ctl-plus`
**Effort:** the decision is cheap; the merge is a day. **Depends on:** T2, T3
(they change how much you still need the fork for).

State (measured):

* Remotes are `origin → aic0d3r/z13ctl-plus`, plus the user's own `github` and
  `gitea` mirrors. **There is no remote for `dahui/z13ctl` at all.** So the
  brief's measurements about rebasing/merging against dahui cannot be reproduced
  here without adding one.
* `go.mod` requires `github.com/dahui/z13ctl/api v1.1.6` but immediately
  `replace`s it with `./api` — the "upstream dependency" is a vendored local
  directory, not a live coupling. Adopting upstream `api` changes is a manual
  diff, today.
* The branch `feat/per-profile-tdp-button-debounce` is exactly 3 commits ahead of
  `main` with no divergence; `origin/main` last moved 2026-08-05.

**Given** (unverified here): dahui is 32 commits ahead of the fork point and
active; rebase stops at commit 1 of 45; merging costs 32 conflicted files once;
"reopen the HID device on resume" cherry-picks cleanly.

The decision is not really "rebase vs merge". It is **how much z13ctl-plus you
still intend to run at all**, and right now the answer is trending toward "not
much":

* its daemon is `inactive` (measured);
* AC/battery preset switching is off (`power_policy.enabled: false` in
  `~/.local/state/z13ctl-plus/state.json` — a **state-file setting, reversible
  with `preset policy --enable`, not a code change**);
* the Armoury button is already conceded to hhd via a systemd drop-in
  `~/.config/systemd/user/z13ctl-plus.service.d/10-no-button.conf` passing
  `--no-button`;
* CPU control already bypasses it outside `amd_pstate=active`.

**Recommendation:** do not merge 32 conflicted files to maintain a fork you are
in the middle of retiring. Do the one cheap, high-value thing — cherry-pick the
resume/HID fix — and defer the rest until T2/T3 have established what, if
anything, still needs the Go daemon. If the answer after T2/T3 is "only
undervolt", the right end state is not a merged fork but a small, single-purpose
binary, or nothing.

### T5 — The hhd device-entry gap, and what it actually costs
**Effort:** the correction below is free; an upstream device entry is a
medium-sized measurement job. **Depends on:** nothing.

**The premise as stated in the brief is partly wrong, and the correction matters
because it changes the priority.** The brief says the Z13's absence from hhd's
device list means "controller combos cannot open the QAM". Measured against
hhd 4.1.12's actual source:

* `hhd/device/generic/const.py` `CONFS` has **33 entries** — confirmed — and the
  Z13's `product_name` (`ROG Flow Z13 GZ302EA_GZ302EA`) is not one of them, nor
  does it hit the vendor fallbacks in `device/generic/__init__.py:113-140`, which
  cover only `ayn`, `tectoy` and `ayaneo`. So no `GenericControllersPlugin` loads.
* **But the QAM shortcut paths do not depend on that.** In
  `hhd/plugins/overlay/__init__.py:244-245` the controller shortcut loop is
  gated by a hardcoded `ctrl = True` with the comment *"For now always monitor
  controllers to be able to grab"*. External-controller combos
  (`shortcuts.controller.xbox_b` → `hhd_qam`, etc.) run regardless of device
  detection. Independently corroborated by `wine-hidpi/FINDINGS.md` Finding 2y,
  which records hhd's Guide chords *intercepting input before Steam* on this
  machine — i.e. they demonstrably work here.
* The **Armoury button** shortcut is likewise enabled: `__init__.py:133-145`
  gates it on `"ASUSTeK COMPUTER INC" in sys_vendor and "ROG Ally" not in
  product_name`, which is true here.
* **Touchscreen** shortcuts are gated on `has_touchscreen()` probing evdev, not
  on DMI, and the Z13 has one.

So what does CONFS membership actually buy? Controller *emulation* (fusing a
built-in gamepad into one virtual output), gyro/IMU config, and the built-in
controller's own `qam_hhd` button. **The Z13 has no built-in gamepad**, so most
of that is inapplicable. It does have an IMU (measured: `iio:device0`,
`accel_3d`, HID sensor on `ITE8353`), which is the one genuinely missing piece —
and it is relevant to tablet rotation, not to the QAM.

**Revised framing:** this is not a "the QAM is unreachable" bug. It is a
"the Z13 is not officially supported upstream" gap, whose real content is
IMU/orientation and the recognition itself. Treat it as a **contribution**
opportunity, not a blocker.

**Why it still matters:** Bazzite ships hhd by default; a merged device entry is
the difference between this work being local tooling and being something a Z13
owner gets for free. But it is not on the critical path for anything the user
does day to day.

### T6 — Telemetry surfacing: `mass-effect-hud` vs the QAM
**Effort:** the decision is free; any convergence work is medium.
**Depends on:** nothing, but is only worth doing after T1/T2.

`mass-effect-hud` (`src/main.rs`, 652 lines, 6 commits) reads CPU total and
per-core %, CPU MHz, CPU temperature, RAM and swap — all from `/proc/stat`,
`/proc/meminfo`, `cpufreq/scaling_cur_freq` and `/sys/class/hwmon`. No root, no
D-Bus, no IPC to anything. It is a `gtk4-layer-shell` client anchored at
`Layer::Bottom`, which makes it a **KDE Plasma desktop widget** — gamescope does
not expose `wlr-layer-shell` to arbitrary clients, so it will not work in the
session where a HUD is most wanted.

Three surfaces currently show overlapping numbers and none of them is the
authority:

1. `mass-effect-hud` — desktop only, sysfs-polled, no power/TDP data at all;
2. hhd's QAM — root, has the actual TDP/profile/battery state, works in
   gamescope, but is a menu you open, not a HUD;
3. MangoHud, driven directly by Steam's own sidebar
   (`~/.local/share/Steam/config/mangohud.conf`, `control=mangohud`) —
   in-game only, and per `wine-hidpi/GAMESCOPE.md` `mangoapp` is broken nested.

**Recommendation: do not converge them.** They serve three disjoint contexts and
the only real duplication (CPU%/temp/RAM between 1 and 3) never appears on screen
at the same time. What *is* worth doing, cheaply, is adding the two numbers
`mass-effect-hud` is missing and that only hhd knows: **current PL1 and the
active profile**. Both are plain sysfs
(`/sys/class/firmware-attributes/asus-armoury/attributes/ppt_pl1_spl/current_value`,
`/sys/firmware/acpi/platform_profile`) — no hhd IPC needed. **Uncertain:**
whether `ppt_pl1_spl/current_value` is world-readable; the legacy `asus-nb-wmi`
copy is `rw-rw-r--` so probably yes, but check before designing around it.

### T7 — The display plugin still spawns a process every 16 seconds
**Effort:** small. **Depends on:** T1.

`46b0a6f` reduced `kscreen-doctor` spawns from two every ~4 s to one every 15 s
and called it done. Measured in the journal today, 14:10:29 → 14:15:40, hhd is
still emitting one line per ~16 s, indefinitely:

```
kscreen-doctor[70542]: Detected locale "C" with character encoding
                       "ANSI_X3.4-1968", which is not UTF-8.
```

That is roughly 5,400 process spawns and 5,400 journal lines per day, forever,
for a reconcile loop that exists only to notice out-of-band changes nobody makes
at that cadence. Two independent fixes, either of which is worth taking:

* set `LC_ALL=C.UTF-8` on the spawn to stop the log line (cosmetic, one line);
* drop the poll further, or make it event-driven. hhd already knows when the QAM
  opens; reconciling on QAM-open plus a slow 60 s floor would be nearly free.

**Why it matters:** low, but it is the kind of thing that makes the journal
useless for diagnosing the *next* problem, and this project diagnoses a lot of
things from the journal.

### T8 — Upgrade fragility: five unversioned edits, four covered by `--check`
**Effort:** small. **Depends on:** nothing.

`plan.md` Part 3 step 2 accepted the toggle patch as a deliberate local edit with
a post-upgrade `--check` habit. That bargain has since been extended to
`x11.py`, `base.py`'s one-shot latch, and `adjustor/drivers/gpu/__init__.py` —
but `--check` still prints only four rows. Add the fifth mark
(`z13-cpu-disable-native`) to the checker so the habit covers what actually
exists. Also confirm the pacman hook fires for adjustor: `af0a845` states
`pacman -Qo` puts adjustor in the `hhd` package, so the existing hook should
cover it, but that has not been observed across a real upgrade.

### T9 — `z13-hibernate-plus`: `plan-hibernate-gpu-gate.md` has not started
**Effort:** varies. **Depends on:** nothing. **Recommendation: leave it.**

Cross-checked against the repo: **none of parts A–E have landed.** Part D
(hardcoded `gunther` username, 5 call sites in `gate-hook.sh`,
`s2idle-resume-fixup.sh`, `hibernate-hook.sh`, `common.sh`) is still live —
and `NOTICE` now explains why it looked odd: `gunther` is the upstream author's
username, a fork leftover, not a mystery. Part C (`charge_control_end_threshold`
re-apply) has no matching code. The one thing that did get fixed on 2026-09-03
(`f55ad40`, the mt7925e reload) was a *different* bug in the same file, found
after that plan was written.

This is a separate project with its own plan document. It is listed here only so
it is not mistaken for done. The one item in it that has actually broken a
working system is the WiFi outage, and that is already fixed.

---

## Part 3 — Recommended sequence

### Do first

**1. T1 — consolidate the plugin copies and repoint the install.**
Everything else edits code that currently exists in two places, one of which is
live and neither of which is authoritative. It is also the only item here with a
*silent catastrophic* failure mode: a worktree cleanup takes hhd's plugin with it
and the symptom is "the QAM controls vanished", with nothing in the journal
pointing at a deleted directory. Do this before writing another line of plugin
code.

**2. T3's measurement only — `z13ctl-plus undervolt --get`.**
One command. It either opens the largest remaining port or closes the thread
permanently, and it is the input to T4's real question ("what still needs the Go
daemon?"). Cheap information that gates an expensive decision should always come
first. Do **not** start the port on the same day.

**3. T2 — port `paneloverdrive` and `bootsound`.**
~40 lines against two files you can already read. Finishing it clears two rows
off `plan.md`'s Part 1 gap table and shrinks the z13ctl-plus surface to
`undervolt` + `preset` + `tablet`, which makes T4 answerable.

### Then, in this order

4. **T8** — extend `--check` to the fifth mark. Ten minutes, and it protects
   everything above from the next `pacman -Syu`.
5. **T4** — with T2 and T3's measurement in hand, decide. Cherry-pick the resume
   HID fix regardless; hold the 32-file merge unless something concrete needs it.
6. **T7** — the polling/log-spam cleanup, once the code has one home.

### Defer

* **T5's upstream device entry.** Real value (Bazzite), no day-to-day cost, and
  the QAM already works. It is a contribution, and contributions should be made
  from a stable base, not from a branch in a worktree.
* **T6's telemetry work beyond adding PL1 + profile to the HUD.** The three
  surfaces are genuinely disjoint.
* **T9 entirely.** Different project, own plan, nothing currently broken.

### Drop

* **`plan.md` verification step 6** ("confirm the refresh control is visibly
  marked unavailable in gamescope"). Obsolete — `8c4dc20` made it work there.
* **The `plan.md` Part 4 DRM mode-setting investigation.** The X-atom path
  solved the same problem at a fraction of the cost.
* **Porting `tablet` from z13ctl-plus.** There is nothing there to port:
  `cmd/tablet.go` is 131 lines of IPC passthrough and `projectTabletSettings`
  (23 lines) writes a JSON file for a *different repo* (`aic0d3r/z13-tablet-kit`,
  not present locally) to poll. The hardware logic was never in z13ctl-plus.
* **Porting `preset`.** `internal/daemon/preset.go` is 567 lines, its AC/DC
  policy is already disabled for fighting hhd, and hhd has its own profile model.
  Re-importing it would recreate the exact conflict that was just resolved. The
  one piece worth keeping was the refresh-rate-follows-power-source behaviour,
  and `36b09ce`/`display.py` already rebuilt that inside the plugin without
  going near TDP.

---

## Part 4 — Known traps

Recorded so they are not re-derived. Each is something that already cost time.

### Hardware and kernel

**K1 — The 3000 MHz ceiling is a boot-time property of the cpufreq policy, not a
mode.** Wrong twice (`60c1d05`, `cfefd4e`) before `36b09ce` measured it. A
policy's maximum is fixed when the policy is created; boost off at *boot* bakes
the nominal clock in for that policy's lifetime. Runtime boost toggling is
reversible (5187↔3000 measured both ways). **This machine is in the clamped state
right now** (`boost=0`, `cpuinfo_max_freq=3000000`), so anyone re-measuring will
reproduce the symptom immediately and may reach the same wrong conclusion a third
time.

**K2 — Never have more than one writer near cpufreq during an amd_pstate mode
switch.** Two hard lockups, neither leaving a kernel fault — the journal simply
stops mid-line (`29d075d`). The second had hhd's adjustor writing a frequency
range while z13ctl-plus retried a power preset while the driver rebuilt every
policy. Mitigation in the plugin is `PSTATE_SETTLE_S`, a settle window after a
mode switch during which nothing else is written and `old_epp`/`boost`/`min`/`max`
are cleared as stale. **Do not shorten or remove it.**

**K3 — Firmware-attribute and pstate sysfs files accept writes they ignore.**
`69c89de`: `amd_pstate/status` takes a write and keeps its old value for anything
the driver declines. Always read back. The same discipline is already applied in
`set_vrr`, `set_refresh` and `set_fps_limit`.

**K4 — Ceiling before floor.** Raising `scaling_min_freq` against a lower
`scaling_max_freq` is rejected. z13ctl-plus did this ordering internally; the
direct-sysfs path in `cfefd4e` has to do it explicitly, and `2e33a02` batches
both sliders into one call for the same reason.

**K5 — Boost silently governs the ceiling.** With boost off, a raised frequency
ceiling is accepted and clamped to nominal, with nothing surfacing the conflict
(`5180ca4`). `_apply_freq` now enables boost before writing a ceiling that needs it.

**K6 — `userspace` governor is a trap in a QAM.** The driver offers it; selecting
it hands control to a `scaling_setspeed` nobody writes, so the CPU stays put and
the control looks broken (`5180ca4`). Filtered out.

**K7 — `amd_pstate` mode resets to the kernel cmdline on every boot.** A runtime
switch is normally throwaway. It sticks only because hhd re-applies its own state
at startup (`69c89de`). If the plugin is ever removed, the mode silently reverts.

**K8 — YAML 1.1 parses bare `on`/`off` as booleans.** Cost a silently broken
string comparison (`219874d`). Quote them.

### Process discovery and privilege

**K9 — Do not read another process's `/proc/<pid>/environ`.** `ptrace_scope=1`
gates it even same-uid, and this failed in *production* under the root daemon,
not only in testing (`b05d703`). Worse, the value you want may not be there at
all: `kwin_wayland` **creates** the Wayland socket (`--socket wayland-0` on its
cmdline) rather than inheriting it. Use `loginctl show-session` and
`/proc/<pid>/cmdline`, neither of which is ptrace-gated.

**K10 — Discover gamescope's display by X atom, not by environment or socket
glob.** Probe every socket in `/tmp/.X11-unix` for `GAMESCOPE_PID`, which
gamescope sets on its own root window and nothing else does, and check that pid
against `/proc` so a stale socket from a dead session is never adopted
(`8c4dc20`). Confirmed live: KDE's XWayland `:0` has no such atom; gamescope's
`:1` does.

**K11 — Do not extend `hhd-ui-wayland`'s socket glob to match
`gamescope-<digits>`.** Tried 2026-08-28, made things worse, reverted the same
day (`31286b2`). On gamescope the X11 fallback is *correct* — hhd needs an X11
window to find, so a native Wayland client on `gamescope-0` guarantees
`find_hhd` can never succeed. `UI Window not found` was already in the log
*before* that change, which is what proved the wrapper was never at fault.
Decide the backend from hhd's own env (`STEAM_OVERLAY` → X11,
`HHD_MANAGED` → Wayland), per `ede0188`.

**K12 — A backgrounded subshell inside a `systemd-sleep` hook is killed when the
hook exits.** Its cgroup is torn down mid-flight, swallowing both the success and
failure log lines, so the failure is invisible (`27606a0`, and `f55ad40` in
`z13-hibernate-plus`). Use `systemd-run --no-block` in a transient unit. Paired
trap: **clear a retry flag only on success**, or one lost attempt becomes
permanent for the rest of the boot.

### hhd internals

**K13 — `settings()` must return category → section-name → Container, not
category → Container.** One level short crashes hhd's *merge* step, not the
plugin, with `AttributeError: 'str' object has no attribute 'get'` in
`merge_reduce`, and hhd does not come back after the install (`0da92ff`). Match
adjustor's own shape: `{"tdp": {"amd_energy": ...}}`. Recovery if it happens:
`sudo pip uninstall -y hhd-z13 && sudo systemctl restart hhd@termis` — the crash
is before any `update()`, so no hardware state was touched.

**K14 — hhd builds `settings()` once at startup.** A control can outlive the
driver that had it (switch out of `active` and the EPP control is still drawn but
`energy_performance_preference` no longer exists). `69c89de`/`29d075d` handle
this by detecting the mismatch and declining to write until hhd restarts. Any new
control whose *existence* depends on hardware state inherits this problem.

**K15 — `launch_overlay_de()` builds a minimal environment from scratch**
(`HOME`, `XAUTHORITY`, `DISPLAY`, `HHD_MANAGED`). Nothing set on hhd's systemd
unit reaches the UI. `HHD_OVERLAY` *is* honoured, and is checked before hhd's
built-in search path — that is why the wrapper works without patching hhd-ui.

**K16 — Never pass `--class` to hhd-ui on gamescope.** Electron sanitises dots to
dashes for the real window but applies `--class` verbatim to a 10×10 helper
window, and `find_hhd()` tries the dotted name first — so every overlay atom
lands on the stub and the real window is never composited (`5364ac8`,
`hhd-patches/README.md`). It is still required on Plasma, where the KWin rule
matches app-id. Pass it only on the desktop path.

**K17 — Do not re-add `DBUS_SESSION_BUS_ADDRESS` to the wrapper.** Measured: it
makes startup *slower*, 0.56 s → 0.87 s, because a working bus makes Electron
perform portal lookups instead of failing fast. The `dbus/bus.cc:405` errors are
cosmetic (`5c542f9`).

**K18 — Closing the QAM: `terminate()`, do not send `cmd:close`.** Four attempts
recorded in `~/Projects/README.md` "hhd OVERLAY WORK". `loop_manage_desktop`
never parses `stat:` at all (proven: zero `stat:` lines in the entire journal),
and `OverlayService.update()` calls `_open_overlay()` *before* writing the
command, so a close races its own reopen — measured 5 s and 3 presses.

**K19 — A leftover gamescope session on a background VT breaks the overlay in
KDE.** Switching VTs does not end the session; it stays `online` holding
`/tmp/.X11-unix/X0` and `X1`, so hhd's process-based check still says "in
gamescope" and searches the wrong display, killing and relaunching the UI on a
~10 s loop. Fixed by `z13-vt-gate` + `z13-oneshot`, but **`pgrep -a gamescope` is
still the one-line triage whenever the overlay misbehaves in Plasma** — the
symptom looks nothing like the cause.

**K20 — `aboverule=3` (Force keep-above) outranks plasmashell.** KDE panels could
not draw over the overlay. Reverted along with borderless (`b23b590`), because
without a titlebar there was no easy way to move or close a window already
dominating the screen. `skiptaskbar` is deliberately **off** so a misbehaving
overlay stays force-closable.

### Cross-repo

**K21 — Two daemons must not write the same knob.** This is the root of the whole
consolidation. Currently resolved by three separate, independently-toggleable
mitigations, all of which are *configuration* and can be silently undone:
`power_policy.enabled: false` in `~/.local/state/z13ctl-plus/state.json`;
`--no-button` via `~/.config/systemd/user/z13ctl-plus.service.d/10-no-button.conf`;
and the `z13-cpu-disable-native` patch to adjustor. None of the three is in a
repo that gets committed. **If TDP or the Armoury button starts fighting again,
check all three before debugging code.**

**K22 — Steam Input draws its own cursor, and hhd grabs evdev below Steam.**
Two separate input-layer traps from `wine-hidpi/FINDINGS.md`: an on-screen cursor
problem that no display/DPI knob can reach (Finding 2y / "RULE OUT STEAM INPUT
FIRST"), and hhd's Guide-chord interception, which happens *below* Steam and is
therefore unaffected by whether Steam is nested in gamescope (Finding 2aa).

**K23 — Measurement hygiene, from `wine-hidpi/GAMESCOPE.md`.** Two wrong
conclusions were reached by measuring the wrong thing: gamescope's refresh state
is meaningless until something is actually presenting through it (measuring with
`sleep` gave a false "pins to 60"), and panel VRR capability must be checked with
`modetest -c`, never sysfs (the sysfs absence produced a false "not VRR-capable").
Both were later corrected in place.
