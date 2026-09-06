"""z13_firmware -- ASUS firmware attributes for the ROG Flow Z13 (GZ302EA).

Owns the asus-armoury firmware-attribute reads and writes, and exposes one
control of its own:

    boot_sound        Power-on POST chime. Takes effect at the next cold boot.

    panel_overdrive   LCD overdrive. Read/written here, but NOT a control
                      here -- display.py surfaces it as the top tier of the
                      Refresh Rate selector ("180 Hz + Overdrive").

WRITTEN DIRECTLY TO SYSFS, NOT VIA z13ctl-plus
    z13ctl-plus has `paneloverdrive` and `bootsound` subcommands that do the
    same writes, and the CPU plugin next door shells out to it for EPP/boost
    because that path is genuinely non-trivial (amd_pstate mode differences,
    an ordering rule between floor and ceiling). Neither applies here: these
    are one-byte writes to a single file each, with the values enumerated by
    the driver itself.

    Adding a z13ctl-plus dependency for that would be a step in the wrong
    direction. The plan is to phase it out, and every new subprocess call is
    another thing to unpick later -- plus a second writer for a file hhd can
    write itself, which is the class of conflict the whole v0.2.0 release
    existed to close.

WHY OVERDRIVE IS A REFRESH TIER, NOT AN INDEPENDENT TOGGLE
    It shipped briefly as its own switch here, on the reasoning that the
    blur-vs-overshoot trade is a judgement about content rather than about
    Hz. That undersold the physics. Overdrive's benefit scales with refresh
    -- a 60 Hz frame lasts 16.7 ms and most pixel transitions finish unaided,
    a 180 Hz frame gives them 5.6 ms and they cannot -- and its overshoot is
    tuned for the short frame, so holding it across a long one is exactly
    where inverse ghosting becomes visible.

    Modelling it as the top rung of the Refresh Rate selector therefore makes
    the useful combination one click and the harmful one unreachable. This is
    what G-Helper does; the earlier decision to diverge was the wrong call.

WHY THE VALUES ARE VALIDATED AGAINST possible_values
    The driver publishes what it accepts (`possible_values`, "0;1" here).
    Writing outside that set fails at the firmware level, and asus-armoury
    reports that as a write error rather than clamping -- so an unsupported
    value would look like a dead toggle. The set is read once in __init__ --
    that is, at autodetect() time, before settings() is ever called -- so a
    firmware update that changes it is noticed on the next hhd start rather
    than silently fought.
"""

import logging
import os

from hhd.plugins import HHDPlugin, load_relative_yaml
from hhd.plugins.conf import Config

logger = logging.getLogger(__name__)

ATTR_DIR = "/sys/class/firmware-attributes/asus-armoury/attributes"

# (config leaf, attribute directory name)
# Controls this plugin OWNS as settings. panel_overdrive is intentionally
# absent: display.py drives it as a tier of the Refresh Rate selector, using
# this module's _read/_write. It stays in ATTR checks below, not here.
ATTRS = (("boot_sound", "boot_sound"),)


def _attr_path(attr: str, leaf: str = "current_value") -> str:
    return os.path.join(ATTR_DIR, attr, leaf)


def _read(attr: str) -> bool | None:
    """Current value as a bool, or None if the attribute is unreadable."""
    try:
        with open(_attr_path(attr)) as f:
            return f.read().strip() == "1"
    except Exception:
        return None


def _supported(attr: str) -> bool:
    """True when the driver exposes the attribute and accepts 0 and 1."""
    try:
        with open(_attr_path(attr, "possible_values")) as f:
            vals = f.read().strip().split(";")
        return "0" in vals and "1" in vals
    except Exception:
        return False


def _write(attr: str, on: bool) -> bool:
    try:
        with open(_attr_path(attr), "w") as f:
            f.write("1" if on else "0")
    except Exception as e:
        logger.error(f"Could not write {attr}: {e}")
        return False
    # Read back rather than trust the write. asus-armoury reports a firmware
    # rejection as a write error on some paths and silently keeps the old
    # value on others, so the only reliable check is what the file says now.
    got = _read(attr)
    if got is not on:
        logger.warning(f"{attr} did not take: wrote {on}, reads {got}")
        return False
    return True


class Z13FirmwarePlugin(HHDPlugin):
    def __init__(self) -> None:
        self.name = "z13_firmware"
        self.priority = 9
        self.log = "z13f"
        # Probe overdrive too, so display.py can rely on this module having
        # verified it, even though it is not a control here.
        self.available = [
            a for a in ("panel_overdrive", "boot_sound") if _supported(a)
        ]
        self.old = {}

    def settings(self):
        if not self.available:
            return {}
        sets = load_relative_yaml("./firmware_settings.yml")

        out = {}
        # panel_overdrive is deliberately NOT a control here. It is a tier on
        # the Refresh Rate selector in display.py ("180 Hz + Overdrive"),
        # because its usefulness scales with refresh and pairing it with 60 Hz
        # is the combination worth preventing. This module still owns the
        # sysfs read/write; display.py calls in.
        if "boot_sound" in self.available:
            # Its own first-level container: a POST chime is not a display
            # setting, and at this level a title actually renders.
            out.setdefault("gamemode", {})["z13_firmware"] = sets["z13_firmware"]
        return out

    def open(self, emit, context, *args, **kwargs):
        self.emit = emit

    def update(self, conf: Config):
        for leaf, attr in ATTRS:
            if attr not in self.available:
                continue
            # Every attribute this plugin still owns lives in its own
            # container. panel_overdrive used to be mounted under
            # gamemode.display and needed a second path here; it became a tier
            # of the Refresh Rate selector instead and is applied from
            # display.py, so the branch was dead.
            path = f"gamemode.z13_firmware.{leaf}"
            try:
                new = conf[path].to(bool)
            except Exception:
                continue

            # Adopt the live value on the first tick rather than writing one.
            # Without this, startup would push the YAML default at hardware
            # the user may have set deliberately -- the same mistake the
            # pstate default made before it was matched to the boot cmdline.
            if leaf not in self.old:
                live = _read(attr)
                if live is not None and live != new:
                    conf[path] = live
                    self.old[leaf] = live
                    continue
                self.old[leaf] = new if live is None else live
                continue

            if new != self.old[leaf]:
                self.old[leaf] = new
                if _write(attr, new):
                    logger.info(f"{attr} -> {'on' if new else 'off'}")
