# Design sketch — multiple custom TDP presets

Goal: named TDP presets beside the stock three, toggleable from the QAM, each
appearing in KDE as the closest standard profile while its own power limits are
what actually apply.

Everything below is measured on this machine (GZ302EA, hhd 4.1.12) rather than
assumed. The short version: **less of this needs building than it looks**, and
the hard part is not the UI — it is making sure only one thing writes PPT.

---

## 1. Constraints that decide the design

### KDE can only ever see three profiles, and that is fine

`net.hadess.PowerProfiles` on the system bus is owned by **hhd itself** — a
Python process under `hhd@termis.service`, which is adjustor's bundled PPD
server. It advertises exactly:

```
power-saver   balanced   performance
```

KDE's battery applet reads `ActiveProfile` from that name. power-profiles-daemon
defines those three and no more, so publishing a fourth would misreport to every
consumer that speaks the spec, KDE included.

So "show up as the closest preset" is not a compromise — it is the only correct
behaviour. **Each preset carries which of the three it reports.** A 32 W preset
can sit under `power-saver` in KDE while actually running 32 W.

### hhd already has the preset mechanism

`/etc/hhd/profiles/` exists, is loaded at startup, and is writable from the QAM
(`profiles[ev["name"]] = ev["config"]`). hhd's own settings documentation
describes this exact use case:

> "you can have **TDP only profiles** that control the energy budget, and
> controller profiles that switch controller behavior. Then, depending on the
> game, you can apply the appropriate 2 profiles" — and "it is encouraged for
> you to stack profiles together."

The directory is currently **empty**. This is an unused feature, not a missing
one.

### The stock tree has exactly one custom slot

`tdp.asus.tdp_v2` has modes `[quiet, balanced, performance, custom]`, and
`custom` holds a single scalar `tdp` plus `boost`. One slot, one number — which
is why N presets need something on top, and why they want a PPT *triple* rather
than one figure.

### The TDP floor is 5 W, not 28 — and this decides which interface to write

`asus-armoury` advertises calibration per attribute:

| attribute | current | min | max | default |
|---|---|---|---|---|
| `ppt_pl1_spl` | 60 | 28 | 80 | 60 |
| `ppt_pl2_sppt` | 75 | 32 | 92 | — |
| `ppt_pl3_fppt` | 86 | 45 | 93 | — |

An earlier draft took that `min: 28` as the hardware floor and concluded a 9 W
setting was invalid. **That was wrong.** 28 is what the *asus-armoury interface*
advertises for itself, not what the hardware accepts. Six sources:

| source | floor |
|---|---|
| `asus-armoury` `min_value` | **28** |
| adjustor `Z1_DATA` — keyed on `"ROG Flow Z13 GZ302"`, this exact model | **5** |
| adjustor's own TDP slider (`drivers/asus/settings.yml`) | **0** (max 30) |
| `asus-nb-wmi` | no bounds files exist at all |
| `z13ctl-plus` | documents only *upper* caps (75 W, 93 W with `--force`) |
| measured on this machine | 9 W, 15 W, 20 W all worked |

Only the armoury interface claims 28, and it is the outlier. `custom.tdp: 9` is
a deliberate, mid-slider, working value.

### Therefore: write `asus-nb-wmi`, not `asus-armoury`

This reverses the earlier recommendation. `asus-nb-wmi` wins on every axis that
matters here:

- **No artificial floor.** Armoury's 28 W minimum would reject the 9/15/20 W
  presets that are the entire point of this feature.
- **More knobs.** nb-wmi exposes five (`ppt_pl1_spl`, `ppt_pl2_sppt`,
  `ppt_fppt`, `ppt_apu_sppt`, `ppt_platform_sppt`); armoury exposes three.
- **It is where everything else already writes.** Both `adjustor_asus` and
  `z13ctl-plus` use it. Converging there means one interface, not a third one.
- **It is not deprecated.** The kernel emits no deprecation warning for it;
  `asus_wmi` and `asus-armoury` are both loaded and current. The "kernel-
  deprecated" note in the older plan is unsubstantiated here.

Armoury stays useful for one thing: its `max_value` figures are a reasonable
*upper* calibration hint, and they corroborate z13ctl-plus's 93 W ceiling. Read
maxima from armoury if you like; ignore its minima and floor at 5 W per
`Z1_DATA`.

### The blocking dependency: two interfaces, two answers

The same three hardware limits, read through both interfaces, right now:

| | SPL | sPPT | fPPT |
|---|---|---|---|
| `asus-nb-wmi` — where `adjustor_asus` writes | 45 | 45 | 45 |
| `asus-armoury` — firmware-calibrated | 60 | 75 | 86 |

`adjustor_asus` is loaded and writes `/sys/devices/platform/asus-nb-wmi/ppt_*`.
It rewrites them on every mode change, AC/DC transition and wakeup. So does
`z13ctl-plus`. That is three writers on one set of registers.

The divergence above is not two interfaces contradicting each other about the
same value — it is nb-wmi showing the last *override* written to it (45 across
the board) while armoury reports the firmware profile's own numbers. Reading one
after writing the other is how "the TDP reverted" gets misdiagnosed.

**This must be resolved before presets are worth building.** The fix is the
treatment already applied to the CPU controls: extend `z13-cpu-disable-native`
in `hhd-patches/hhd-qam-toggle-patch.py` to neuter `adjustor_asus`'s PPT writes,
and make z13ctl-plus's startup re-apply conditional, so hhd-z13 is the single
writer — on `asus-nb-wmi`, per the section above.

Corollary, and the rule that caught this: **read back through the interface you
wrote.** Writing armoury and verifying via nb-wmi would have shown 45 and looked
like a failure.

---

## 2. Shape

### Storage — our own config, not `/etc/hhd/profiles`

Tempting to reuse hhd's profiles, but they are whole-config snapshots applied by
name, which makes them awkward for a quick toggle and couples preset editing to
hhd's profile UI. Presets live in our own settings tree instead, so they appear
inline in the QAM next to the other Z13 controls, and hhd profiles remain
available on top for anyone who wants to stack a preset with controller settings.

### Settings tree

Mirrors the Auto/Manual pattern already used for refresh rate and CPU boost, so
the QAM stays consistent — the detail only appears once you choose to see it.

```yaml
z13_tdp:
  type: mode
  tags: [ordinal]
  default: stock
  modes:
    stock:
      title: Stock            # hands PPT back to the platform profile
    preset:
      title: Preset
      children:
        active:
          type: discrete       # dropdown, populated from the presets below
          tags: [dropdown]
        # one block per preset slot; ranges filled from firmware at settings()
        slot1:
          type: mode
          children:
            name:   { type: display }
            # floor 5 per Z1_DATA -- NOT armoury's min_value, which would
            # reject the 9/15/20 W presets this feature exists for.
            # maxima may be read from armoury's max_value as a hint.
            spl:    { type: int, min: 5, max: 80 }
            sppt:   { type: int, min: 5, max: 92 }
            fppt:   { type: int, min: 5, max: 93 }
            reports:                                   # what KDE is told
              type: discrete
              options: { "power-saver": Silent, "balanced": Balanced,
                         "performance": Performance }
```

Two details worth fixing at design time, both already burned once:

- **Quote every option key.** YAML 1.1 turns bare `on`/`off`/`no` into booleans
  and bare `60` into an int, so keys become `True`/`60` and stop matching.
- **`settings()` runs once at startup.** Firmware ranges must be read there, and
  adding a preset slot at runtime is not possible — so ship a fixed number of
  slots (4 is plenty) rather than a growable list.

### `reports` default, not derived

It is tempting to derive the KDE profile from SPL with thresholds. Don't — the
whole point of a custom preset is that the user disagrees with the stock mapping.
Default it from SPL (`<40 → power-saver`, `<60 → balanced`, else `performance`)
and let it be overridden. Deriving it silently reintroduces the coupling that
made panel overdrive worth decoupling from refresh rate.

### Apply path

On preset selection:

1. Clamp to `5 .. max`. The floor is `Z1_DATA.min_tdp`, **not** armoury's
   `min_value` — clamping to 28 would silently raise a 9 W preset to 28 W and
   look like the TDP "reverting" all over again.
2. Write `ppt_pl1_spl`, then `ppt_pl2_sppt`, then `ppt_fppt` under
   `/sys/devices/platform/asus-nb-wmi/`. Ascending order, because the firmware
   rejects a configuration where a lower tier exceeds a higher one.
   `ppt_apu_sppt` and `ppt_platform_sppt` exist here too — z13ctl-plus sets both
   to the PL2 value, which is a sane default to copy.
3. Read all three **back through `asus-nb-wmi`** and log a warning on mismatch.
   Verifying via armoury will show the firmware profile's numbers instead and
   look like a failure that did not happen.
4. Set PPD `ActiveProfile` to the preset's `reports` value so KDE follows.

Re-apply on resume and on AC/DC transition — those are precisely the events
where `adjustor_asus` used to clobber the values, and where the firmware itself
may reset them.

---

## 3. Sequencing

| | |
|---|---|
| **v0.2.0** (now) | **Nothing.** `custom.tdp: 9` is valid and deliberate; leave it. The earlier "raise it above the floor" item was based on the false 28 W floor and has been dropped. |
| **prereq** | Extend `z13-cpu-disable-native` to `adjustor_asus`'s PPT writes. Nothing below is trustworthy until PPT has one writer. |
| **v0.5.0** | The tree above: 4 slots, firmware-derived ranges, PPD mapping, resume/AC re-apply. |
| **later** | Per-preset fan curve and CPU boost, if wanted. Deliberately out of scope at first — one new writer at a time. |

The prerequisite is the real work; the UI is a settings tree and about 80 lines
of apply logic. Note also `tdp.tdp_enable` is currently **`false`**, so hhd's
TDP control is switched off entirely — that is why adjustor has written no TDP
values at all, and it must be turned on before any of this does anything.

---

## 4. Deliberately not doing

- **A fourth D-Bus profile.** Breaks the PPD spec and every consumer of it.
- **Deriving the KDE profile from SPL with no override.** Silent coupling.
- **Reusing `/etc/hhd/profiles` as preset storage.** Whole-config snapshots,
  wrong granularity for a toggle. Still useful for stacking on top.
- **Writing `asus-armoury`.** Its `min_value` of 28 W would reject the low
  presets this feature exists for, and it exposes three knobs where nb-wmi
  exposes five. Its `max_value` figures are still worth reading as calibration.
- **Clamping to armoury's advertised minimum.** That is the specific mistake
  that produced the false "9 W is below the floor" conclusion.
