"""TDP presets, layered onto adjustor's own Custom mode.

ONE SLIDER, NOT TWO
    adjustor's Custom mode renders its own TDP and TDP Boost controls, and
    they are what drive the hardware. So they are THE editor: selecting a
    preset LOADS its stored values into them, and editing them SAVES back
    into the selected preset.

    An earlier attempt gave each preset its own sliders and hid adjustor's by
    tagging them `hide`. That does not work -- verified against the live tree,
    where the tags read ['hide'] and hhd-ui rendered the controls anyway.
    `hide` is honoured for hhd's own `version` field and evidently not for
    ordinary controls, so a duplicate can only be removed structurally.

WHY PRESETS ARE DATA, NOT MODES
    The first version added preset MODES to adjustor's TDP dropdown. It lost
    to KDE: unplugging makes KDE change its power profile, which reaches
    adjustor as a "ppd" event, and its update() FORCE-writes the mapped stock
    mode over the config -- writing the value captured in notify() rather than
    reading the config, so a preset mode was simply replaced.

    Stock Custom survives that, and not by avoiding it. The mode still flips
    to quiet and the label still reads Silent, but the quiet branch writes
    only throttle_thermal_policy and never touches PPT, so the wattage stays
    exactly where Custom put it. Applying presets through Custom inherits that
    for free.

WHY THE STORE IS A FILE
    Anything declared in the settings tree renders. Per-slot values are state,
    not controls, and putting them in the tree is precisely what forced a
    second set of sliders to appear. They live in a JSON file instead, which
    also lets the slot count change without the tree growing.

    The same file carries the PL2/PL3 targets that the z13-tdp-presets patch
    reads, so adjustor takes them from one place rather than from config keys
    that would have to render somewhere.

PL2/PL3
    Computed from this panel's own firmware figures: it ships 60 / 75 / 86 W
    for PL1 / PL2 / PL3, so Auto reproduces those proportions -- 1.25x and
    1.433x -- capped at its 92 / 93 W maxima.

    TDP Boost is the gate. With it off all three limits are the same number,
    which is why Power Limits is omitted from the tree entirely while boost is
    off rather than sitting there inert.

    Deliberately not left to adjustor, whose derivation is built around a
    fixed 35 W ceiling. This machine sustains up to 65 W, so from 35 W upward
    that ceiling sits below the sustained value itself and its
    min(max(steady, ceiling), steady * ratio) collapses to steady: all three
    limits equal, across most of the range this machine actually runs in.
"""

import glob
import json
import logging
import os
from copy import deepcopy

from hhd.plugins import HHDPlugin, load_relative_yaml
from hhd.plugins.conf import Config

from .plugin import _on_ac

logger = logging.getLogger(__name__)

MIN_PRESETS = 1
MAX_PRESETS = 8
DEFAULT_PRESETS = 2

# Persistent, because presets must survive a restart. Alongside hhd's own
# state.yml, which is the established home for its mutable state.
STORE_PATH = "/etc/hhd/z13-tdp-presets.json"

# adjustor's three stock profiles. LABELS ARE ADJUSTOR'S, NOT ITS KEYS, which
# do not agree: `balanced` is displayed "Performance" and `performance` is
# displayed "Turbo", following Armoury Crate. Naming them by key would put two
# different meanings of "Performance" in one dropdown.
STOCK_MODES = ("quiet", "balanced", "performance")
STOCK_LABELS = {"quiet": "Silent", "balanced": "Performance", "performance": "Turbo"}

# adjustor's Z1_DATA min_tdp/max_tdp, and what it clamps to while "Enforce
# Device TDP Limits" is on. Matching exactly means its clamp never fires.
MIN_TDP = 5
MAX_TDP = 65

# ── this panel's own power limits ─────────────────────────────────────────
# Read off THIS machine, from
# /sys/class/firmware-attributes/asus-armoury/attributes/:
#
#   attribute       default_value   max_value
#   ppt_pl1_spl          60             80
#   ppt_pl2_sppt         75             92
#   ppt_pl3_fppt         86             93
FW_PL1, FW_PL2, FW_PL3 = 60, 75, 86
SPPT_RATIO = FW_PL2 / FW_PL1        # 1.25
FPPT_RATIO = FW_PL3 / FW_PL1        # 1.4333...
MAX_SPPT = 92
MAX_FPPT = 93

NBWMI = "/sys/devices/platform/asus-nb-wmi"

BASE = "tdp.asus"
MODE_PATH = f"{BASE}.tdp_v2.mode"
CUSTOM = f"{BASE}.tdp_v2.custom"
CUSTOM_TDP = f"{CUSTOM}.tdp"          # adjustor's slider -- THE editor
CUSTOM_BOOST = f"{CUSTOM}.boost"      # adjustor's toggle -- THE gate
PRESET = f"{CUSTOM}.z13_preset.mode"
PPT_MODE = f"{CUSTOM}.z13_ppt.mode"
PPT_SPPT = f"{CUSTOM}.z13_ppt.manual.sppt"
PPT_FPPT = f"{CUSTOM}.z13_ppt.manual.fppt"
APPLIED = f"{CUSTOM}.z13_applied"
ADD = f"{CUSTOM}.z13_add"
REMOVE = f"{CUSTOM}.z13_remove"
STATUS = f"{CUSTOM}.z13_status"
POWER = f"{BASE}.z13_power"

PATCH_SENTINEL = "z13-tdp-presets"
ADJUSTOR_ASUS_GLOB = "/usr/lib/python3*/site-packages/adjustor/drivers/asus/__init__.py"
PATCH_WARNING = "Power limits need the adjustor patch — run hhd-patches/install.sh"


def _pid(i: int) -> str:
    return f"p{i}"


def _clamp(v, lo, hi):
    return min(max(v, lo), hi)


def _read_w(attr: str):
    """One PPT value from asus-nb-wmi, or None. Never raises."""
    try:
        with open(os.path.join(NBWMI, attr)) as f:
            return int(f.read().strip())
    except Exception:
        return None


def _adjustor_is_patched():
    """Is the installed adjustor carrying the z13-tdp-presets patch?

    None means unanswerable (not found, unreadable). Callers make no claim
    then: a false banner is cheaper than a wrong reassurance, but only when we
    actually know.
    """
    hits = sorted(glob.glob(ADJUSTOR_ASUS_GLOB))
    if not hits:
        return None
    try:
        with open(hits[-1]) as f:
            return PATCH_SENTINEL in f.read()
    except Exception:
        return None


def _default_preset(i: int):
    # Two useful starting points rather than N copies of the same number.
    return {"tdp": 45 if i == 1 else 20, "boost": True,
            "ppt": "auto", "sppt": 0, "fppt": 0}


class Z13TdpPlugin(HHDPlugin):
    def __init__(self) -> None:
        self.name = "z13_tdp"
        # Above adjustor's 6, which is what puts our prepare() ahead of its
        # update() in the same tick -- hhd runs prepare in DESCENDING priority
        # and update in ascending, and adjustor implements no prepare().
        self.priority = 11
        self.log = "z13t"

        self._tdp_ready = False
        self._store = None        # {"presets": [...], "applied": {...}}
        self._gen_boost = True    # boost value the current tree was built for
        self._gen_count = DEFAULT_PRESETS

        self.old_ac = None        # None = first tick adopts, never applies
        self.old_loaded = None    # (pid, tdp, boost, ppt, sppt, fppt) as loaded
        self.old_applied = None   # last string written to the telemetry field
        self._patched = None
        self._warned = False

    def open(self, emit, context, *args, **kwargs):
        self.emit = emit

    # ── the store ─────────────────────────────────────────────────────────
    def _load_store(self):
        if self._store is not None:
            return self._store
        data = None
        try:
            with open(STORE_PATH) as f:
                data = json.load(f)
        except FileNotFoundError:
            pass
        except Exception as e:
            # Corrupt store: start clean rather than refuse to run. The file
            # is regenerated below, so this self-heals.
            logger.warning(f"Could not read {STORE_PATH}, starting fresh: {e}")
        if not isinstance(data, dict) or not isinstance(data.get("presets"), list) \
                or not data["presets"]:
            data = {"presets": [_default_preset(i) for i in (1, 2)],
                    "applied": {"sppt": 0, "fppt": 0}}
        self._store = data
        self._gen_count = len(data["presets"])
        return data

    def _save_store(self):
        try:
            os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
            tmp = STORE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._store, f, indent=2)
            os.replace(tmp, STORE_PATH)
        except Exception as e:
            logger.error(f"Could not write {STORE_PATH}: {e}")

    def _slot(self, pid: str):
        st = self._load_store()
        try:
            i = int(pid[1:]) - 1
        except Exception:
            return None
        return st["presets"][i] if 0 <= i < len(st["presets"]) else None

    # ── settings ──────────────────────────────────────────────────────────
    def settings(self):
        if not self._tdp_ready:
            # Contributing alone would render an "Asus TDP" section with no
            # TDP Mode selector for the presets to drive.
            return {}

        st = self._load_store()
        n = _clamp(len(st["presets"]), MIN_PRESETS, MAX_PRESETS)
        sets = load_relative_yaml("./tdp_settings.yml")
        kids = sets["z13_custom_children"]

        # Slot containers are empty: the values are edited through adjustor's
        # sliders and stored in the JSON file.
        kids["z13_preset"]["modes"] = {
            _pid(i): {"type": "container", "title": f"Preset {i}"}
            for i in range(1, n + 1)
        }
        kids["z13_preset"]["default"] = _pid(1)

        # Boost off means PL1 = PL2 = PL3, so there is nothing for Power
        # Limits to say. Drop it from the tree rather than render it inert.
        # update() rebuilds when the toggle moves.
        if not self._gen_boost:
            kids.pop("z13_ppt", None)

        opts = {_pid(i): f"Preset {i}" for i in range(1, n + 1)}
        opts.update({k: STOCK_LABELS[k] for k in STOCK_MODES})
        power = sets["z13_power"]
        ac = power["modes"]["enabled"]["children"]["ac_preset"]
        dc = power["modes"]["enabled"]["children"]["dc_preset"]
        ac["options"], dc["options"] = dict(opts), dict(opts)
        ac["default"], dc["default"] = _pid(1), _pid(min(2, n))

        return {
            "tdp": {
                "asus": {
                    # `type` required at EVERY level or merge_reduce refuses to
                    # union and the contribution is silently dropped. Titles
                    # omitted so adjustor's own survive pick_tag.
                    "type": "container",
                    "children": {
                        "tdp_v2": {
                            "type": "mode",
                            "modes": {
                                "custom": {"type": "container", "children": kids}
                            },
                        },
                        # Its own area, NOT nested in Custom: it selects which
                        # preset to apply, which must be reachable from any
                        # mode -- including the one KDE flips you to.
                        "z13_power": power,
                    },
                }
            }
        }

    # ── deriving the three limits ─────────────────────────────────────────
    def _limits(self, watts, boost, ppt_mode, sppt, fppt):
        if not boost:
            # Boost off means exactly that: one flat limit.
            return watts, watts
        if ppt_mode == "manual":
            s = _clamp(sppt, MIN_TDP, MAX_SPPT)
            f = _clamp(fppt, MIN_TDP, MAX_FPPT)
        else:
            s = min(round(watts * SPPT_RATIO), MAX_SPPT)
            f = min(round(watts * FPPT_RATIO), MAX_FPPT)
        # The firmware REJECTS a lower tier that exceeds a higher one rather
        # than clamping it, so a nonsensical order would silently apply
        # nothing at all. Raise rather than reject.
        s = max(s, watts)
        f = max(f, s)
        return s, f

    # ── per-tick ──────────────────────────────────────────────────────────
    def prepare(self, conf: Config):
        # One try/except around everything: hhd's run_plugin_cmd REMOVES a
        # plugin that raises, permanently, not just for this tick.
        try:
            if PRESET not in conf or CUSTOM_TDP not in conf:
                return
            self._power_change(conf)
            self._sync(conf)
        except Exception as e:
            logger.warning(f"TDP preset prepare failed: {e}")

    def _current(self, conf: Config):
        pid = conf[PRESET].to(str)
        watts = _clamp(conf[CUSTOM_TDP].to(int), MIN_TDP, MAX_TDP)
        boost = conf[CUSTOM_BOOST].to(bool)
        ppt = conf.get(PPT_MODE, "auto")
        sppt = conf.get(PPT_SPPT, 0)
        fppt = conf.get(PPT_FPPT, 0)
        return pid, watts, boost, ppt, sppt, fppt

    def _load_into(self, conf: Config, pid: str):
        """Push a stored preset into adjustor's sliders and our PPT control."""
        slot = self._slot(pid)
        if slot is None:
            return
        conf[CUSTOM_TDP] = _clamp(int(slot.get("tdp", 45)), MIN_TDP, MAX_TDP)
        conf[CUSTOM_BOOST] = bool(slot.get("boost", True))
        if PPT_MODE in conf:
            conf[PPT_MODE] = slot.get("ppt", "auto")
            if slot.get("sppt"):
                conf[PPT_SPPT] = slot["sppt"]
            if slot.get("fppt"):
                conf[PPT_FPPT] = slot["fppt"]
        self.old_loaded = self._current(conf)
        logger.info(f"Loaded {pid}: {slot.get('tdp')}W boost={slot.get('boost')}")

    def _sync(self, conf: Config):
        cur = self._current(conf)
        pid = cur[0]

        if self.old_loaded is None:
            # First tick adopts. hhd starting must not change the power limit
            # by itself, and must not overwrite a stored preset either.
            self.old_loaded = cur
            self._publish(conf, cur)
            return

        if pid != self.old_loaded[0]:
            # The preset selection moved: LOAD, do not save.
            self._load_into(conf, pid)
            self._publish(conf, self._current(conf))
            # Selecting a preset means applying it.
            conf[MODE_PATH] = "custom"
            return

        if cur == self.old_loaded:
            return

        # Same preset, different values: the sliders were edited, so SAVE.
        slot = self._slot(pid)
        if slot is not None:
            _, watts, boost, ppt, sppt, fppt = cur
            slot.update({"tdp": watts, "boost": boost, "ppt": ppt,
                         "sppt": sppt, "fppt": fppt})
            self._save_store()
        self.old_loaded = cur
        self._publish(conf, cur)

    def _publish(self, conf: Config, cur):
        """Write the PL2/PL3 targets the patch reads."""
        _, watts, boost, ppt, sppt, fppt = cur
        s, f = self._limits(watts, boost, ppt, sppt, fppt)
        st = self._load_store()
        if st.get("applied") != {"sppt": s, "fppt": f}:
            st["applied"] = {"sppt": s, "fppt": f}
            self._save_store()

    def _power_change(self, conf: Config):
        """Follow the charger, on the EDGE only."""
        if f"{POWER}.mode" not in conf:
            return
        if conf[f"{POWER}.mode"].to(str) != "enabled":
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
        target = conf.get(f"{POWER}.enabled.{key}", "")
        where = "AC" if ac else "battery"

        if target in STOCK_MODES:
            logger.info(f"Power source -> {where}: TDP mode -> {target}")
            conf[MODE_PATH] = target
            return
        if self._slot(target) is None:
            return  # a preset that no longer exists
        logger.info(f"Power source -> {where}: preset -> {target}")
        conf[PRESET] = target
        # Load directly: _sync would see the selection change on the next
        # tick anyway, but the transition is exactly when it must land.
        self._load_into(conf, target)
        self._publish(conf, self._current(conf))
        conf[MODE_PATH] = "custom"

    # ── add / remove ──────────────────────────────────────────────────────
    def _resize(self, conf: Config, delta: int) -> bool:
        st = self._load_store()
        n = len(st["presets"])
        new = _clamp(n + delta, MIN_PRESETS, MAX_PRESETS)
        if new == n:
            logger.info(
                f"Preset count already at its {'maximum' if delta > 0 else 'minimum'} ({n})"
            )
            return False

        if delta > 0:
            # Seed from the active slot, so a new preset starts from something
            # useful rather than a default nobody chose.
            src = self._slot(conf[PRESET].to(str)) or _default_preset(1)
            st["presets"].append(dict(src))
        else:
            st["presets"].pop()
            # If the removed slot was selected, step back, or validate_config
            # would reset the selection and the result would look arbitrary.
            if conf[PRESET].to(str) == _pid(n):
                conf[PRESET] = _pid(new)
                self.old_loaded = None  # force a reload of the new selection
        self._save_store()
        logger.info(f"Preset slots: {n} -> {new}")
        return True

    def update(self, conf: Config):
        try:
            self._tdp_ready = conf.get("hhd.settings.tdp_ready", False)
            if STATUS not in conf:
                return

            rebuild = False
            if conf.get_action(ADD):
                rebuild = self._resize(conf, +1)
            elif conf.get_action(REMOVE):
                rebuild = self._resize(conf, -1)

            # Power Limits is gated on TDP Boost, and hhd fixes the tree when
            # settings() runs -- so a change to the toggle needs a rebuild for
            # the control to appear or go. Compared against what the CURRENT
            # tree was built for, so this fires once per toggle, not per tick.
            boost = conf.get(CUSTOM_BOOST, True)
            if boost != self._gen_boost:
                self._gen_boost = boost
                rebuild = True

            if rebuild:
                self.emit({"type": "settings"})

            self._telemetry(conf)

            if self._patched is None:
                self._patched = _adjustor_is_patched()
                if self._patched is False and not self._warned:
                    self._warned = True
                    logger.error(
                        "adjustor is not carrying the z13-tdp-presets patch: "
                        "presets still work, but PL2/PL3 will be derived by "
                        "adjustor instead. Run hhd-patches/install.sh."
                    )
            conf[STATUS] = "" if self._patched is not False else PATCH_WARNING
        except Exception as e:
            logger.warning(f"TDP preset update failed: {e}")

    def _telemetry(self, conf: Config):
        """Read the limits back from the interface adjustor writes.

        Same contract as the refresh-rate indicator: read the hardware, show
        it, never write it. Only rewritten when the text changes, because
        every config write wakes hhd's long-poll and redraws the UI.
        """
        if APPLIED not in conf:
            return
        vals = [_read_w(a) for a in ("ppt_pl1_spl", "ppt_pl2_sppt", "ppt_fppt")]
        shown = (
            " / ".join(f"{v}" for v in vals) + " W   (PL1 / PL2 / PL3)"
            if all(v is not None for v in vals)
            else "unavailable"
        )
        if shown != self.old_applied:
            self.old_applied = shown
            conf[APPLIED] = shown

    def close(self):
        # Nothing to release: no hardware, no descriptors. Leaving the last
        # preset applied is correct -- the machine keeps the limit chosen,
        # exactly as if hhd were stopped while on the stock Custom mode.
        pass
