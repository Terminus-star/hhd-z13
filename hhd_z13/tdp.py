"""Extra TDP preset slots, added to adjustor's own dropdown.

WHY THIS WRITES NO HARDWARE
    adjustor already has a correct PPT writer for this machine: the right
    floor (5 W, from its own Z1_DATA, not asus-armoury's advertised 28), the
    right interface (asus-nb-wmi), the right ordering and debounce, the
    thermal-profile selection, and the KDE power-profile report derived from
    the wattage. What it lacks is more than ONE custom slot.

    So this plugin adds slots and nothing else. It contributes extra modes
    into adjustor's tdp.asus.tdp_v2 dropdown and, when one is selected,
    mirrors that slot's watts into the two config keys adjustor's custom mode
    already reads. adjustor does every write.

    That is the point. v0.4.0 exists to hand TDP ownership BACK to adjustor as
    z13ctl-plus is phased out -- growing a second PPT writer here would have
    recreated, inside hhd, exactly the multi-writer ambiguity the release is
    meant to end.

WHY prepare() AND NOT update()
    hhd runs prepare() for every plugin immediately before the update loop,
    and it runs it in DESCENDING priority order (run_plugin_cmd(...,
    reverse=True)) while update() runs ascending. adjustor's ASUS driver is
    priority 6 and implements no prepare() at all, so anything above 6 gets a
    clean write window in the SAME tick, before adjustor reads.

    Mirroring from update() instead would land after adjustor had already
    read, so the value would be one poll interval late -- POLL_DELAY is 2s --
    and on a mode switch adjustor would arm its debounce against the PREVIOUS
    slot's wattage before seeing the new one.

WHY THE MIRROR IS CHANGE-GATED
    adjustor writes custom.tdp back when its own clamp fires. If we rewrote
    the key unconditionally we would undo that every tick and it would rewrite
    it every tick: the one oscillation this design admits. The slot sliders
    are also declared over adjustor's exact enforced range, so its clamp has
    nothing to clamp in the first place. Belt and braces, because a stuck
    apply loop here means the CPU's power limits flapping.

REQUIRES THE ADJUSTOR PATCH
    adjustor gates every PPT write on `mode == "custom"`, and its preset
    dispatch ends in `case _:` meaning performance -- so without
    hhd-patches' z13-tdp-presets table a preset would set the Turbo thermal
    profile and write nothing. That is invisible from the UI, so update()
    checks for the patch and says so in a status field rather than failing
    quietly.
"""

import glob
import logging
import time

from hhd.plugins import HHDPlugin, load_relative_yaml
from hhd.plugins.conf import Config

from .plugin import _on_ac

logger = logging.getLogger(__name__)

# The mode keys we contribute. Order is the dropdown order.
PRESET_MODES = ("z13_ac", "z13_dc", "z13_game", "z13_alt")

# The three modes adjustor itself handles. Anything else reaches its custom
# apply path once patched. Kept here so the guard below reads the same way the
# patch does.
STOCK_MODES = ("quiet", "balanced", "performance")

# What the power-change policy may select. The stock firmware profiles are
# included deliberately: "on battery, just use Silent" is a legitimate policy,
# and restricting the dropdown to our own slots would have forced someone who
# wants it to burn a slot reproducing a profile adjustor already has.
SELECTABLE_MODES = PRESET_MODES + STOCK_MODES

# How long after a power transition the chosen mode is held against anything
# that tries to move it.
#
# Not belt-and-braces -- this is load-bearing. Unplugging makes KDE switch its
# power profile, which arrives as a "ppd" event ~3s later (measured on this
# machine at 19:27:42, three seconds after the 19:27:39 unplug). adjustor's
# notify() maps that onto quiet/balanced/performance and its update() then
# FORCE-writes it over the config:
#
#     new_mode = self.new_mode
#     ...
#     elif new_mode:
#         mode = new_mode
#         conf["tdp.asus.tdp_v2.mode"] = mode
#
# It writes the value captured in notify() rather than reading the config, so
# our prepare() write from an earlier tick is simply overwritten -- and being
# edge-triggered, we had no reason to fire again. The symptom was "unplugging
# selects Silent instead of the battery preset".
#
# The window is what separates the transition's own aftershocks from a genuine
# later choice by the user, which must still win. 15s covers the observed 3s
# echo plus adjustor's APPLY_DELAY and the 2s poll interval, with margin.
HOLD_S = 15.0

# adjustor's Z1_DATA min_tdp/max_tdp for "ROG Flow Z13 GZ302", and what it
# clamps to while "Enforce Device TDP Limits" is on. Matching it exactly is
# what keeps its clamp from ever firing against us.
MIN_TDP = 5
MAX_TDP = 65

# A fragment of the patched guard. Present only in a patched adjustor.
PATCH_SENTINEL = 'if mode == "custom" or (mode and mode not in'
ADJUSTOR_ASUS_GLOB = "/usr/lib/python3*/site-packages/adjustor/drivers/asus/__init__.py"

MODE_PATH = "tdp.asus.tdp_v2.mode"
CUSTOM_TDP = "tdp.asus.tdp_v2.custom.tdp"
CUSTOM_BOOST = "tdp.asus.tdp_v2.custom.boost"
AUTO_PATH = "tdp.asus.z13_tdp_auto"
STATUS_PATH = "tdp.asus.z13_tdp_status"

PATCH_WARNING = (
    "TDP presets need the adjustor patch — run hhd-patches/install.sh"
)


def _adjustor_is_patched() -> bool | None:
    """Is the installed adjustor carrying the z13-tdp-presets patch?

    None means the question could not be answered (adjustor not found, file
    unreadable). Callers treat None as "do not claim either way" -- failing
    this check OPEN would be worse than a false banner, because an unpatched
    adjustor silently applies the Turbo thermal profile and no power limit.
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
        # hhd-z13 plugins in the descending prepare order and cannot perturb
        # them.
        self.priority = 11
        self.log = "z13t"

        # Starts False exactly as adjustor's own driver starts disabled, so we
        # appear on the same settings pass it appears on: AdjustorInitPlugin
        # (priority 5) flips hhd.settings.tdp_ready and emits a settings event
        # in the same tick, and our update() at 11 has already run by the time
        # that regeneration is processed.
        self._tdp_ready = False

        # None means "not established yet". Both are first-tick-adopts.
        self.old_ac = None
        self.old_mirror = None
        # (target_mode, monotonic_deadline) while a transition is settling.
        self._hold = None
        self._patched = None
        self._warned = False

    def open(self, emit, context, *args, **kwargs):
        self.emit = emit

    def settings(self):
        if not self._tdp_ready:
            # Contributing alone would render an "Asus TDP" section holding
            # only our presets and none of adjustor's modes, whose selections
            # would do nothing. Better to be absent.
            return {}
        sets = load_relative_yaml("./tdp_settings.yml")
        return {
            "tdp": {
                "asus": {
                    # `type` is required at BOTH levels or merge_reduce
                    # refuses to union and our contribution is silently
                    # dropped. Title omitted so adjustor's "Asus TDP" and its
                    # `default: balanced` survive pick_tag.
                    "type": "container",
                    "children": {
                        "tdp_v2": {
                            "type": "mode",
                            "modes": sets["z13_tdp_presets"]["modes"],
                        },
                        "z13_tdp_auto": sets["z13_tdp_auto"],
                        "z13_tdp_status": sets["z13_tdp_status"],
                    },
                }
            }
        }

    def prepare(self, conf: Config):
        # One try/except around everything on purpose. hhd's run_plugin_cmd
        # REMOVES a plugin that raises, for the rest of the process lifetime --
        # an unguarded exception here is permanent, not transient.
        try:
            if MODE_PATH not in conf:
                return  # adjustor's TDP tree is not up yet
            self._auto_switch(conf)  # may change the mode ...
            self._mirror(conf)  # ... so mirror after it, same tick
        except Exception as e:
            logger.warning(f"TDP preset prepare failed: {e}")

    def _auto_switch(self, conf: Config):
        """Follow the charger, on the EDGE only."""
        if f"{AUTO_PATH}.mode" not in conf:
            return
        if conf[f"{AUTO_PATH}.mode"].to(str) != "enabled":
            # Re-arm, so turning the policy back on adopts the current power
            # source rather than firing a stale edge from before it was off.
            self.old_ac = None
            self._hold = None
            return

        # Hold the target across the transition's aftershocks. Checked before
        # the edge logic because the thing that overwrites us arrives SEVERAL
        # TICKS AFTER the edge, by which point old_ac is already updated and
        # nothing would fire again.
        if self._hold is not None:
            target, until = self._hold
            if time.monotonic() >= until:
                self._hold = None
            elif conf[MODE_PATH].to(str) != target:
                logger.info(
                    f"TDP mode was moved off {target} during the power "
                    f"transition (KDE's power profile does this); re-asserting"
                )
                conf[MODE_PATH] = target

        ac = _on_ac()
        if ac is None:
            return  # unreadable: leave the mode alone rather than guess

        if self.old_ac is None:
            # First tick adopts and writes nothing. Starting hhd must never
            # move a mode the user chose.
            self.old_ac = ac
            return
        if ac == self.old_ac:
            # Deliberately NOT continuous. This is the whole answer to "don't
            # fight the user": a mode picked by hand after a transition stays
            # picked until the next transition.
            return

        self.old_ac = ac
        key = "ac_preset" if ac else "dc_preset"
        target = conf[f"{AUTO_PATH}.enabled.{key}"].to(str)
        if target not in SELECTABLE_MODES:
            return
        # The hold is armed even when the mode is ALREADY the target. Nothing
        # has been overwritten yet at this point -- the overwrite comes a few
        # seconds later -- so arming only on a change would leave the common
        # case (unplug, already on the battery preset) unprotected.
        self._hold = (target, time.monotonic() + HOLD_S)
        if target != conf[MODE_PATH].to(str):
            logger.info(
                f"Power source -> {'AC' if ac else 'battery'}: TDP mode -> {target}"
            )
            conf[MODE_PATH] = target

    def _mirror(self, conf: Config):
        """Copy the active slot's values into the keys adjustor reads."""
        mode = conf[MODE_PATH].to(str)
        if mode not in PRESET_MODES:
            # Leaving a preset invalidates the baseline, so re-entering the
            # SAME preset later still counts as a change and re-applies.
            self.old_mirror = None
            return

        watts = conf[f"tdp.asus.tdp_v2.{mode}.tdp"].to(int)
        boost = conf[f"tdp.asus.tdp_v2.{mode}.boost"].to(bool)
        watts = min(max(watts, MIN_TDP), MAX_TDP)

        if (mode, watts, boost) == self.old_mirror:
            return
        self.old_mirror = (mode, watts, boost)
        conf[CUSTOM_TDP] = watts
        conf[CUSTOM_BOOST] = boost

    def update(self, conf: Config):
        try:
            self._tdp_ready = conf.get("hhd.settings.tdp_ready", False)
            if STATUS_PATH not in conf:
                return

            if self._patched is None:
                self._patched = _adjustor_is_patched()
                if self._patched is False and not self._warned:
                    self._warned = True
                    logger.error(
                        "adjustor is not carrying the z13-tdp-presets patch: "
                        "TDP presets will select the Turbo thermal profile and "
                        "write no power limit. Run hhd-patches/install.sh."
                    )

            conf[STATUS_PATH] = "" if self._patched is not False else PATCH_WARNING
        except Exception as e:
            logger.warning(f"TDP preset update failed: {e}")

    def close(self):
        # Nothing to release. This plugin owns no hardware and no file
        # descriptors -- every write went through adjustor's config keys, and
        # leaving the last preset applied is correct: the machine keeps the
        # power limit the user chose, exactly as it would if hhd were stopped
        # while on the stock Custom mode.
        pass
