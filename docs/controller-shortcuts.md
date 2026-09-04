# Why no Xbox button combination opens the hhd QAM

Measured on the GZ302EA with an **external** Xbox Wireless Controller over
Bluetooth (`045e:02fd`, `event25`/`js0`), Steam running under gamescope.

Symptom: no Guide+button chord opens the hhd overlay, but once the overlay is
open the controller navigates it perfectly.

## It is not the `xbox_x` bug

There *is* a real upstream bug — `plugins/overlay/__init__.py` has

```python
case "xbox_b":  ...   # line 321
case "xbox_y":  ...   # 324
case "xbox_a":  ...   # 327
case "xbox_b":  ...   # 330  <- unreachable duplicate, should be "xbox_x"
```

so `shortcuts.controller.xbox_x` can never dispatch. The *producer* side is
fine: `overlay/controllers.py:547` emits `f"xbox_{ev}"` for all of a/b/x/y.

**But fixing it would change nothing here**, because the events never reach hhd
at all. `xbox_y` is correctly handled at line 324, is bound to `hhd_qam` in
`state.yml`, and still does nothing.

## The actual cause: Steam holds an exclusive grab

```
$ fuser -v /dev/input/event25
                     USER        PID ACCESS COMMAND
/dev/input/event25:  termis     3240 f.... gsr-ui
                     termis   639309 F.... steam     <- F = opened for writing
```

Steam opens the pad read-write and `EVIOCGRAB`s it. An exclusive grab means
every other reader — hhd included — receives nothing.

Everything else checks out, which is what makes this confusing:

- hhd **does** enumerate the device:
  `- 'Xbox Wireless Controller' [045e:02fd] (Controller[ukn=0500...])`
- the device **does** advertise the right buttons. `KEY=7fff000000000000 0
  100040000000 0 0` sets bits 304–318, i.e. `BTN_A`(304) through
  `BTN_THUMBR`(318), so `BTN_MODE`(316) — what `CONTROLLER_WAKE_BUTTON` needs
  for the `mode` half of the chord — is present.
- the device is **not** hidden. hhd drops controllers whose group-read bit is
  cleared (`controllers.py:875`, "Removing hidden device"); `event25` is
  `crw-rw---- root:input` and that log line has never appeared.
- so hhd opens it, reads it, and gets an empty stream. `process_ctrl` never
  advances past `if not state.get("mode")`, and `"Xbox+<ev> pressed"` has been
  logged **zero** times, ever.

## Why it works once the overlay is up

`controllers.py:826` — `should_intercept = emit and emit.should_intercept()`,
true only while the overlay is open. hhd deliberately does not hold the
controller during normal use (games need it) and takes it only when the QAM
appears. That is why input works *inside* the overlay and nowhere else.

## Why handhelds do not have this problem

On a ROG Ally or Legion Go, hhd sits **in** the input chain: it reads the
physical controller and publishes a virtual one, and Steam grabs the *virtual*
device. The physical device stays hhd's, so chords are always visible.

The Z13 has no built-in gamepad and hhd has no driver for a generic external
pad, so hhd is not in the chain — Steam talks to the physical device directly
and hhd is left outside. `shortcuts.controller.*` is therefore inert for
external controllers by construction, not by misconfiguration.

## What to use instead

- **The Armoury button.** `shortcuts.custom.armoury: hhd_qam` works — it comes
  in via `Asus WMI hotkeys`, a device Steam has no interest in grabbing.
  Confirmed firing (`Gesture: custom_armoury` -> `Executing overlay command:
  'open_qam'`).
- **Keyboard chords** work too, but only under gamescope: `kbd_` gestures are
  gated on `is_gamescope_running()` (`__init__.py:311`) because KDE/GNOME would
  otherwise swallow them. `meta_hold` is currently bound to `steam_qam`; it
  could be pointed at `hhd_qam` instead.

## Could hhd own an external pad the way it owns a handheld's?

Yes, architecturally — and **all the machinery already exists**. It is not a
new mechanism, just a missing plugin.

`hhd/device/rog_ally/base.py` is the reference:

```python
d_xinput = GenericGamepadEvdev(..., hide=True)   # grab + hide the physical pad
...
unhide_all()                                     # on exit
```

`hide=True` (`hhd/controller/lib/hide.py`) makes the physical device invisible
to everything else, and hhd publishes a virtual controller in its place —
`controller/virtual/uinput/const.py` already ships `xbox_360`, `xbox_one`,
`xbox_one_elite` and `xbox_sx` descriptors. Steam then talks to the virtual
device while hhd keeps the physical one, so chords are always visible.

Incidentally this is the same mechanism as the "hidden device" check at
`controllers.py:875` — hhd clears the group-read bit to hide a pad it owns,
and the shortcut watcher skips devices in that state.

**What is missing** is a device plugin that selects on the *controller's*
VID/PID. Every existing plugin — including `generic` — is chosen by machine
**DMI product name** (`CONFS.get(dmi)`), because they all assume a built-in
controller. This machine reports `ROG Flow Z13 GZ302EA_GZ302EA`, which is in
no table, and has no built-in pad anyway.

Real difficulties, roughly in order of nastiness:

1. **Racing Steam for the grab.** A Bluetooth pad that connects while Steam is
   already running may be grabbed by Steam first. Handhelds dodge this because
   hhd hides the device from boot, before Steam exists. Needs udev-driven
   hide-on-connect, and it must win every time or the shortcut silently stops
   working — the exact failure being debugged here.
2. **Force feedback.** This pad advertises FF (`FF=107030000`). Rumble has to
   be forwarded from the virtual device back to the physical one, or every
   game loses vibration.
3. **Identity.** The virtual device's VID/PID decides Steam's button glyphs and
   which Steam Input template applies. Publishing as `xbox_sx` is the obvious
   choice for an Xbox pad, but it must be right or the UI lies.
4. **Blast radius.** If hhd grabs and then dies without reaching
   `unhide_all()`, the controller is unusable until something unhides it. This
   sits in the input path for the *whole system*, not just the QAM.
5. **Hotplug churn.** BT reconnects change the device node; hide+grab has to be
   redone each time.
6. **A third consumer.** `gsr-ui` also holds the device open.

Estimate: a new device plugin of roughly 300–500 lines modelled on
`rog_ally/base.py`. This is a **device driver**, a different class of work from
the settings plugin hhd-z13 is today, and it would benefit every desktop or
tablet user with an external pad — so it belongs upstream as a generic feature
rather than a Z13 special case.

Sequencing: it is the riskiest item proposed for this project, because failure
mode is "controller stops working system-wide" rather than "a toggle does
nothing". It should come after the existing feature ladder, not before it.

## Consequence for the upstream plan

The `xbox_x` duplicate-case fix is still worth submitting — it is one line and
obviously correct — but it must not be described as fixing "controller
shortcuts do not open the QAM". It fixes one binding that is dead in code, on
devices where those bindings work at all. It does not help this machine.
