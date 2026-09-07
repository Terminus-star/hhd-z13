"""TDP presets: saved configurations in adjustor's own TDP Mode dropdown.

THE SHAPE
    Custom is the scratchpad -- TDP, TDP Boost, PL2, PL3 -- and "Add Preset"
    freezes its current values into a new entry in the dropdown, beside
    Silent / Performance / Turbo. Those entries carry no sliders: a saved
    preset you could edit in place is just Custom with extra steps.

    Selecting one writes its values into adjustor's own custom.tdp /
    custom.boost and lets adjustor's custom apply path do every hardware
    write. The z13-tdp-presets patch widens that path to accept our mode
    names; nothing else about adjustor changes.

WHY PRESET MODES ARE VIABLE AGAIN
    An earlier attempt lost to KDE: adjustor's ppd handler mapped an incoming
    power profile onto a stock mode and force-wrote it, replacing a preset a
    second or two after it was selected. That was adjustor answering an ECHO
    of its own report -- applying 9 W emits energy='power', the PPD server
    publishes power-saver, and it came straight back as an event. The patch's
    ppd-echo entry ignores events matching what adjustor last reported, so a
    selected mode stays selected.

WHY THE STORE IS NOT IN /etc/hhd
    hhd registers F_NOTIFY with DN_MODIFY on its config directory
    (__main__.py), so ANY write there raises SIGPOLL and triggers a full
    "Reloading configuration". With the store in /etc/hhd that meant a
    settings reload per preset write -- 65 in 90 minutes -- each one
    rebuilding the tree and cycling the overlay, which is what kept jostling
    Steam. It lives in /var/lib now, which hhd does not watch.

PL2 / PL3
    Computed from this panel's own firmware figures: it ships 60 / 75 / 86 W
    for PL1 / PL2 / PL3, so they track the sustained value at 1.25x and
    1.433x, capped at its 92 / 93 W maxima. Moving the TDP slider recomputes
    both; nudging them afterwards sticks until it moves again.

    TDP Boost is the gate. With it off all three limits are the same number,
    so the sliders are omitted from the tree entirely rather than shown inert.

    Deliberately not left to adjustor, whose derivation is built around a
    fixed 35 W ceiling: this machine sustains up to 65 W, so from 35 W upward
    that ceiling sits below the sustained value itself and its
    min(max(steady, ceiling), steady * ratio) collapses to steady.
"""

import glob
import json
import logging
import os
import time
from copy import deepcopy

from hhd.plugins import HHDPlugin, load_relative_yaml
from hhd.plugins.conf import Config

from .plugin import _on_ac

logger = logging.getLogger(__name__)

MAX_PRESETS = 8

# How long a power-change selection is held against anything that moves it.
#
# Unplugging fires TWO policies at once: ours picks a mode, and KDE
# independently switches its own power profile. KDE's arrives as a "ppd" event
# a moment later, adjustor maps it onto a stock mode, and its update()
# FORCE-writes that over the config -- after our prepare(), so it wins.
# Measured: selecting z13_p1 (9 W) on unplug, and 40 W -- quiet's wattage --
# applied two seconds later.
#
# ppd-echo cannot help here. That entry suppresses adjustor answering an echo
# of its OWN report; this is a genuine desktop-initiated change that merely
# happens to be reacting to the same unplug we are. So the resolution has to
# be on our side: hold the choice across the transition's aftershocks, then
# stop, so a profile picked by hand a moment later still wins.
HOLD_S = 15.0

# NOT /etc/hhd: hhd watches that directory and reloads its whole configuration
# on any write there. See the module docstring.
STORE_PATH = "/var/lib/hhd-z13/presets.json"

# adjustor's three stock profiles. LABELS ARE ADJUSTOR'S, NOT ITS KEYS, which
# do not agree: `balanced` is displayed "Performance" and `performance` is
# displayed "Turbo", following Armoury Crate.
STOCK_MODES = ("quiet", "balanced", "performance")
STOCK_LABELS = {"quiet": "Silent", "balanced": "Performance", "performance": "Turbo"}

# adjustor's Z1_DATA min_tdp/max_tdp, and what it clamps to while "Enforce
# Device TDP Limits" is on. Matching exactly means its clamp never fires.
MIN_TDP, MAX_TDP = 5, 65

# This panel's own figures, from
# /sys/class/firmware-attributes/asus-armoury/attributes/:
#   ppt_pl1_spl  default 60  max 80
#   ppt_pl2_sppt default 75  max 92
#   ppt_pl3_fppt default 86  max 93
FW_PL1, FW_PL2, FW_PL3 = 60, 75, 86
SPPT_RATIO = FW_PL2 / FW_PL1        # 1.25
FPPT_RATIO = FW_PL3 / FW_PL1        # 1.4333...
MAX_SPPT, MAX_FPPT = 92, 93

NBWMI = "/sys/devices/platform/asus-nb-wmi"

BASE = "tdp.asus"
MODE_PATH = f"{BASE}.tdp_v2.mode"
CUSTOM = f"{BASE}.tdp_v2.custom"
CUSTOM_TDP = f"{CUSTOM}.tdp"          # adjustor's slider
CUSTOM_BOOST = f"{CUSTOM}.boost"      # adjustor's toggle, and the PL2/PL3 gate
SPPT = f"{CUSTOM}.z13_sppt"
FPPT = f"{CUSTOM}.z13_fppt"
# A sibling of the TDP Mode selector, not a child of Custom: the reading
# matters most while a preset or a stock profile is active, which is exactly
# when Custom's own sliders are not on screen to read it off.
APPLIED = f"{BASE}.z13_applied"
ADD = f"{CUSTOM}.z13_add"
STATUS = f"{CUSTOM}.z13_status"
POWER = f"{BASE}.z13_power"

PRESET_PREFIX = "z13_p"
PATCH_SENTINEL = "z13-tdp-presets"
ADJUSTOR_ASUS_GLOB = "/usr/lib/python3*/site-packages/adjustor/drivers/asus/__init__.py"
PATCH_WARNING = "TDP presets need the adjustor patch — run hhd-patches/install.sh"


def _pid(i):
    return f"{PRESET_PREFIX}{i}"


def _clamp(v, lo, hi):
    return min(max(v, lo), hi)


def _read_w(attr):
    """One PPT value from asus-nb-wmi, or None. Never raises."""
    try:
        with open(os.path.join(NBWMI, attr)) as f:
            return int(f.read().strip())
    except Exception:
        return None


def _adjustor_is_patched():
    """None means unanswerable -- make no claim unless we actually know."""
    hits = sorted(glob.glob(ADJUSTOR_ASUS_GLOB))
    if not hits:
        return None
    try:
        with open(hits[-1]) as f:
            return PATCH_SENTINEL in f.read()
    except Exception:
        return None


def _derive(watts, boost):
    """PL2/PL3 from this panel's own firmware proportions."""
    if not boost:
        # Boost off means exactly that: one flat limit.
        return watts, watts
    return (min(round(watts * SPPT_RATIO), MAX_SPPT),
            min(round(watts * FPPT_RATIO), MAX_FPPT))


def _order(watts, sppt, fppt):
    """The firmware REJECTS a lower tier that exceeds a higher one rather than
    clamping it, so a nonsensical order silently applies nothing at all."""
    sppt = max(_clamp(sppt, MIN_TDP, MAX_SPPT), watts)
    return sppt, max(_clamp(fppt, MIN_TDP, MAX_FPPT), sppt)


class Z13TdpPlugin(HHDPlugin):
    def __init__(self) -> None:
        self.name = "z13_tdp"
        # Above adjustor's 6, which puts our prepare() ahead of its update()
        # in the same tick: hhd runs prepare in DESCENDING priority and update
        # in ascending, and adjustor implements no prepare().
        self.priority = 11
        self.log = "z13t"

        self._tdp_ready = False
        self._store = None
        self._gen_boost = True     # boost value the current tree was built for
        self._gen_n = 0            # preset count the current tree was built for

        self.old_ac = None         # None = no previous source to compare to
        self._hold = None          # (mode, deadline) while a transition settles
        # Start ARMED. A fresh boot is a power change nobody watched, exactly
        # like the hibernate case notify() exists for: the machine can be shut
        # down on AC and started on battery, and the mode restored from
        # state.yml is whatever the previous session ended on. There is no
        # edge, so an adopt-only first tick would leave the wrong preset in
        # place until the next unplug -- the same symptom, reached by booting
        # instead of resuming. Arming here makes the first tick re-evaluate
        # against the CURRENT source, through the resume path below.
        self._recheck = True
        self.old_state = None      # (mode, watts, boost, sppt, fppt)
        self.old_applied = None    # last telemetry string written
        self._patched = None
        self._warned = False

    def open(self, emit, context, *args, **kwargs):
        self.emit = emit

    def notify(self, events):
        """Watch for a resume.

        The power-change policy is edge-triggered, which is right while the
        system is running and wrong across a suspend: hibernate restores this
        process from the image with old_ac exactly as it was, so a charger
        moved while the machine was OFF produces no edge to act on. Observed
        after a hibernate -- on battery, dc_preset set, and the mode still on
        whatever was selected beforehand, because nothing had transitioned
        from hhd's point of view.

        A resume is therefore treated as "re-evaluate", not "adopt": the
        power source is authoritative again, whether or not it changed while
        we were not running.
        """
        for ev in events:
            if ev.get("type") == "special" and ev.get("event") == "wakeup":
                self._recheck = True

    # ── the store ─────────────────────────────────────────────────────────
    def _load(self):
        if self._store is not None:
            return self._store
        data = None
        try:
            with open(STORE_PATH) as f:
                data = json.load(f)
        except FileNotFoundError:
            pass
        except Exception as e:
            # Corrupt store: start clean rather than refuse to run.
            logger.warning(f"Could not read {STORE_PATH}, starting fresh: {e}")
        if not isinstance(data, dict) or not isinstance(data.get("presets"), list):
            data = {"presets": [], "applied": {"sppt": 0, "fppt": 0}}
        self._store = data
        return data

    def _save(self):
        try:
            os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
            tmp = STORE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._store, f, indent=2)
            os.replace(tmp, STORE_PATH)
        except Exception as e:
            logger.error(f"Could not write {STORE_PATH}: {e}")

    def _preset(self, mode):
        if not mode.startswith(PRESET_PREFIX):
            return None
        try:
            i = int(mode[len(PRESET_PREFIX):]) - 1
        except Exception:
            return None
        ps = self._load()["presets"]
        return ps[i] if 0 <= i < len(ps) else None

    # ── settings ──────────────────────────────────────────────────────────
    def settings(self):
        if not self._tdp_ready:
            return {}

        ps = self._load()["presets"]
        n = min(len(ps), MAX_PRESETS)
        self._gen_n = n
        sets = load_relative_yaml("./tdp_settings.yml")
        kids = sets["z13_custom_children"]

        # Boost off means PL1 = PL2 = PL3, so there is nothing to configure.
        # Drop the sliders rather than render them inert. update() rebuilds
        # when the toggle moves.
        if not self._gen_boost:
            kids.pop("z13_sppt", None)
            kids.pop("z13_fppt", None)

        # Saved presets, as modes beside adjustor's own. The wattage goes in
        # the title, which is honest here precisely because a saved preset is
        # immutable -- it cannot go stale without a rebuild that regenerates
        # this title anyway.
        modes = {}
        for i, p in enumerate(ps[:n], start=1):
            c = deepcopy(sets["preset_template"])
            w, b = int(p.get("tdp", 45)), bool(p.get("boost", True))
            c["title"] = f"Preset {i} · {w} W" + ("" if b else " (no boost)")
            modes[_pid(i)] = c

        opts = {_pid(i): f"Preset {i} · {int(p.get('tdp', 45))} W"
                for i, p in enumerate(ps[:n], start=1)}
        opts.update({k: STOCK_LABELS[k] for k in STOCK_MODES})
        power = sets["z13_power"]
        ac = power["modes"]["enabled"]["children"]["ac_preset"]
        dc = power["modes"]["enabled"]["children"]["dc_preset"]
        ac["options"], dc["options"] = dict(opts), dict(opts)
        first = _pid(1) if n else "balanced"
        ac["default"], dc["default"] = first, _pid(min(2, n)) if n else "quiet"

        tdp_v2 = {"type": "mode",
                  "modes": {"custom": {"type": "container", "children": kids}}}
        tdp_v2["modes"].update(modes)

        return {
            "tdp": {
                "asus": {
                    # `type` required at EVERY level or merge_reduce refuses to
                    # union and the contribution is silently dropped. Titles
                    # omitted so adjustor's own survive pick_tag.
                    "type": "container",
                    "children": {
                        "tdp_v2": tdp_v2,
                        "z13_applied": sets["z13_applied"],
                        "z13_power": power,
                    },
                }
            }
        }

    # ── per-tick ──────────────────────────────────────────────────────────
    def prepare(self, conf: Config):
        # One try/except around everything: hhd's run_plugin_cmd REMOVES a
        # plugin that raises, permanently, not just for this tick.
        try:
            # Every key _read() touches, not just two of them. A settings
            # rebuild replaces the tree in place, so there is a window where
            # some of adjustor's custom children are visible and others are
            # not -- observed once as "prepare failed: 'boost'" right after a
            # restart. Caught and logged rather than fatal, but a resume is
            # exactly when that window is hit, so it is guarded instead.
            if any(k not in conf for k in (MODE_PATH, CUSTOM_TDP, CUSTOM_BOOST)):
                return
            self._hold_choice(conf)
            self._power_change(conf)
            self._sync(conf)
        except Exception as e:
            logger.warning(f"TDP preset prepare failed: {e}")

    def _read(self, conf):
        mode = conf[MODE_PATH].to(str)
        watts = _clamp(conf[CUSTOM_TDP].to(int), MIN_TDP, MAX_TDP)
        boost = conf[CUSTOM_BOOST].to(bool)
        sppt = conf.get(SPPT, 0) or 0
        fppt = conf.get(FPPT, 0) or 0
        return mode, watts, boost, sppt, fppt

    def _publish(self, watts, sppt, fppt):
        """Hand the limits to adjustor, via the file its patch reads."""
        st = self._load()
        want = {"sppt": sppt, "fppt": fppt}
        if st.get("applied") != want:
            st["applied"] = want
            self._save()

    def _apply_preset(self, conf, mode):
        p = self._preset(mode)
        if p is None:
            return
        w = _clamp(int(p.get("tdp", 45)), MIN_TDP, MAX_TDP)
        b = bool(p.get("boost", True))
        s, f = _order(w, int(p.get("sppt") or 0) or _derive(w, b)[0],
                      int(p.get("fppt") or 0) or _derive(w, b)[1])
        conf[CUSTOM_TDP] = w
        conf[CUSTOM_BOOST] = b
        if SPPT in conf:
            conf[SPPT], conf[FPPT] = s, f
        self._publish(w, s, f)
        self.old_state = (mode, w, b, s, f)
        logger.info(f"Applied {mode}: {w}W boost={b} PL2={s} PL3={f}")

    def _sync(self, conf):
        cur = self._read(conf)
        mode, watts, boost, sppt, fppt = cur

        if self.old_state is None:
            # First tick adopts the sustained value -- hhd starting must not
            # change what the machine is running at. The two boost limits are
            # a different case: they are DERIVED from it, and a stored pair
            # left over from another wattage (or a default that was only ever
            # right for 45 W) would show limits with no relationship to the
            # slider above them. Bring them into line here so the panel is
            # coherent the moment it opens.
            self.old_state = cur
            if mode == "custom":
                s, f = _order(watts, *_derive(watts, boost))
                if SPPT in conf and (s, f) != (sppt, fppt):
                    conf[SPPT], conf[FPPT] = s, f
                self._publish(watts, s, f)
                self.old_state = (mode, watts, boost, s, f)
            return
        if cur == self.old_state:
            return

        old_mode, old_w, old_b, _, _ = self.old_state

        if mode.startswith(PRESET_PREFIX):
            if mode != old_mode:
                # Newly selected: apply the preset's own values. Returns
                # immediately -- _apply_preset sets old_state to the values it
                # just wrote, and falling through would overwrite that with
                # the pre-apply snapshot, so the very next tick would see a
                # difference and fork to Custom on its own.
                self._apply_preset(conf, mode)
                return
            elif (watts, boost) != (old_w, old_b):
                # Edited in place. This used to put the preset's values back,
                # which made the TDP slider look BROKEN: drag it while a
                # preset is selected and the number snaps back with nothing
                # said. Reported as "TDP is unresponsive, hard locked to 9W".
                #
                # Immutability was the right instinct -- a preset the user can
                # silently redefine by brushing a slider is not a saved
                # configuration -- but reverting is the wrong way to enforce
                # it. Fork to Custom instead, carrying the value they just
                # dialled in: the preset on disk stays exactly as saved, and
                # the edit does what they plainly meant it to do.
                #
                # Falls THROUGH to the Custom handling below rather than
                # returning. Clearing old_state instead would make the next
                # tick take the first-tick adopt path, and a preset selected
                # before that tick would then be adopted rather than applied
                # -- the slider would work but choosing a preset would not.
                # Falling through also recomputes PL2/PL3 for the new PL1 in
                # this same tick, so the panel is never briefly inconsistent.
                logger.info(f"Edited while on {mode}; switching to Custom at {watts}W")
                conf[MODE_PATH] = "custom"
                mode = "custom"
            else:
                self.old_state = cur
                return

        if mode != "custom":
            # A stock profile. adjustor owns it outright; publish nothing so
            # its own derivation applies.
            self.old_state = cur
            return

        # Custom, the scratchpad. PL2/PL3 follow PL1 whenever PL1 or boost
        # moves -- that is what keeps them from drifting into limits unrelated
        # to the sustained value -- and stick when nudged on their own.
        if watts != old_w or boost != old_b or not sppt or not fppt:
            sppt, fppt = _derive(watts, boost)
        sppt, fppt = _order(watts, sppt, fppt)
        if SPPT in conf and (sppt, fppt) != (cur[3], cur[4]):
            conf[SPPT], conf[FPPT] = sppt, fppt
        self._publish(watts, sppt, fppt)
        self.old_state = (mode, watts, boost, sppt, fppt)

    def _hold_choice(self, conf):
        """Re-assert a power-change selection while the transition settles.

        Checked BEFORE the edge logic, because what overwrites us arrives
        seconds after the edge -- by which point old_ac is already updated and
        nothing would fire again.
        """
        if self._hold is None:
            return
        target, until = self._hold
        if time.monotonic() >= until:
            self._hold = None
            return
        if conf[MODE_PATH].to(str) == target:
            return
        logger.info(f"TDP mode moved off {target} during the power transition; re-asserting")
        conf[MODE_PATH] = target
        if target.startswith(PRESET_PREFIX):
            self._apply_preset(conf, target)
        else:
            self.old_state = None

    def _power_change(self, conf):
        """Follow the charger, on the EDGE only."""
        if f"{POWER}.mode" not in conf:
            return
        if conf[f"{POWER}.mode"].to(str) != "enabled":
            # Re-arm, so turning the policy back on adopts the current source
            # rather than firing a stale edge from before it was off.
            self.old_ac = None
            self._hold = None
            # Drop a pending re-evaluation too. With the policy off, a boot or
            # a resume must not leave an apply queued that fires the instant
            # it is switched back on. Enabling adopts the current source; the
            # next real transition is what acts.
            self._recheck = False
            return
        ac = _on_ac()
        if ac is None:
            return  # unreadable: leave everything alone rather than guess

        recheck, self._recheck = self._recheck, False

        if self.old_ac is None:
            self.old_ac = ac
            if not recheck:
                return
        elif ac == self.old_ac and not recheck:
            # Not continuous: a mode picked by hand after a transition stays
            # until the next transition. A resume is the one exception -- see
            # notify() -- because the transition may have happened while this
            # process was not running to see it.
            return
        self.old_ac = ac
        key = "ac_preset" if ac else "dc_preset"
        target = conf.get(f"{POWER}.enabled.{key}", "")
        if target in STOCK_MODES or self._preset(target) is not None:
            logger.info(f"Power source -> {'AC' if ac else 'battery'}: {target}")
            # Armed even when the mode is ALREADY the target: the overwrite
            # lands seconds later, so arming only on a change would leave the
            # common case unprotected.
            self._hold = (target, time.monotonic() + HOLD_S)
            conf[MODE_PATH] = target
            if target in STOCK_MODES:
                self.old_state = None
            else:
                self._apply_preset(conf, target)

    # ── add / remove ──────────────────────────────────────────────────────
    def _add(self, conf):
        st = self._load()
        if len(st["presets"]) >= MAX_PRESETS:
            logger.info(f"Preset list is full ({MAX_PRESETS})")
            return False
        _, w, b, s, f = self._read(conf)
        if not s or not f:
            s, f = _derive(w, b)
        s, f = _order(w, s, f)
        st["presets"].append({"tdp": w, "boost": b, "sppt": s, "fppt": f})
        self._save()
        logger.info(f"Saved preset {len(st['presets'])}: {w}W boost={b} {s}/{f}")
        return True

    def _remove(self, conf, mode):
        st = self._load()
        try:
            i = int(mode[len(PRESET_PREFIX):]) - 1
        except Exception:
            return False
        if not (0 <= i < len(st["presets"])):
            return False
        st["presets"].pop(i)
        self._save()
        # The mode is about to stop existing; validate_config would reset the
        # selection to adjustor's default, which reads as arbitrary. Land on
        # Custom, which is where the values came from.
        conf[MODE_PATH] = "custom"
        self.old_state = None
        logger.info(f"Removed preset {i + 1}")
        return True

    def update(self, conf: Config):
        try:
            self._tdp_ready = conf.get("hhd.settings.tdp_ready", False)
            if STATUS not in conf:
                return

            # Resolve the patch state BEFORE deciding to rebuild.
            if self._patched is None:
                self._patched = _adjustor_is_patched()
                if self._patched is False and not self._warned:
                    self._warned = True
                    logger.error(
                        "adjustor is not carrying the z13-tdp-presets patch: "
                        "saved presets will not apply, and the list cannot "
                        "refresh without an hhd restart. Run "
                        "hhd-patches/install.sh."
                    )

            # A settings rebuild is FATAL to an unpatched adjustor:
            # hhd.settings.tdp_ready has no default, so a rebuild clears it,
            # and adjustor only ever sets it once behind a latched self.init.
            # Everything downstream gates on it, so the whole TDP section
            # disappears until hhd restarts. A control of ours appearing is
            # never worth the user's TDP controls vanishing.
            can_rebuild = self._patched is not False

            rebuild = False
            if conf.get_action(ADD):
                rebuild = self._add(conf)
            else:
                mode = conf[MODE_PATH].to(str)
                if mode.startswith(PRESET_PREFIX) and conf.get_action(
                    f"{BASE}.tdp_v2.{mode}.remove"
                ):
                    rebuild = self._remove(conf, mode)

            boost = conf.get(CUSTOM_BOOST, True)
            if can_rebuild and boost != self._gen_boost:
                self._gen_boost = boost
                rebuild = True

            if rebuild and can_rebuild:
                self.emit({"type": "settings"})
            elif rebuild:
                logger.warning(
                    "Not rebuilding the settings tree: adjustor is unpatched "
                    "and would lose its TDP section. The change is saved and "
                    "shows up after `systemctl restart hhd@<user>`."
                )

            self._telemetry(conf)
            conf[STATUS] = "" if self._patched is not False else PATCH_WARNING
        except Exception as e:
            logger.warning(f"TDP preset update failed: {e}")

    def _telemetry(self, conf):
        """Read the limits back from the interface adjustor writes.

        Only rewritten when the text changes: every config write wakes hhd's
        long-poll and redraws the UI.
        """
        if APPLIED not in conf:
            return
        vals = [_read_w(a) for a in ("ppt_pl1_spl", "ppt_pl2_sppt", "ppt_fppt")]
        shown = (
            " / ".join(str(v) for v in vals) + " W   (PL1 / PL2 / PL3)"
            if all(v is not None for v in vals) else "unavailable"
        )
        if shown != self.old_applied:
            self.old_applied = shown
            conf[APPLIED] = shown

    def close(self):
        # Nothing to release. Leaving the last values applied is correct: the
        # machine keeps the limit chosen, as it would if hhd were stopped
        # while on adjustor's own Custom mode.
        pass
