from typing import Sequence

from hhd.plugins import HHDPlugin

from .plugin import Z13CpuPlugin
from .display import Z13DisplayPlugin

# DMI product_name substring identifying the hardware these plugins are for.
# This machine reports "ROG Flow Z13 GZ302EA_GZ302EA"; the trailing SKU differs
# between units, so match a prefix rather than the whole string.
#
# Substring, and this exact key, to match upstream: adjustor looks its ASUS
# table up with `for k, v in ASUS_DATA.items(): if k in prod` (adjustor/hhd.py
# :287-289) against the key "ROG Flow Z13 GZ302". Using the same test means a
# machine adjustor considers a GZ302 is a machine these plugins load on, and
# there is no window where one matches and the other does not.
DMI_MATCH = "ROG Flow Z13 GZ302"
DMI_PATH = "/sys/devices/virtual/dmi/id/product_name"


def _is_z13() -> bool:
    """True only on the hardware these plugins actually drive.

    Without this the package loaded on ANY machine hhd runs on, and both
    plugins write hardware unconditionally: plugin.py drives amd_pstate,
    cpufreq boost and every cpufreq policy, and display.py drives
    kscreen-doctor against a hardcoded eDP-1 at a hardcoded 2560x1600. On
    other hardware that is at best a set of dead controls and at worst a
    plugin fighting the real driver for the same sysfs files.

    Fails CLOSED: if the DMI node cannot be read the plugins do not load,
    because "unknown hardware" is exactly the case that must not be assumed
    to be a Z13.

    That try/except is the one deliberate deviation from upstream's shape.
    hhd/device/rog_ally/__init__.py does the same substring match on the same
    file but opens it bare, so a missing or unreadable DMI node raises out of
    autodetect() instead of simply declining to load. Everything else here --
    the `if len(existing): return existing` guard, the substring rather than
    exact match, returning a plugin list on match and [] otherwise -- follows
    that module exactly.
    """
    try:
        with open(DMI_PATH) as f:
            return DMI_MATCH in f.read()
    except Exception:
        return False


def autodetect(existing: Sequence[HHDPlugin]) -> Sequence[HHDPlugin]:
    if len(existing):
        return existing
    if not _is_z13():
        return []
    return [Z13CpuPlugin(), Z13DisplayPlugin()]
