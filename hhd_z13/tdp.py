"""User-created TDP presets, applied through adjustor's own Custom mode.

WHY PRESETS ARE DATA, NOT MODES
    The first version of this added preset MODES to adjustor's TDP dropdown.
    It worked, and then lost to KDE. Unplugging makes KDE change its power
    profile; that reaches adjustor as a "ppd" event, which its notify() maps
    onto a stock mode and its update() FORCE-writes over the config:

        new_mode = self.new_mode
        ...
        elif new_mode:
            mode = new_mode
            conf["tdp.asus.tdp_v2.mode"] = mode

    It writes the value captured in notify() rather than reading the config,
    so a preset mode was simply replaced -- observed live as "unplugging
    selects Silent instead of the battery preset".

    Adjustor's stock Custom mode has always survived this, and the reason is
    worth stating: it does NOT avoid the overwrite. The mode still flips to
    quiet and the label still reads Silent. But the quiet branch writes only
    throttle_thermal_policy and never touches PPT, so nothing overwrites the
    registers and the wattage stays exactly where Custom put it.

    So a preset is now a stored value set. Selecting one writes adjustor's own
    custom.tdp / custom.boost and selects Custom -- the identical path a user
    takes by hand -- and inherits that tolerance for free. It also removed two
    patch anchors: adjustor's mode dispatch no longer needs widening at all.

WHY prepare() AND NOT update()
    hhd runs prepare() for every plugin immediately before the update loop, in
    DESCENDING priority order, while update() runs ascending. adjustor's
    driver is priority 6 and implements no prepare(), so anything above 6 gets
    a clean write window in the same tick, before adjustor reads. From
    update() the write would be a poll interval late (2s) and adjustor would
    arm its debounce against the previous preset's wattage.

WHY THE PRESET LIST CAN CHANGE AT RUNTIME
    hhd fixes a mode's entries when settings() runs, so a dynamic list is only
    possible by asking hhd to rebuild the tree -- emit {"type": "settings"}.
    The count is persisted as an ordinary setting, read back in update(), and
    a disagreement with what was last generated triggers exactly one rebuild.

PL2/PL3
    All three limits are computed here, from this panel's own firmware
    figures, and written through the z13-tdp-presets patch.

    The firmware ships 60 / 75 / 86 W for PL1 / PL2 / PL3, so Auto reproduces
    those proportions at whatever sustained value a preset asks for -- PL2 at
    1.25x, PL3 at 1.433x -- capped at the firmware's own 92 / 93 W maxima.
    Manual states all three outright. Boost off means one flat limit.

    Deliberately not left to adjustor, whose derivation is built around a
    fixed 35 W ceiling. This machine sustains up to 65 W, so from 35 W upward
    that ceiling sits below the sustained value itself and its
    min(max(steady, ceiling), steady * ratio) collapses to steady: all three
    limits equal and the boost flag inert, across most of the range this
    machine actually runs in. 45 W would come out 45/45/45 rather than
    45/56/64.
"""

import glob
import logging
from copy import deepcopy

from hhd.plugins import HHDPlugin, load_relative_yaml
from hhd.plugins.conf import Config

from .plugin import _on_ac

logger = logging.getLogger(__name__)

MIN_PRESETS = 1
MAX_PRESETS = 8
DEFAULT_PRESETS = 2

# adjustor's three stock profiles. LABELS ARE ADJUSTOR'S, NOT ITS KEYS, which
# do not agree: `balanced` is displayed "Performance" and `performance` is
# displayed "Turbo", following Armoury Crate. Naming them by key would put two
# different meanings of "Performance" in one dropdown.
STOCK_MODES = ("quiet", "balanced", "performance")
STOCK_LABELS = {"quiet": "Silent", "balanced": "Performance", "performance": "Turbo"}

# adjustor's Z1_DATA min_tdp/max_tdp, and what it clamps to while "Enforce
# Device TDP Limits" is on. Matching exactly means its clamp never fires, so
# it never writes custom.tdp back underneath us.
MIN_TDP = 5
MAX_TDP = 65

# ── this panel's own power limits ─────────────────────────────────────────
# Every number below is read off THIS machine, from
# /sys/class/firmware-attributes/asus-armoury/attributes/:
#
#   attribute       default_value   max_value
#   ppt_pl1_spl          60             80
#   ppt_pl2_sppt         75             92
#   ppt_pl3_fppt         86             93
#
# So the firmware's own shipped relationship between the three tiers is
# 60 / 75 / 86 W, i.e. PL2 is 1.25x the sustained limit and PL3 is 1.433x.
# Those ratios are what "Auto" reproduces at any sustained value, and the
# max_value figures are the ceilings.
#
# Deriving them here rather than leaving it to adjustor is deliberate.
# adjustor's derivation is built around a fixed 35 W ceiling that has nothing
# to do with this hardware: this machine sustains up to 65 W, so from 35 W
# upward that ceiling sits below the sustained value itself and its
# min(max(steady, ceiling), steady * ratio) collapses to steady -- all three
# limits equal, boost inert, at exactly the wattages this machine spends most
# of its time in. Computing from the panel's own figures keeps the boost flag
# meaningful across the whole 5-65 W range.
FW_PL1, FW_PL2, FW_PL3 = 60, 75, 86
SPPT_RATIO = FW_PL2 / FW_PL1        # 1.25
FPPT_RATIO = FW_PL3 / FW_PL1        # 1.4333...
MAX_SPPT = 92
MAX_FPPT = 93

BASE = "tdp.asus"
MODE_PATH = f"{BASE}.tdp_v2.mode"
CUSTOM_TDP = f"{BASE}.tdp_v2.custom.tdp"
CUSTOM_BOOST = f"{BASE}.tdp_v2.custom.boost"
PRESETS = f"{BASE}.z13_presets"
ACTIVE = f"{PRESETS}.active.mode"
COUNT = f"{PRESETS}.count"
ADD = f"{PRESETS}.add"
REMOVE = f"{PRESETS}.remove"
SPPT = f"{PRESETS}.sppt"
FPPT = f"{PRESETS}.fppt"
AUTO = f"{BASE}.z13_tdp_auto"
STATUS = f"{BASE}.z13_tdp_status"

PATCH_SENTINEL = '_z13_s = conf.get("tdp.asus.z13_presets.sppt", 0)'
ADJUSTOR_ASUS_GLOB = "/usr/lib/python3*/site-packages/adjustor/drivers/asus/__init__.py"
PATCH_WARNING = (
    "Manual PL2/PL3 needs the adjustor patch — run hhd-patches/install.sh"
)


def _pid(i: int) -> str:
    return f"p{i}"


def _clamp(v, lo, hi):
    return min(max(v, lo), hi)


def _adjustor_is_patched() -> bool | None:
    """Is the installed adjustor carrying the z13-tdp-presets patch?

    None means unanswerable (not found, unreadable). Callers make no claim in
    that case: a false banner is cheaper than a wrong reassurance, but only
    when we actually know.
    """
    hits = sorted(glob.glob(ADJUSTOR_ASUS_GLOB))
    if not hits:
        return None
    try:
        with open(hits[-1]) as f:
            return PATCH_SENTINEL in f.read()
    except Exception:
        return None


class Z13TdpPlugin(HHDPlugin):
    def __init__(self) -> None:
        self.name = "z13_tdp"
        # Above adjustor's 6, which is what puts our prepare() ahead of its
        # update(). 11 rather than 10 so we also sit behind the other two
        # hhd-z13 plugins in the descending prepare order.
        self.priority = 11
        self.log = "z13t"

        # False until update() reads it, mirroring adjustor's own driver
        # starting disabled -- so we appear on the same settings pass it does.
        self._tdp_ready = False
        self._count = DEFAULT_PRESETS

        self.old_ac = None      # None = first tick adopts, never applies
        self.old_apply = None   # last applied (pid, w, boost, sppt, fppt)
        self._patched = None
        self._warned = False

    def open(self, emit, context, *args, **kwargs):
        self.emit = emit

    # ── settings ──────────────────────────────────────────────────────────
    def settings(self):
        if not self._tdp_ready:
            # Contributing alone would render an "Asus TDP" section with no
            # TDP Mode selector for the presets to drive. Better to be absent.
            return {}

        sets = load_relative_yaml("./tdp_settings.yml")
        n = _clamp(self._count, MIN_PRESETS, MAX_PRESETS)

        # deepcopy per slot: a YAML alias would make every preset the same
        # dict object, and the tree is walked and annotated in several places.
        modes = {}
        for i in range(1, n + 1):
            c = deepcopy(sets["preset_template"])
            c["title"] = f"Preset {i}"
            modes[_pid(i)] = c

        node = sets["z13_presets"]
        node["children"]["active"]["modes"] = modes
        node["children"]["active"]["default"] = _pid(1)
        node["children"]["count"]["default"] = n

        # Presets first, then the stock profiles -- the same order they appear
        # in adjustor's own dropdown reads oddly here, and "my presets, then
        # the built-ins" is what someone picking a policy is looking for.
        opts = {_pid(i): f"Preset {i}" for i in range(1, n + 1)}
        opts.update({k: STOCK_LABELS[k] for k in STOCK_MODES})

        auto = sets["z13_tdp_auto"]
        ac = auto["modes"]["enabled"]["children"]["ac_preset"]
        dc = auto["modes"]["enabled"]["children"]["dc_preset"]
        ac["options"] = dict(opts)
        dc["options"] = dict(opts)
        ac["default"] = _pid(1)
        dc["default"] = _pid(min(2, n))

        return {
            "tdp": {
                "asus": {
                    # `type` required or merge_reduce refuses to union and the
                    # contribution is silently dropped. Title omitted so
                    # adjustor's "Asus TDP" survives pick_tag.
                    "type": "container",
                    "children": {
                        "z13_presets": node,
                        "z13_tdp_auto": auto,
                        "z13_tdp_status": sets["z13_tdp_status"],
                    },
                }
            }
        }

    # ── reading a preset ──────────────────────────────────────────────────
    def _values(self, conf: Config, pid: str):
        base = f"{PRESETS}.active.{pid}"
        watts = _clamp(conf[f"{base}.tdp"].to(int), MIN_TDP, MAX_TDP)
        boost = conf[f"{base}.boost"].to(bool)

        if conf.get(f"{base}.ppt.mode", "auto") == "manual":
            sppt = _clamp(conf[f"{base}.ppt.manual.sppt"].to(int), MIN_TDP, MAX_SPPT)
            fppt = _clamp(conf[f"{base}.ppt.manual.fppt"].to(int), MIN_TDP, MAX_FPPT)
        elif boost:
            # Auto: this panel's own firmware ratios, applied at whatever
            # sustained value the preset asks for. Unlike a fixed ceiling,
            # this keeps the boost flag meaningful at every wattage the
            # machine supports rather than only below some other device's
            # maximum.
            sppt = min(round(watts * SPPT_RATIO), MAX_SPPT)
            fppt = min(round(watts * FPPT_RATIO), MAX_FPPT)
        else:
            # Boost off means exactly that: one flat limit, no headroom.
            sppt = fppt = watts

        # The firmware REJECTS a lower tier that exceeds a higher one rather
        # than clamping it, so a nonsensical order would silently apply
        # nothing at all. Raise rather than reject -- the request was for at
        # least this much headroom.
        sppt = max(sppt, watts)
        fppt = max(fppt, sppt)
        return watts, boost, sppt, fppt

    def _apply(self, conf: Config, pid, watts, boost, sppt, fppt):
        conf[CUSTOM_TDP] = watts
        conf[CUSTOM_BOOST] = boost
        conf[SPPT] = sppt
        conf[FPPT] = fppt
        # Selecting Custom is what makes adjustor act. Note this is asserted
        # only when a preset is APPLIED -- never re-asserted afterwards. If
        # KDE flips the label to Silent we leave it, exactly as stock Custom
        # behaves, because the wattage survives regardless and fighting for
        # the label would be a loop with no benefit.
        conf[MODE_PATH] = "custom"
        logger.info(
            f"Applied {pid}: PL1={watts}W boost={boost}"
            + (f" PL2={sppt}W PL3={fppt}W" if sppt else " PL2/PL3 derived")
        )

    # ── per-tick ──────────────────────────────────────────────────────────
    def prepare(self, conf: Config):
        # One try/except around everything: hhd's run_plugin_cmd REMOVES a
        # plugin that raises, permanently, not just for this tick.
        try:
            if ACTIVE not in conf or MODE_PATH not in conf:
                return
            self._auto_switch(conf)
            self._apply_active(conf)
        except Exception as e:
            logger.warning(f"TDP preset prepare failed: {e}")

    def _apply_active(self, conf: Config):
        pid = conf[ACTIVE].to(str)
        if f"{PRESETS}.active.{pid}.tdp" not in conf:
            return  # mid-rebuild, or a stale selection
        vals = self._values(conf, pid)
        sig = (pid, *vals)

        if self.old_apply is None:
            # First tick adopts. Starting hhd must not change the power limit
            # by itself; adjustor re-applies custom.tdp on its own at startup.
            self.old_apply = sig
            return
        if sig == self.old_apply:
            return
        self.old_apply = sig
        self._apply(conf, pid, *vals)

    def _auto_switch(self, conf: Config):
        """Follow the charger, on the EDGE only."""
        if f"{AUTO}.mode" not in conf:
            return
        if conf[f"{AUTO}.mode"].to(str) != "enabled":
            # Re-arm, so turning the policy back on adopts the current source
            # rather than firing a stale edge from before it was off.
            self.old_ac = None
            return

        ac = _on_ac()
        if ac is None:
            return  # unreadable: leave everything alone rather than guess

        if self.old_ac is None:
            self.old_ac = ac
            return
        if ac == self.old_ac:
            # Deliberately not continuous: a change made by hand after a
            # transition stays until the next transition.
            return

        self.old_ac = ac
        key = "ac_preset" if ac else "dc_preset"
        target = conf.get(f"{AUTO}.enabled.{key}", "")
        where = "AC" if ac else "battery"

        if target in STOCK_MODES:
            logger.info(f"Power source -> {where}: TDP mode -> {target}")
            conf[MODE_PATH] = target
            return

        if f"{PRESETS}.active.{target}.tdp" not in conf:
            return  # a preset that no longer exists
        logger.info(f"Power source -> {where}: preset -> {target}")
        conf[ACTIVE] = target
        # Apply directly rather than leaving it to _apply_active: the values
        # may be identical to what is already applied (so the change gate
        # would skip), while the MODE may have been moved off custom in the
        # meantime. The transition is exactly when it must be re-asserted.
        vals = self._values(conf, target)
        self.old_apply = (target, *vals)
        self._apply(conf, target, *vals)

    # ── add / remove ──────────────────────────────────────────────────────
    def _resize(self, conf: Config, delta: int) -> bool:
        n = _clamp(conf.get(COUNT, DEFAULT_PRESETS), MIN_PRESETS, MAX_PRESETS)
        new = _clamp(n + delta, MIN_PRESETS, MAX_PRESETS)
        if new == n:
            logger.info(
                f"Preset count already at its {'maximum' if delta > 0 else 'minimum'}"
                f" ({n})"
            )
            return False

        if delta > 0:
            # Seed the new slot from the active one, so a new preset starts
            # from something useful rather than a default nobody chose.
            # __setitem__ creates missing paths, so this lands before the tree
            # is rebuilt and survives into it.
            src = conf[ACTIVE].to(str)
            dst = _pid(new)
            base_src = f"{PRESETS}.active.{src}"
            base_dst = f"{PRESETS}.active.{dst}"
            if f"{base_src}.tdp" in conf:
                for leaf in ("tdp", "boost", "ppt.mode",
                             "ppt.manual.sppt", "ppt.manual.fppt"):
                    if f"{base_src}.{leaf}" in conf:
                        conf[f"{base_dst}.{leaf}"] = conf[f"{base_src}.{leaf}"].conf
        else:
            # Removing the tail: if it is selected, step back first, or
            # validate_config would reset the selection to the default and the
            # choice would look arbitrary.
            if conf[ACTIVE].to(str) == _pid(n):
                conf[ACTIVE] = _pid(new)

        conf[COUNT] = new
        logger.info(f"Preset slots: {n} -> {new}")
        return True

    def update(self, conf: Config):
        try:
            self._tdp_ready = conf.get("hhd.settings.tdp_ready", False)
            if STATUS not in conf:
                return

            # get_action returns True once and self-clears.
            changed = False
            if conf.get_action(ADD):
                changed = self._resize(conf, +1)
            elif conf.get_action(REMOVE):
                changed = self._resize(conf, -1)

            # The count is also editable directly, and is what survives a
            # restart -- settings() cannot read the config, so this is how it
            # learns the real number. One rebuild per disagreement.
            n = _clamp(conf.get(COUNT, DEFAULT_PRESETS), MIN_PRESETS, MAX_PRESETS)
            if changed or n != self._count:
                self._count = n
                self.old_apply = None  # the tree is about to change under us
                self.emit({"type": "settings"})

            if self._patched is None:
                self._patched = _adjustor_is_patched()
                if self._patched is False and not self._warned:
                    self._warned = True
                    logger.error(
                        "adjustor is not carrying the z13-tdp-presets patch: "
                        "presets still work, but a preset set to Manual power "
                        "limits will have its PL2/PL3 derived instead. Run "
                        "hhd-patches/install.sh."
                    )

            conf[STATUS] = "" if self._patched is not False else PATCH_WARNING
        except Exception as e:
            logger.warning(f"TDP preset update failed: {e}")

    def close(self):
        # Nothing to release: this plugin owns no hardware and no descriptors.
        # Leaving the last preset applied is correct -- the machine keeps the
        # limit the user chose, exactly as if hhd were stopped while on the
        # stock Custom mode.
        pass
