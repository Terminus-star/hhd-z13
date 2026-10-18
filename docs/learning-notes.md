# Learning notes

Short entry per task: what changed, why, and one concept worth understanding.

## 2026-10-19 — keyboard navigation for hhd-ui (q/e bumpers, visible highlight)

**What changed.** hhd-ui now has a keyboard build: arrows, Enter and Escape act
as D-pad, A and B; Tab / Shift+Tab step through settings; q / e switch tabs like
LB / RB; and the focused item gets a gold ring and glow. It is built from the
v3.4.2 source plus `hhd-patches/hhd-ui-keyboard.patch` by
`hhd-patches/build-hhd-ui`, installed under
`~/.local/share/hhd-z13/hhd-ui/`, and chosen by `hhd-patches/hhd-ui-wayland`
(falling back to the packaged asar when no build exists).

**Why.** The installed 3.4.2 has controller navigation but no keyboard mapping;
upstream added one after the release (commit 12b62e6) and it is unreleased.
Tab only moved the browser's DOM focus, which this UI never draws.

**Concept: two separate "focus" systems.** The browser has DOM focus (what Tab
moves, `document.activeElement`). hhd-ui keeps its own selection in Redux
(`navigation.curr`), and only that drives the controller highlight. Making the
keyboard "work like the controller" meant translating keys into the controller's
commands, not styling DOM focus. Related: match physical keys with
`event.code`, not `event.key`, or shortcuts break on a non-Latin layout.

**Testing lesson.** A headless window renders no frames, so CSS transitions
never advance and computed styles stay at their start value. The first probe
reported "no highlight" for that reason alone; turning transitions off in the
page made the real, settled styles visible.

**Not deployed yet.** `/usr/local/bin/hhd-ui-wayland` is a root-owned copy, so
the new wrapper takes effect only after it is installed with sudo.

**Follow-up: install the right file.** The first `sudo install` used a relative
path from the main checkout, so it re-installed the OLD wrapper (the checksum
matched the main checkout's copy, not the worktree's). Always give an absolute
path when work lives in a worktree, and verify with `sha256sum`.

## 2026-10-19 — header buttons (Y / close) reachable by keyboard

**What changed.** The Y button (switch QAM / expanded view) and the close button
are now items in the navigation list, so Tab / the arrows reach them, ← / →
step between them, Enter acts, and they get their own highlight (gap ring + gold
ring + glow). The patch grew to seven files; initial focus is still the first
real setting.

**Concept: pseudo-items in a selection model.** Instead of special-casing the
buttons in the key handler, they became extra entries in `navigation.choices`
with reserved paths (`@header-mode`, `@header-close`). Everything that already
walks that list (goNext / goPrev, the highlight hook) then works unchanged; only
"activate" needed a special case. The names have no dot on purpose: setting
paths are `section.name`, and a dotless path resolves safely to "no setting".

**Testing lessons.** (1) A headless Electron window is 4x4 pixels, so nothing
renders or receives pointer events until a viewport is forced with
`Emulation.setDeviceMetricsOverride`. (2) Electron's zoom factor makes the page
lay out in a much bigger CSS space, so screenshots need CSS `zoom` to undo it.
(3) A screenshot beats a computed style for judging "is this obvious": it showed
at once that a plain gold ring would vanish on a gold button.

**"Highlight broke touch and clicks": not reproduced, cause unknown.** In overlay
mode, driven the way hhd drives it (stdin commands), mouse clicks and touch taps
worked 8/8 on the packaged build and on both keyboard builds. They were later
reported working again on the real session, but the cause of the earlier
failure was never identified, so treat it as open if it returns. The quick way
to separate the UI build from everything else is an A/B: rename
`~/.local/share/hhd-z13/hhd-ui/current` and relaunch, which falls back to the
packaged UI.

## 2026-10-19 — dropdown submenus and WASD

**What changed.** Choosing inside an opened dropdown now works from the keyboard
(arrows, and W / S), and W A S D act as a second D-pad everywhere (Q / E stay the
bumpers). All matched by physical key, so they work on any layout.

**Cause (my own regression).** Opening a dropdown shows `EditModal`, a Chakra
`Modal`, and focuses the highlighted choice. My earlier "leave keys alone inside
a dialog" guard therefore saw every key press come from inside a dialog and
ignored it. The fix lifts the guard while a selection is open
(`selectHasSelection`); other dialogs are still left alone.

**Concept: a guard has to be tested against the thing it was not meant for.**
The guard protected ordinary dialogs, but the controller's own choice list is
also a dialog. Reproducing first (the same script before and after) showed the
failure and the fix in one table: the selection never moved, then it moved on
every key.

**Testing lessons.** (1) Cancel with Escape and never press Enter inside the
modal, so a test cannot change a live setting. (2) framer-motion animations need
animation frames, so a headless dialog stays invisible; force it visible in the
page before a screenshot. (3) The CSS zoom used to make screenshots legible also
scales `100vw / 100vh` containers, so a centred dialog lands far off the forced
viewport: capture with `captureBeyondViewport`.

## 2026-10-19 — Ctrl+3 / Ctrl+4 stopped working (keyd)

**Cause.** Not the UI build. hhd reads these shortcuts straight from keyboard
devices, but its rule for "this is a Keyboard" needs ONE device with Meta, Ctrl,
3, 4 *and* the Armoury key (`KEY_PROG3`). On this laptop the keys are split over
different input nodes, so no physical device qualifies; only a *virtual*
keyboard that re-emits everything can. hhd's own log shows who played that role:
gsr-ui's virtual keyboard until 2026-10-18 16:41, then keyd's. keyd was stopped
at 00:26:49 (the Armoury test), the device vanished, and hhd has had no Keyboard
device since, so Ctrl+3/4 and the Meta shortcuts died together.

**The trade-off that caused it.** keyd also grabbed "Asus WMI hotkeys" and hid
the Armoury button from hhd. Stopping keyd fixed Armoury but removed the
keyboard device. The fix that keeps both is to leave keyd running and exclude
that one device (`-0000:0000` under `*` in `[ids]`).

**Concept: devices are a graph, not one keyboard.** `/proc/bus/input/devices`
lists many nodes per physical keyboard, each with its own capability bitmap. A
program that matches "a device that has all of these keys" can fail on hardware
where the keys are spread out, and a remapper like keyd silently becomes part
of the path by presenting one combined virtual device.

**Process lesson.** Check the log timeline before blaming the last thing you
changed: the failure lined up to the second with stopping keyd, not with the UI
rebuild. Also a correction to an earlier note of mine: the editable install maps
`hhd_z13` to the MAIN checkout (`/usr/lib/.../__editable___hhd_z13_*_finder.py`),
not to a worktree. My earlier check ran from inside the worktree, where the
current directory shadowed the install. A worktree's code is live only after it
is merged.

## 2026-10-19 — consolidating onto master

Everything was already merged except one branch whose only unique commit was
superseded. This session's uncommitted work was moved into the main checkout
(master) so it can be staged and committed there. See the final report for the
cleanup commands; deleting worktrees and branches is left to you.
