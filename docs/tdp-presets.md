# TDP presets

What v0.4.0 built, and why it is shaped this way.

This document was rewritten after implementation. The design it originally
described — a standalone `z13_tdp` selector, per-slot SPL/sPPT/fPPT triples, a
`reports` field, and a patch to *stop* adjustor writing PPT — was built on two
premises that turned out to be false. Both are recorded below, because the
false version is the intuitive one and it will be reinvented otherwise.

---

## 1. What it does

Four preset slots appear as extra entries in adjustor's **existing** TDP
dropdown, beside Silent / Balanced / Performance / Custom:

```
TDP Mode  [ Custom · On Battery  ▾ ]
            Silent
            Balanced
            Performance
            Custom
            Custom · Plugged In
            Custom · On Battery      ← ours
            Custom · Gaming
            Custom · Alternate
   TDP     [=====      ] 20 W
   TDP Boost [x]

Preset on Power Change  [ Enabled ▾ ]
   On AC       [ Custom · Plugged In ▾ ]
   On Battery  [ Custom · On Battery ▾ ]
```

Each slot carries its own `tdp` (5–65 W) and `boost`. The power-change policy
is **disabled by default** and **edge-triggered**: it acts on a plug/unplug
transition and never continuously, so a mode picked by hand afterwards sticks
until the next transition.

**hhd-z13 writes no PPT registers at all.** That is the design, not an
omission — see §3.

---

## 2. Two corrections that reshaped this

### 2a. adjustor writes PPT only in `custom` mode

The original plan called "neuter `adjustor_asus`'s PPT writes" a *hard
prerequisite*, believing adjustor rewrites `ppt_*` on every mode change, AC/DC
event and wakeup.

It does not. Every PPT write in `adjustor/drivers/asus/__init__.py` lives
inside `if mode == "custom":`. In quiet/balanced/performance it writes
`throttle_thermal_policy` and nothing else. The AC/DC and wakeup handlers only
set `self.queue_tdp`, which is drained exclusively inside that same branch.

So there was nothing to neuter. The problem was *reaching* adjustor's writer,
not suppressing it — which inverted the whole release and deleted its riskiest
item.

### 2b. TDP control was already enabled

The claim "`tdp.tdp_enable` is `false`, so adjustor has written zero TDP
values" came from conflating two keys with the same leaf name:

- `tdp.tdp.tdp_enable` — an **action button**, rendered only while TDP is off.
  False is its normal resting state when TDP is already on.
- `hhd.settings.tdp_enable` — the real persisted bool. **Already true.**

Read live over hhd's API, `tdp.asus` exists with all four stock modes and a
`custom.tdp` slider of `min=5, max=65, default=45`. There is no enabling step.

---

## 3. Why adjustor does the writing

adjustor already has a correct writer for this machine: the right floor (§4),
the right interface (§5), the ordering, the debounce, the thermal-profile
selection, and the KDE power-profile report derived from wattage. What it
lacked was more than one custom slot.

So hhd-z13 adds slots and nothing else:

1. **Contribute modes.** hhd's `merge_reduce` unions a `mode`'s `modes` dict
   exactly as it unions a container's `children`, so extra dropdown entries
   are a merge, not a patch.
2. **Patch adjustor to reach them** (§6).
3. **Mirror** the selected slot's `tdp`/`boost` into
   `tdp.asus.tdp_v2.custom.tdp` / `.custom.boost` — the two keys adjustor's
   custom branch already reads.

A second PPT writer inside hhd would have rebuilt, in-process, the exact
multi-writer ambiguity this release exists to end.

### The merge contract

The contribution **omits `title`, `default` and `tags`.** `pick_tag` resolves
as `b.get(tag, a.get(tag, default))` and plugins merge in ascending priority
order, so adjustor (6) is `a` and hhd-z13 (11) is `b`. Supplying any of them
would overwrite adjustor's "TDP Mode" title and its `default: balanced`.

`type` is the one key that is **required**, at both levels. `merge_reduce`
only unions when `b["type"] == a["type"]`; get it wrong and the contribution
is silently dropped rather than raising.

### Why the mirror runs in `prepare()`

hhd runs `prepare()` for every plugin immediately before the update loop, in
**descending** priority order (`run_plugin_cmd(..., reverse=True)`), while
`update()` runs ascending. adjustor's driver is priority 6 and implements no
`prepare()`, so priority 11 gets a clean write window in the *same tick*.

From `update()` the write would land after adjustor had already read: one poll
interval late (`POLL_DELAY` is 2 s), and on a mode switch adjustor would arm
its debounce against the *previous* slot's wattage.

### Why the mirror is change-gated

adjustor writes `custom.tdp` back when its own clamp fires. Rewriting
unconditionally would undo that every tick while it rewrote every tick — the
CPU's power limits flapping. The mirror is gated on the tuple
`(mode, watts, boost)`, and the slot sliders are declared over adjustor's
*exact* enforced range (5–65 W) so its clamp never fires in the first place.

---

## 4. The floor is 5 W, not 28

`asus-armoury` advertises calibration per attribute:

| attribute | current | min | max | default |
|---|---|---|---|---|
| `ppt_pl1_spl` | 60 | 28 | 80 | 60 |
| `ppt_pl2_sppt` | 75 | 32 | 92 | — |
| `ppt_pl3_fppt` | 86 | 45 | 93 | — |

An earlier draft took that `min: 28` as the hardware floor and concluded a 9 W
setting was invalid. **That was wrong.** 28 is what the *asus-armoury
interface* advertises for itself, not what the hardware accepts:

| source | floor |
|---|---|
| `asus-armoury` `min_value` | **28** |
| adjustor `Z1_DATA`, keyed on `"ROG Flow Z13 GZ302"` — this exact model | **5** |
| adjustor's own TDP slider | **0** |
| `asus-nb-wmi` | no bounds files exist at all |
| `z13ctl-plus` | documents only *upper* caps (75 W, 93 W with `--force`) |
| measured on this machine | 9 W, 15 W, 20 W all worked |

Only armoury claims 28, and it is the outlier.

---

## 5. Write `asus-nb-wmi` — an explicit tradeoff, not a free win

adjustor writes `ppt_pl1_spl`, `ppt_pl2_sppt` and `ppt_fppt` under
`/sys/devices/platform/asus-nb-wmi/`. That is the right choice here, but the
earlier version of this document oversold it.

**Correction: it IS deprecated.** The original text claimed "the kernel emits
no deprecation warning for it." It does — lazily, on first access to the
legacy path, which is why a boot-time scan misses it:

```
asus_wmi: Accessing attributes through /sys/bus/platform/asus_wmi is
deprecated and will be removed in a future release.
Please switch over to /sys/class/firmware_attributes.
```

We write it anyway, with eyes open:

- **Armoury's 28 W floor would reject the presets this feature exists for.**
  That alone settles it: on the modern interface, a 9 W preset is unreachable.
- **Armoury does not expose live state.** Its `current_value` reads back the
  firmware profile's own numbers — 60/75/86 here — regardless of what override
  is active. nb-wmi reads 45/45/45, the value actually in force.
- **Armoury exposes three knobs; nb-wmi exposes five.**
- **It is where everything else already writes**, so converging there means
  one interface rather than a third.

There is currently **no path that gives both** live state and a non-deprecated
interface. When `firmware_attributes` grows a real minimum and reports live
values, this decision should be revisited — that is the migration trigger.

### The rule that follows

> **Read back through the interface you wrote.**

Verifying a nb-wmi write by reading armoury shows the firmware profile's
numbers and looks like a failure that did not happen. This is the single most
common misdiagnosis on this machine, and `system/v040-acceptance.sh` prints
armoury's value side by side with nb-wmi's, explicitly labelled informational,
so the divergence is visible rather than surprising.

---

## 6. The patch

`hhd-patches/hhd-qam-toggle-patch.py`, mark `z13-tdp-presets`. A **new** mark,
not `z13-cpu-disable-native`: that one means "adjustor stops writing this
because hhd-z13 owns it", and this is the exact opposite.

Four `"replace"` entries, all anchors verified unique:

| label | effect |
|---|---|
| `preset-fallthrough` | `mode != "custom"` → `mode in ("quiet","balanced","performance")` |
| `preset-apply` | `mode == "custom"` → also any mode outside those three |
| `wakeup-clock` | `time.time()` → `time.perf_counter()` |
| `acdc-clock` | `time.time()` → `time.perf_counter()` |

Entry 1 is needed because adjustor's dispatch ends in `case _:` meaning
*performance* — an unrecognised mode would set the Turbo thermal profile and
write no power limit. Both preset entries are phrased as "not one of the three
modes upstream handles", so adjustor never learns our mode names and behaviour
is identical with no presets installed.

### The clock bug (entries 3–4)

Found while tracing the above, and a genuine upstream defect.
`AsusDriverPlugin.update()` computes `curr = time.perf_counter()` and drains
its debounce with `self.queue_tdp < curr`. But `notify()` arms it with
`time.time()`. `perf_counter` is seconds since boot (~8×10³); `time()` is the
Unix epoch (~1.79×10⁹). An epoch deadline is never less than `curr`, so:

- **adjustor's resume and AC/DC re-applies have never fired.** The journal
  shows `Power adapter status switched to 'ac', resetting TDP.` with no write
  after it.
- It compounds: `queue_tdp` then stays truthy forever, and the AC/DC arm is
  guarded on `not self.queue_tdp`, so that stops arming too.

This is load-bearing. `z13-hibernate`'s `tdp-restore.sh` exists *because*
nothing re-applied TDP after resume; fixing the clock is what makes retiring
it safe. It is also the strongest upstreaming candidate in this repo — two
characters each, affecting every ASUS device adjustor supports, with a symptom
users report as "TDP doesn't come back after sleep".

**Known remaining wart:** with the clock fixed, a wakeup arms `queue_tdp` even
in stock modes, where nothing drains it, blocking later AC/DC arms. Unchanged
from today's behaviour for stock modes, fixed for custom and preset modes.
Report it alongside rather than silently half-fixing it.

---

## 7. Retirements

`system/v040-tdp.sh`, dry-run by default, `--revert`able.

**z13ctl-plus.** Its resume path re-applies PPT and is *not* gated on its own
`tdp_active` flag, so editing `state.json` cannot stop it. The working lever is
the write grant: the daemon runs as the user and can only write those files
because `z13ctl-plus-perms.service` chgrp/chmods them. hhd runs as root, so
revoking the grant stops z13ctl-plus and leaves adjustor untouched. The other
four grants in that unit are kept — `firmware.py` and the CPU plugin need them.

**z13-hibernate.** `tdp-restore.sh` is scheduled from two different sleep
hooks, so the script itself is replaced with a documented no-op rather than
editing both hooks.

**Do not retire either until the ordering gate passes:** suspend, resume, and
confirm `Waking up from sleep…` is followed within ~10 s by
`Setting tdp value 'steady'`. `z13-hibernate/common.sh:830` records a
2026-08-26 measurement claiming hhd already re-asserts at T+4, which
contradicts the clock analysis. One of the two is stale and only the machine
can settle it. If the gate fails, keep the old restore path — the presets work
either way.

---

## 8. Deliberately not done

- **A fourth D-Bus power profile.** hhd owns `net.hadess.PowerProfiles` and
  the spec has exactly three. Each preset maps to the closest, derived by
  adjustor from wattage (`<42` power-saver, `<50` balanced, else performance).
  Deriving is what was asked for: "show up on KDE as the closest preset".
- **Per-slot sPPT/fPPT.** adjustor derives them from the sustained value and
  the boost flag. Exposing them would mean bypassing its writer.
- **Live wattage in the mode titles.** Titles are static YAML, so
  "Quiet 15 W" becomes a lie the moment that slot is edited. Role names stay
  true. Rendering live values needs a settings regeneration per slider drag —
  a new failure class for a cosmetic win.
- **Reaching `max_tdp_oc` (90 W).** Slots stay 5–65 to match adjustor's
  enforced range exactly. 90 W remains available through stock Custom with
  *Enforce Device TDP Limits* off.
- **`/etc/hhd/profiles/` as storage.** Whole-config snapshots; wrong
  granularity. It stays available for stacking.
