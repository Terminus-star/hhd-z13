"""z13_cpu -- CPU EPP/boost/perf-mode control for the ASUS ROG Flow Z13.

Shells out to `z13ctl-plus cpu`, which already does the actual sysfs work
(EPP, frequency floor, boost) and has a tested daemon behind it. This plugin
exists only to put those controls natively in hhd's QAM -- inside gamescope
included, since QAM controls render there regardless of which plugin owns
them -- because hhd's own tdp.amd_energy driver either duplicates (EPP), does
not work (Boost -- every attempt logs "Failed to set CPU boost:" with no
usable error, measured 2026-09-03), or never exposes (frequency floor,
present in the schema but never rendered even after a restart) the same
controls.

hhd-patches/hhd-qam-toggle-patch.py's z13-cpu-disable-native patch removes
hhd's own CPU Power/Boost controls, so this plugin is not fighting a second
writer for the same sysfs files.

SYNC WITH z13ctl-plus's OWN AUTOMATIC PRESET SWITCHING
    z13ctl-plus's daemon separately auto-applies a "Plugged In"/"On Battery"
    preset on every AC/battery transition, and those presets carry their own
    CPUPower (EPP/Boost/floor) -- kept enabled deliberately (see
    plan-hibernate-gpu-gate.md and this session's design discussion), so it
    is a second, legitimate writer to the same sysfs files, not a bug to
    route around. Without reconciliation the QAM sliders would go stale the
    moment a preset changed EPP behind their back -- displaying a value that
    no longer matches live hardware. update() polls `cpu --get --json`
    (throttled, RECONCILE_INTERVAL_S) and treats a live value that has
    drifted away from what we last agreed on (self.old_epp/self.old_boost)
    as "someone else changed it" -- pulling the new value INTO the config so
    the slider stays truthful, without re-writing what is already correct.
    A config value that has drifted from self.old_* instead means the user
    touched the slider, and is written out as before. Both compare against
    the SAME self.old_* baseline, which is what lets a pending user edit
    (config already new, live not yet caught up for this one tick) be told
    apart from an external change, rather than the two being confused for
    each other.

    Known limitation: perf_mode's own toggle is NOT reconciled the same way
    -- it is a local snapshot/restore state machine, not a mirror of a
    single live value, and reconciling it correctly (e.g. auto-flipping to
    Disabled and discarding a now-stale snapshot if an automatic preset
    fires while perf_mode is enabled) needs more machinery than a read-back
    comparison. Left as a known rough edge rather than solved partially:
    perf_mode's displayed state can go stale if an automatic preset switch
    happens while it reads Enabled; EPP/Boost stay correct regardless.
"""
import glob
import json
import logging
import os
import subprocess
import time

from hhd.plugins import HHDPlugin, load_relative_yaml
from hhd.plugins.conf import Config

logger = logging.getLogger(__name__)

Z13CTL = "z13ctl-plus"
TIMEOUT_S = 10

# amd_pstate has three modes and they do not expose the same knobs.
#
#   active  -> driver amd-pstate-epp. EPP is the knob; the governor is nearly
#              vestigial (only powersave/performance exist). Measured on this
#              machine: scaling_max_freq is clamped to the nominal 3000 MHz and
#              refuses anything higher, so the ceiling is not really settable.
#   guided  -> driver amd-pstate. Firmware picks within OS-set limits.
#   passive -> driver amd-pstate. The kernel governor drives perf directly.
#
# In guided and passive the energy_performance_preference file DOES NOT EXIST,
# and z13ctl-plus fails on every `cpu` subcommand because its read path starts
# by reading EPP -- so the whole plugin, not just the EPP slider, goes dead if
# it keeps shelling out. Outside active mode this drives sysfs directly, which
# hhd can do because it runs as root, and offers the cpufreq governor in EPP's
# place. Both modes also expose the full 5187 MHz range rather than 3000.
#
# EPP presence, not the mode string, is the functional test: it is what decides
# which knobs exist. The mode string is only used for labelling.
PSTATE_STATUS = "/sys/devices/system/cpu/amd_pstate/status"
BOOST_PATH = "/sys/devices/system/cpu/cpufreq/boost"
CPU0 = "/sys/devices/system/cpu/cpu0/cpufreq"
EPP_LEAF = "energy_performance_preference"


def _on_ac() -> bool | None:
    """True on mains, False on battery, None if it cannot be determined --
    which callers treat as "leave boost alone" rather than guessing."""
    import glob as _g
    for f in _g.glob("/sys/class/power_supply/A*/online"):
        try:
            with open(f) as fh:
                return fh.read().strip() == "1"
        except Exception:
            continue
    return None


def _resolve_boost(mode: str, manual: bool) -> bool | None:
    """Map the Auto/Manual gate onto an actual boost state.

    Manual is authoritative -- an explicit choice is never overridden by the
    charger. Auto follows the power source, and returns None when that cannot
    be read, which callers treat as "leave it alone" rather than guessing."""
    if mode == "manual":
        return bool(manual)
    ac = _on_ac()
    return None if ac is None else ac

def _pstate_mode() -> str:
    try:
        with open(PSTATE_STATUS) as f:
            return f.read().strip()
    except Exception:
        return "unknown"


def _has_epp() -> bool:
    return os.path.exists(os.path.join(CPU0, EPP_LEAF))


def _read_leaf(leaf: str) -> str | None:
    try:
        with open(os.path.join(CPU0, leaf)) as f:
            return f.read().strip()
    except Exception:
        return None


def _write_all(leaf: str, value: str) -> bool:
    """Write a cpufreq leaf on every policy. True if at least one took."""
    n = 0
    for d in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq"):
        try:
            with open(os.path.join(d, leaf), "w") as f:
                f.write(str(value))
            n += 1
        except Exception:
            continue
    return n > 0


def _write_boost(on: bool) -> bool:
    try:
        with open(BOOST_PATH, "w") as f:
            f.write("1" if on else "0")
        return True
    except Exception:
        return False


# 'userspace' is offered by the driver but is a trap in a QAM: selecting it
# hands control to a scaling_setspeed nobody writes, so the CPU simply stays
# wherever it was and the control appears to do nothing.
GOVERNOR_HIDE = ("userspace",)


def _governors() -> list[str]:
    v = _read_leaf("scaling_available_governors")
    if not v:
        return []
    return [g for g in v.split() if g not in GOVERNOR_HIDE]


def _get_live_sysfs() -> dict | None:
    """The same shape _get_live() returns, read straight from sysfs, for the
    modes where z13ctl-plus cannot read anything at all."""
    try:
        mn = _read_leaf("scaling_min_freq")
        mx = _read_leaf("scaling_max_freq")
        lo = _read_leaf("cpuinfo_min_freq")
        hi = _read_leaf("cpuinfo_max_freq")
        if None in (mn, mx, lo, hi):
            return None
        boost = _read_leaf("../../cpufreq/boost")
        if boost is None:
            try:
                with open(BOOST_PATH) as f:
                    boost = f.read().strip()
            except Exception:
                boost = None
        return {
            "min_frequency_khz": int(mn),
            "max_frequency_khz": int(mx),
            "min_limit_khz": int(lo),
            "max_limit_khz": int(hi),
            "governor": _read_leaf("scaling_governor"),
            "boost": boost == "1",
        }
    except Exception:
        return None
RECONCILE_INTERVAL_S = 2.0


def _run(*args) -> bool:
    """Run `z13ctl-plus cpu <args>`. Never raises -- a QAM control must not
    be able to crash hhd's main loop no matter what the subprocess does."""
    try:
        subprocess.run(
            [Z13CTL, "cpu", *args],
            check=True,
            capture_output=True,
            timeout=TIMEOUT_S,
        )
        return True
    except Exception:
        return False


def _get_live() -> dict | None:
    """Live CPU power state. Outside active mode z13ctl-plus cannot read
    anything at all -- its read path begins with EPP, which does not exist --
    so this falls back to sysfs rather than returning None forever and leaving
    every slider frozen. None on any
    failure -- a read-back that cannot complete must not disturb anything
    already applied, so callers treat None as "skip reconciliation this
    tick", never as a value to act on."""
    if not _has_epp():
        return _get_live_sysfs()
    try:
        r = subprocess.run(
            [Z13CTL, "cpu", "--get", "--json"],
            check=True,
            capture_output=True,
            timeout=TIMEOUT_S,
            text=True,
        )
        return json.loads(r.stdout)
    except Exception:
        return None


PERF_FLOOR_MHZ = 2000


def _apply_perf(plugin, enable: bool) -> bool:
    """z13ctl-plus's --perf/--save in active mode; the same idea in sysfs
    otherwise -- performance governor, a raised floor so there is nothing to
    ramp from, and boost on. The displaced values are snapshotted on the way
    IN only, so repeating --perf never overwrites the originals with perf's
    own, and --save restores what was actually there rather than a guess."""
    if _has_epp():
        return _run("--perf" if enable else "--save")
    if enable:
        if plugin.perf_snapshot is None:
            plugin.perf_snapshot = {
                "governor": _read_leaf("scaling_governor"),
                "min": _read_leaf("scaling_min_freq"),
                "boost": _read_leaf("../../cpufreq/boost"),
            }
        ok = _write_all("scaling_governor", "performance")
        ok = _write_all("scaling_min_freq", str(PERF_FLOOR_MHZ * 1000)) and ok
        return _write_boost(True) and ok
    snap = plugin.perf_snapshot or {}
    plugin.perf_snapshot = None
    ok = True
    if snap.get("governor"):
        ok = _write_all("scaling_governor", snap["governor"]) and ok
    mn = snap.get("min") or _read_leaf("cpuinfo_min_freq")
    if mn:
        ok = _write_all("scaling_min_freq", mn) and ok
    if snap.get("boost") is not None:
        ok = _write_boost(snap["boost"] == "1") and ok
    return ok


PSTATE_MODES = ("active", "guided", "passive")

# How long to leave the CPU alone after a mode switch, in seconds.
PSTATE_SETTLE_S = 5.0


def _apply_pstate(mode: str) -> bool:
    """Switch amd_pstate mode. Verified by reading back, since the file
    accepts a write and silently keeps the old value for anything the driver
    will not do.

    The kernel resets this to the amd_pstate= boot argument on every reboot,
    which is exactly why exposing it here is worth doing: hhd re-applies its
    own state at startup, so a choice made in the QAM survives a reboot with
    no bootloader edit."""
    if mode not in PSTATE_MODES:
        return False
    if _pstate_mode() == mode:
        return True
    try:
        with open(PSTATE_STATUS, "w") as f:
            f.write(mode)
    except Exception:
        return False
    return _pstate_mode() == mode


def _apply_pref(value: str) -> bool:
    """EPP in active mode, cpufreq governor everywhere else."""
    if _has_epp():
        return _run("--epp", value)
    return _write_all("scaling_governor", value)


def _apply_boost(enabled: bool) -> bool:
    if _has_epp():
        return _run("--boost", "1" if enabled else "0")
    return _write_boost(enabled)


def _nominal_ceiling_khz() -> int | None:
    try:
        v = _read_leaf("cpuinfo_max_freq")
        return int(v) if v else None
    except Exception:
        return None


def _ensure_boost_for(max_mhz: int) -> None:
    """Raising the ceiling above the nominal frequency is impossible while
    boost is off: with boost disabled the kernel reports cpuinfo_max_freq as
    the NOMINAL clock (3000 MHz on this machine, against a 5187 MHz boost
    ceiling), and scaling_max_freq is clamped to it. Writes are accepted and
    silently ignored, so a ceiling set from the QAM snapped straight back to
    3000 with nothing logged.

    Boost is therefore a precondition of the ceiling, not an independent
    toggle, and is switched on before a write that needs it rather than
    leaving the user to discover the coupling."""
    hi = _nominal_ceiling_khz()
    if hi is None or max_mhz * 1000 <= hi:
        return
    try:
        with open(BOOST_PATH) as f:
            if f.read().strip() == "1":
                return
    except Exception:
        return
    _write_boost(True)


def _reexpand_ceiling() -> None:
    """The reverse of _ensure_boost_for(): boost has just been turned ON, so
    re-open scaling_max_freq to the ceiling that boost made available.

    _ensure_boost_for() only covers "the user asked for a ceiling above
    nominal, so enable boost first". Nothing covered the other direction, and
    the kernel does not do it for us: enabling boost raises cpuinfo_max_freq
    (3000000 -> 5187500 here) but leaves every existing policy's
    scaling_max_freq exactly where it was. A policy built while boost was off
    therefore keeps the nominal ceiling forever.

    That is the "3000 MHz comes back" bug in its last hiding place. The path
    is: battery (Auto turns boost off, policies clamp to nominal) -> plug in
    AC (Auto turns boost on, cpuinfo_max_freq rises) -> ceiling stays at
    3000000, i.e. 58% of the hardware, silently, with nothing logged.

    Only widens, never narrows: if scaling_max_freq is already at or above
    cpuinfo_max_freq there is nothing to do, so a deliberately lower manual
    ceiling set through freq_mode is left alone.
    """
    try:
        hi = _read_leaf("cpuinfo_max_freq")
        cur = _read_leaf("scaling_max_freq")
        if not hi or not cur:
            return
        hi_i, cur_i = int(hi), int(cur)
        if cur_i >= hi_i:
            return
        if _write_all("scaling_max_freq", str(hi_i)):
            logger.info(
                f"Re-expanded CPU ceiling {cur_i // 1000} -> {hi_i // 1000} MHz "
                "after boost was enabled"
            )
    except Exception as e:
        logger.warning(f"Could not re-expand the CPU ceiling: {e}")


def _release_freq_clamp() -> None:
    """Hand the frequency limits back to the hardware's full range.

    Leaving Manual used to do nothing, on the stated assumption that the
    limits "simply stop being gated". They do not: the last manual write stays
    in scaling_min_freq/scaling_max_freq, unowned, until something else
    rebuilds the cpufreq policies -- a reboot, or an amd_pstate mode switch.
    So a floor dragged to 2000 MHz in Manual survived a switch back to Auto
    and kept the CPU off its idle clocks indefinitely, which is the opposite
    of what the label promises. Measured 2026-09-06.

    Writes the hardware's own limits rather than remembered values, because
    cpuinfo_* IS what an unclamped policy looks like, and it already tracks
    the current boost state -- so this lands correctly whether boost is on or
    off, and _reexpand_ceiling() widens the ceiling later if boost turns on.

    Fires ONLY on the manual -> auto transition, never continuously. Auto
    means this plugin does not manage the limits, so re-asserting them every
    tick would be managing them -- and would fight the EPP/Perf Mode/preset
    that Auto exists to defer to.
    """
    try:
        lo = _read_leaf("cpuinfo_min_freq")
        hi = _read_leaf("cpuinfo_max_freq")
        if not lo or not hi:
            return
        cur_lo = _read_leaf("scaling_min_freq")
        cur_hi = _read_leaf("scaling_max_freq")
        # Ceiling before floor: raising the floor first can be rejected
        # against a ceiling that is still low. Same ordering _apply_freq()
        # documents for z13ctl-plus.
        ok = _write_all("scaling_max_freq", hi)
        ok = _write_all("scaling_min_freq", lo) and ok
        if ok and (cur_lo != lo or cur_hi != hi):
            logger.info(
                f"Released CPU frequency clamp: "
                f"[{int(cur_lo or 0) // 1000}, {int(cur_hi or 0) // 1000}] -> "
                f"[{int(lo) // 1000}, {int(hi) // 1000}] MHz"
            )
    except Exception as e:
        logger.warning(f"Could not release the CPU frequency clamp: {e}")


def _apply_freq(min_mhz: int | None, max_mhz: int | None) -> bool:
    """Ceiling before floor when both move -- raising the floor first can be
    rejected against a lower ceiling. z13ctl-plus does this ordering itself;
    the sysfs path has to do it explicitly."""
    if max_mhz is not None:
        _ensure_boost_for(max_mhz)
    if _has_epp():
        if min_mhz is not None and max_mhz is not None:
            return _run("--max-freq", str(max_mhz), "--min-freq", str(min_mhz))
        if max_mhz is not None:
            return _run("--max-freq", str(max_mhz))
        if min_mhz is not None:
            return _run("--min-freq", str(min_mhz))
        return True
    ok = True
    if max_mhz is not None:
        ok = _write_all("scaling_max_freq", str(int(max_mhz) * 1000)) and ok
    if min_mhz is not None:
        ok = _write_all("scaling_min_freq", str(int(min_mhz) * 1000)) and ok
    return ok


class Z13CpuPlugin(HHDPlugin):
    def __init__(self) -> None:
        self.name = "z13_cpu"
        self.priority = 10
        self.log = "z13c"

        # None on purpose: forces one real apply on hhd's first update() call
        # rather than assuming the QAM's displayed default already matches
        # live hardware state.
        self.old_pstate = None
        # A mode switch tears down and rebuilds every cpufreq policy. Two
        # hard lockups were traced to writes landing during that window --
        # hhd's own adjustor was setting frequency scaling and z13ctl-plus was
        # retrying a power preset around the same moment, and the machine froze
        # with nothing in the journal (a hard lockup loses buffered writes).
        # Nothing else is written until this deadline passes.
        self._settle_until = 0.0
        self.perf_snapshot = None

        self.old_epp = None
        self.old_boost = None
        self.old_perf = None
        self.old_freq_mode = None
        self.old_min_freq = None
        self.old_max_freq = None

        self._last_reconcile = 0.0

    def settings(self):
        # hhd's settings merge expects a THIRD level of nesting beyond what
        # load_relative_yaml returns: {category: {section_name: Container}}.
        # Confirmed against adjustor's own settings() return
        # ({"tdp": {"amd_energy": sets["enabled"]}, ...}) and by reproducing
        # the actual crash locally: without the "root" section-name level,
        # merge_reduce_sec() iterates our Container's own top-level keys
        # (title/type/hint/children) as if each were its own section, hits
        # "title": "Z13 CPU" (a bare string), and crashes trying to
        # merge_reduce() a string. See hhd/plugins/settings.py
        # merge_reduce_sec()/merge_reduce().
        sets = load_relative_yaml("./settings.yml")
        # Nested INSIDE adjustor's amd_energy container ("Processor Settings")
        # in the tdp section, so these controls sit with the machine's other
        # CPU configuration instead of forming a block of their own.
        #
        # hhd's section list is fixed -- hhd/sections.yml, ten keys, served to
        # the UI over /sections -- so a top-level key that is not one of them
        # has nowhere to be filed and renders as an orphan section. That is
        # what "z13_cpu" was.
        #
        # "type": "container" is REQUIRED and "title" is deliberately ABSENT:
        # merge_reduce() only unions children when both sides report the same
        # type (settings.py:324-333), and pick_tag() resolves every other tag
        # as b.get(tag, a.get(tag, default)) -- so supplying a title here would
        # overwrite adjustor's "Processor Settings", while omitting it inherits
        # theirs whichever order the two plugins merge in.
        return {
            "tdp": {
                "amd_energy": {
                    "type": "container",
                    "children": {"z13_cpu": sets["z13_cpu"]},
                }
            }
        }

    def update(self, conf: Config):
        new_pstate = conf["tdp.amd_energy.z13_cpu.pstate.mode"].to(str)
        new_perf = conf["tdp.amd_energy.z13_cpu.perf_mode"].to(bool)
        # The control is nested under the mode that owns it, so the path
        # follows the selection rather than a key fixed at startup.
        pref_path = (
            "tdp.amd_energy.z13_cpu.pstate.active.epp"
            if new_pstate == "active"
            else f"tdp.amd_energy.z13_cpu.pstate.{new_pstate}.governor"
        )
        new_epp = conf[pref_path].to(str)
        # Resolved, not raw: in "auto" this follows the power source, so an
        # AC transition changes the target without the setting changing. The
        # comparison below is against the resolved value for that reason.
        boost_mode = conf["tdp.amd_energy.z13_cpu.boost.mode"].to(str)
        boost_manual = conf["tdp.amd_energy.z13_cpu.boost.manual.enabled"].to(bool)
        new_boost = _resolve_boost(boost_mode, boost_manual)
        new_freq_mode = conf["tdp.amd_energy.z13_cpu.freq_mode.mode"].to(str)
        new_min_freq = conf["tdp.amd_energy.z13_cpu.freq_mode.manual.min_freq"].to(int)
        new_max_freq = conf["tdp.amd_energy.z13_cpu.freq_mode.manual.max_freq"].to(int)

        # amd_pstate mode first: it decides which of the controls below even
        # exist, so applying it before them avoids writing a governor to an
        # EPP file (or the reverse) within the same tick. Adopt silently on
        # the first tick like the others, so hhd starting up never itself
        # switches the CPU driver.
        if time.monotonic() < self._settle_until:
            return

        if self.old_pstate is None:
            live_mode = _pstate_mode()
            if live_mode in PSTATE_MODES:
                self.old_pstate = live_mode
                conf["tdp.amd_energy.z13_cpu.pstate.mode"] = live_mode
        elif new_pstate != self.old_pstate:
            self.old_pstate = new_pstate
            _apply_pstate(new_pstate)
            self._settle_until = time.monotonic() + PSTATE_SETTLE_S
            # Everything below is invalidated: the driver changed, so the
            # values we last agreed on describe a policy that no longer exists.
            self.old_epp = None
            self.old_boost = None
            self.old_min_freq = None
            self.old_max_freq = None
            return

        # perf_mode is NOT like epp/boost: firing once unconditionally on the
        # first tick (self.old_perf is None) would run --save on every hhd
        # startup, even when the user never touched the toggle -- silently
        # discarding a snapshot, or reverting a perf mode set manually via
        # the CLI. Adopt the starting value silently instead; only an actual
        # change after that should fire anything.
        if self.old_perf is None:
            self.old_perf = new_perf
        elif new_perf != self.old_perf:
            self.old_perf = new_perf
            _apply_perf(self, new_perf)
            # --perf/--save already set EPP+Boost+floor(+ceiling) as one
            # unit. Adopt whatever all four sliders currently read as
            # "already applied" for this tick, so stale slider values left
            # over from before perf mode do not immediately re-fire below
            # and overwrite what --perf/--save just set. Reconciliation
            # below then pulls the sliders to the real post-perf/save
            # values on the next tick.
            self.old_epp = new_epp
            self.old_boost = new_boost
            self.old_min_freq = new_min_freq
            self.old_max_freq = new_max_freq
            return

        # freq_mode is a MODE GATE for the manual sliders below, not a plain
        # value like epp/boost: entering "manual" must (a) force CPU Perf
        # Mode off first -- it also owns floor+ceiling as one unit and would
        # otherwise immediately fight the manual sliders -- and (b) start
        # the manual sliders from LIVE clocks rather than a stale value left
        # over from the last time Manual was used, so flipping the selector
        # alone never itself changes anything the CPU is actually doing.
        # Leaving "manual" (back to auto) RELEASES the clamp. This used to do
        # nothing, on the assumption that the limits "simply stop being gated"
        # and something else takes over -- but nothing does: the last manual
        # write stays in scaling_min/max_freq, unowned, until a reboot or an
        # amd_pstate switch rebuilds the policies. A 2000 MHz floor set in
        # Manual therefore survived the switch back to Auto. Adopt the
        # starting value silently on the very first tick, same as perf_mode
        # above, so hhd startup never fires this unconditionally.
        if self.old_freq_mode is None:
            self.old_freq_mode = new_freq_mode
        elif new_freq_mode != self.old_freq_mode:
            self.old_freq_mode = new_freq_mode
            if new_freq_mode == "manual":
                if new_perf:
                    _apply_perf(self, False)
                    conf["tdp.amd_energy.z13_cpu.perf_mode"] = False
                    self.old_perf = False
                    # --save already re-set EPP+Boost as part of its unit;
                    # adopt the current slider values as "already applied"
                    # so the epp/boost blocks below don't immediately
                    # re-fire this same tick against a now-stale comparison.
                    self.old_epp = new_epp
                    self.old_boost = new_boost
                live = _get_live()
                if live is not None:
                    if live.get("min_frequency_khz") is not None:
                        v = round(live["min_frequency_khz"] / 1000)
                        conf["tdp.amd_energy.z13_cpu.freq_mode.manual.min_freq"] = v
                        self.old_min_freq = v
                    if live.get("max_frequency_khz") is not None:
                        v = round(live["max_frequency_khz"] / 1000)
                        conf["tdp.amd_energy.z13_cpu.freq_mode.manual.max_freq"] = v
                        self.old_max_freq = v
                # else: live read failed. Leave old_min_freq/old_max_freq and
                # the sliders as they were -- the ordinary reconciliation
                # path below will pick up real values on its own schedule
                # once freq_mode is "manual", same as any other drift.
            else:
                # Manual -> Auto: give the limits back to the hardware, then
                # stop touching them. old_min/max_freq are cleared so the next
                # entry into Manual re-reads live clocks rather than comparing
                # against the values this release just invalidated.
                _release_freq_clamp()
                self.old_min_freq = None
                self.old_max_freq = None
            return

        # A user-driven change (config moved away from what we last agreed
        # on) always takes priority and is written out immediately, without
        # waiting for or depending on a read-back this same tick.
        # If the mode was switched live, the schema built at startup no longer
        # matches the hardware -- an EPP control against a driver with no EPP
        # file, or a governor control in active mode. Writing either would be
        # wrong, so hold off until hhd restarts and rebuilds the settings.
        # Do not write a governor to a driver that wants EPP, or the reverse:
        # the selection can be ahead of the hardware for a tick after a switch.
        schema_stale = _has_epp() != (new_pstate == "active")
        epp_changed_by_user = (not schema_stale) and new_epp != self.old_epp
        if epp_changed_by_user:
            self.old_epp = new_epp
            _apply_pref(new_epp)

        boost_changed_by_user = new_boost is not None and new_boost != self.old_boost
        if boost_changed_by_user:
            self.old_boost = new_boost
            _apply_boost(new_boost)

        # Deliberately NOT gated on boost_changed_by_user. A policy built while
        # boost was off keeps the nominal ceiling, and that state outlives the
        # transition that caused it -- a resume, or an hhd restart, leaves the
        # machine stuck at 3000 MHz with boost already reading 1, so a
        # change-gated fix would never fire. Idempotent and cheap: two reads,
        # and it only writes when the ceiling is genuinely below the hardware.
        if new_boost:
            _reexpand_ceiling()

        # min_freq/max_freq are GATED by freq_mode: outside "manual" they are
        # hidden in the QAM and must never be applied to hardware -- Auto
        # means EPP/Perf Mode/an active preset stay in sole control, exactly
        # as if these sliders did not exist. Within "manual" they are
        # handled together, not like epp/boost above: z13ctl-plus REJECTS
        # (rather than silently clamps) a write that would put the floor
        # above the current ceiling or the ceiling below the current floor
        # -- see z13ctl-plus's cmd/cpu.go ORDERING NOTE, confirmed
        # 2026-09-03. If the user drags both sliders in the same hhd tick,
        # applying them separately in the wrong order can spuriously reject
        # one; applying them together in one `cpu` invocation lets
        # z13ctl-plus's own runCPUSet apply ceiling-before-floor internally,
        # so raising both together (or lowering both together) always
        # succeeds regardless of which slider the user actually moved first.
        # A genuinely invalid combination (e.g. a floor deliberately dragged
        # above an untouched ceiling) still fails -- that failure is silent
        # here (see _run's docstring) and self-heals below: reconciliation
        # notices the slider doesn't match live and snaps it back.
        min_changed_by_user = False
        max_changed_by_user = False
        if new_freq_mode == "manual":
            min_changed_by_user = new_min_freq != self.old_min_freq
            max_changed_by_user = new_max_freq != self.old_max_freq
            if min_changed_by_user or max_changed_by_user:
                self.old_min_freq = new_min_freq
                self.old_max_freq = new_max_freq
                _apply_freq(
                    new_min_freq if min_changed_by_user else None,
                    new_max_freq if max_changed_by_user else None,
                )

        # Reconciliation: only when nothing just moved (an in-flight user
        # write already handles those fields for this tick), and only every
        # RECONCILE_INTERVAL_S -- this is a subprocess call, not a sysfs
        # read, so it is not free to run on every single hhd tick.
        if epp_changed_by_user or boost_changed_by_user or min_changed_by_user or max_changed_by_user:
            return
        now = time.monotonic()
        if now - self._last_reconcile < RECONCILE_INTERVAL_S:
            return
        self._last_reconcile = now

        live = _get_live()
        if live is None:
            return

        live_pref = None if schema_stale else live.get("epp" if new_pstate == "active" else "governor")
        if live_pref is not None and live_pref != self.old_epp:
            self.old_epp = live_pref
            conf[pref_path] = live_pref

        # Only reconcile an explicit choice. Under "auto" the power source is
        # the authority, so pulling a live reading back into the config would
        # overwrite the user's policy with a momentary state.
        # Only reconcile an explicit choice. Under Auto the power source is the
        # authority, so pulling a live reading back into the config would
        # overwrite the policy with a momentary state.
        if "boost" in live and boost_mode == "manual":
            live_boost = bool(live["boost"])
            if live_boost != boost_manual:
                self.old_boost = live_boost
                conf["tdp.amd_energy.z13_cpu.boost.manual.enabled"] = live_boost

        # Gated the same as the write side above: outside "manual" the
        # sliders are hidden, so there is nothing useful to reconcile them
        # against -- entering "manual" later re-syncs them from a fresh live
        # read of its own (see the freq_mode transition block above), not
        # from whatever drifted in here while hidden.
        if new_freq_mode == "manual":
            if live.get("min_frequency_khz") is not None:
                live_min = round(live["min_frequency_khz"] / 1000)
                if live_min != self.old_min_freq:
                    self.old_min_freq = live_min
                    conf["tdp.amd_energy.z13_cpu.freq_mode.manual.min_freq"] = live_min

            if live.get("max_frequency_khz") is not None:
                live_max = round(live["max_frequency_khz"] / 1000)
                if live_max != self.old_max_freq:
                    self.old_max_freq = live_max
                    conf["tdp.amd_energy.z13_cpu.freq_mode.manual.max_freq"] = live_max
