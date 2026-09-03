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
import json
import subprocess
import time

from hhd.plugins import HHDPlugin, load_relative_yaml
from hhd.plugins.conf import Config

Z13CTL = "z13ctl-plus"
TIMEOUT_S = 10
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
    """Read live CPU power state via `cpu --get --json`. None on any
    failure -- a read-back that cannot complete must not disturb anything
    already applied, so callers treat None as "skip reconciliation this
    tick", never as a value to act on."""
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


class Z13CpuPlugin(HHDPlugin):
    def __init__(self) -> None:
        self.name = "z13_cpu"
        self.priority = 10
        self.log = "z13c"

        # None on purpose: forces one real apply on hhd's first update() call
        # rather than assuming the QAM's displayed default already matches
        # live hardware state.
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
        return {"z13_cpu": {"root": sets["z13_cpu"]}}

    def update(self, conf: Config):
        new_perf = conf["z13_cpu.root.perf_mode"].to(str)
        new_epp = conf["z13_cpu.root.epp"].to(str)
        new_boost = conf["z13_cpu.root.boost"].to(str)
        new_freq_mode = conf["z13_cpu.root.freq_mode.mode"].to(str)
        new_min_freq = conf["z13_cpu.root.freq_mode.manual.min_freq"].to(int)
        new_max_freq = conf["z13_cpu.root.freq_mode.manual.max_freq"].to(int)

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
            _run("--perf" if new_perf == "enabled" else "--save")
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
        # Leaving "manual" (back to auto) needs no action: frequency limits
        # simply stop being gated, and whatever EPP/Perf Mode/an active
        # z13ctl-plus preset says next takes over as normal. Adopt the
        # starting value silently on the very first tick, same as perf_mode
        # above, so hhd startup never fires this unconditionally.
        if self.old_freq_mode is None:
            self.old_freq_mode = new_freq_mode
        elif new_freq_mode != self.old_freq_mode:
            self.old_freq_mode = new_freq_mode
            if new_freq_mode == "manual":
                if new_perf == "enabled":
                    _run("--save")
                    conf["z13_cpu.root.perf_mode"] = "disabled"
                    self.old_perf = "disabled"
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
                        conf["z13_cpu.root.freq_mode.manual.min_freq"] = v
                        self.old_min_freq = v
                    if live.get("max_frequency_khz") is not None:
                        v = round(live["max_frequency_khz"] / 1000)
                        conf["z13_cpu.root.freq_mode.manual.max_freq"] = v
                        self.old_max_freq = v
                # else: live read failed. Leave old_min_freq/old_max_freq and
                # the sliders as they were -- the ordinary reconciliation
                # path below will pick up real values on its own schedule
                # once freq_mode is "manual", same as any other drift.
            return

        # A user-driven change (config moved away from what we last agreed
        # on) always takes priority and is written out immediately, without
        # waiting for or depending on a read-back this same tick.
        epp_changed_by_user = new_epp != self.old_epp
        if epp_changed_by_user:
            self.old_epp = new_epp
            _run("--epp", new_epp)

        boost_changed_by_user = new_boost != self.old_boost
        if boost_changed_by_user:
            self.old_boost = new_boost
            _run("--boost", "1" if new_boost == "enabled" else "0")

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
                if min_changed_by_user and max_changed_by_user:
                    _run("--max-freq", str(new_max_freq), "--min-freq", str(new_min_freq))
                elif max_changed_by_user:
                    _run("--max-freq", str(new_max_freq))
                else:
                    _run("--min-freq", str(new_min_freq))

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

        if live.get("epp") is not None and live["epp"] != self.old_epp:
            self.old_epp = live["epp"]
            conf["z13_cpu.root.epp"] = live["epp"]

        if "boost" in live:
            live_boost = "enabled" if live["boost"] else "disabled"
            if live_boost != self.old_boost:
                self.old_boost = live_boost
                conf["z13_cpu.root.boost"] = live_boost

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
                    conf["z13_cpu.root.freq_mode.manual.min_freq"] = live_min

            if live.get("max_frequency_khz") is not None:
                live_max = round(live["max_frequency_khz"] / 1000)
                if live_max != self.old_max_freq:
                    self.old_max_freq = live_max
                    conf["z13_cpu.root.freq_mode.manual.max_freq"] = live_max
