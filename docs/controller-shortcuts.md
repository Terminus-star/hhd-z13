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

## Consequence for the upstream plan

The `xbox_x` duplicate-case fix is still worth submitting — it is one line and
obviously correct — but it must not be described as fixing "controller
shortcuts do not open the QAM". It fixes one binding that is dead in code, on
devices where those bindings work at all. It does not help this machine.
