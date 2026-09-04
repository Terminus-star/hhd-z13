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

### Firmware calibration is populated, so ranges self-derive

| attribute | current | min | max | default |
|---|---|---|---|---|
| `ppt_pl1_spl` | 60 | 28 | 80 | 60 |
| `ppt_pl2_sppt` | 75 | 32 | 92 | — |
| `ppt_pl3_fppt` | 86 | 45 | 93 | — |

Read these at `settings()` time instead of hardcoding. They survive firmware
updates, and they are the authority on what is even accepted — note the SPL
floor is **28 W**, so the stored `custom.tdp: 9` was never applicable.

### The blocking dependency: two interfaces, two answers

The same three hardware limits, read through both interfaces, right now:

| | SPL | sPPT | fPPT |
|---|---|---|---|
| `asus-nb-wmi` — where `adjustor_asus` writes | 45 | 45 | 45 |
| `asus-armoury` — firmware-calibrated | 60 | 75 | 86 |

`adjustor_asus` is loaded and writes `/sys/devices/platform/asus-nb-wmi/ppt_*`,
the kernel-deprecated path. It rewrites them on every mode change, AC/DC
transition and wakeup.

**This must be resolved before presets are worth building.** Two writers on the
same registers through two interfaces that disagree produces exactly the
"TDP reverts by itself" behaviour already seen. The fix is the treatment already
applied to the CPU controls: extend `z13-cpu-disable-native` in
`hhd-patches/hhd-qam-toggle-patch.py` to neuter `adjustor_asus`'s PPT writes, so
hhd-z13 is the only writer and it writes `asus-armoury`.

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
            spl:    { type: int, min: 28, max: 80 }   # from min_value/max_value
            sppt:   { type: int, min: 32, max: 92 }
            fppt:   { type: int, min: 45, max: 93 }
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

1. Clamp each value to the firmware `min_value`/`max_value` — never write an
   out-of-range figure and hope.
2. Write `spl`, then `sppt`, then `fppt` to
   `asus-armoury/attributes/ppt_*/current_value`. Ascending order, because the
   firmware rejects a configuration where a lower tier exceeds a higher one.
3. Read all three **back through `asus-armoury`** and log a warning on mismatch.
4. Set PPD `ActiveProfile` to the preset's `reports` value so KDE follows.

Re-apply on resume and on AC/DC transition — those are precisely the events
where `adjustor_asus` used to clobber the values, and where the firmware itself
may reset them.

---

## 3. Sequencing

| | |
|---|---|
| **v0.2.0** (now) | Raise `custom.tdp` above the 28 W floor. No preset work. |
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
- **Writing `asus-nb-wmi`.** Deprecated, and it is what currently disagrees.
