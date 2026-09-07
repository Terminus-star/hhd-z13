"""Direct ASUS Aura RGB control: no daemon, no HTTP, no external tool.

WHY THIS EXISTS
    RGB on this machine had two writers -- hhd's own aura driver and the
    z13ctl-plus daemon -- and they cannot be made to share. The daemon
    repaints its saved lighting from FOUR ungated paths (logind
    PrepareForSleep, a UPower lid signal, a 2-second hotplug poll, and its own
    startup), none of which has a flag, config file or environment variable to
    turn off. Setting `lighting.enabled = false` makes it worse rather than
    quieter: the daemon then writes TurnOff instead, which is six more packets
    contending with whoever else is writing.

    So the daemon has to go, and its one irreplaceable job -- painting the
    progress LEDs from a systemd-sleep hook, where hhd's plugin loop cannot be
    reached -- has to live somewhere else. That is this module.

TWO ENTRY POINTS, ONE IMPLEMENTATION
    In-process:  from hhd_z13 import aura; aura.apply(color="0a0014")
    From a hook: python3 -m hhd_z13.aura --color 0a0014 --brightness low

    The sleep hooks need the second form because they run as root shell
    scripts outside hhd. It is still "inside hhd" in the sense that matters:
    one package, one protocol implementation, no third-party binary.

WHY OUTPUT REPORTS RATHER THAN hhd's FEATURE REPORTS
    hhd's driver sends feature reports through libhidapi and pays for it: an
    init handshake it reads back and asserts, then a commit DEFERRED A FULL
    SECOND (RGB_APPLY_DELAY), plus a 1.5s revert timer on one-off colours.
    That is the entire reason RGB through hhd felt slower than z13ctl-plus.

    This writes plain 64-byte output reports straight to /dev/hidrawN: no
    library, no read-back, no waiting, nothing deferred. Measured cost is one
    write() syscall per packet. It is also stdlib-only, which is what lets a
    sleep hook call it without importing hhd at all.

PROTOCOL SOURCE
    Ported from z13ctl-plus's internal/aura and internal/hid, which in turn
    cite g-helper's Aura.cs. Every packet below was read from that source
    rather than inferred. z13ctl-plus/PROTOCOL.md is the reference document.
"""

import argparse
import array
import fcntl
import glob
import logging
import os
import re
import struct
import sys

logger = logging.getLogger(__name__)

# ── discovery ─────────────────────────────────────────────────────────────
# Matched against HID_ID= in each hidraw node's uevent. Vendor 0x0B05 is ASUS;
# 0x1A30 is the keyboard backlight, 0x18C6 the edge lightbar.
AURA_IDS = {
    "keyboard": "0003:00000B05:00001A30",
    "lightbar": "0003:00000B05:000018C6",
}

# _IOR('H', 0x01, int) and _IOR('H', 0x02, struct hidraw_report_descriptor),
# the latter being 4 + 4096 bytes.
HIDIOCGRDESCSIZE = 0x80044801
HIDIOCGRDESC = 0x90044802

REPORT_SIZE = 64
AURA_ID = 0x5D

# ── protocol constants ────────────────────────────────────────────────────
# NOTE ON NAMING: hhd's own driver calls 0x02 "rainbow" and 0x03 "spiral";
# z13ctl-plus and g-helper call 0x02 "cycle" and 0x03 "rainbow". Same wire
# bytes, swapped labels. These are z13ctl-plus's names, because the sleep
# hooks were written against that vocabulary -- translating hhd's config by
# NAME rather than by byte would visibly swap the two effects.
MODES = {
    "static": 0x00,
    "breathe": 0x01,
    "cycle": 0x02,
    "rainbow": 0x03,
    "strobe": 0x0A,
}
SPEEDS = {"slow": 0xE1, "normal": 0xEB, "fast": 0xF5}
BRIGHTNESS = {"off": 0, "low": 1, "medium": 2, "high": 3}

# Zone byte in the 0xB3 packet. There is no "all zones" wildcard: each zone
# needs its own packet, and a physical device ignores the other zone's byte.
ZONES = (0, 1)


def _hidraw_path(sysfs_dir):
    """/sys/class/hidraw/hidrawN/device/uevent -> /dev/hidrawN

    Must match hidraw followed by DIGITS. The class directory itself is also
    called "hidraw", so a plain startswith() picks that up first and yields
    "/dev/hidraw", which does not exist -- every device then looks absent.
    """
    for p in sysfs_dir.split("/"):
        if re.fullmatch(r"hidraw\d+", p):
            return "/dev/" + p
    return None


def _has_aura_report(fd):
    """True if the report descriptor declares Report ID 0x5D.

    Load-bearing, not belt-and-braces: each USB device exposes several hidraw
    interfaces and only one carries Aura. Writing to the wrong one is silently
    accepted and does nothing, which is a miserable thing to debug.

    In HID short-form encoding a Report ID item is 0x85 followed by the id.
    """
    try:
        size_buf = array.array("i", [0])
        fcntl.ioctl(fd, HIDIOCGRDESCSIZE, size_buf, True)
        size = size_buf[0]
        if size <= 0 or size > 4096:
            return False
        desc = array.array("B", struct.pack("I", size) + b"\x00" * 4096)
        fcntl.ioctl(fd, HIDIOCGRDESC, desc, True)
        value = desc[4:4 + size]
        return any(value[i] == 0x85 and value[i + 1] == AURA_ID
                   for i in range(len(value) - 1))
    except Exception:
        return False


def find_devices(target=None):
    """[(name, path)] for the Aura nodes. `target` filters to one name.

    Never raises: an absent or unreadable device yields an empty list, because
    every caller here would rather do nothing than fail.
    """
    found = []
    want = AURA_IDS if target is None else {
        k: v for k, v in AURA_IDS.items() if k == target
    }
    for uevent in sorted(glob.glob("/sys/class/hidraw/hidraw*/device/uevent")):
        try:
            with open(uevent) as f:
                text = f.read()
        except Exception:
            continue
        for name, hid_id in want.items():
            if f"HID_ID={hid_id}" not in text:
                continue
            path = _hidraw_path(uevent)
            if not path or not os.path.exists(path):
                continue
            try:
                fd = os.open(path, os.O_RDWR)
            except Exception:
                continue
            try:
                if _has_aura_report(fd):
                    found.append((name, path))
            finally:
                os.close(fd)
    return found


class Aura:
    """Open Aura nodes and write packets to them.

    Holds the fds open for its lifetime. That is the other half of why the Go
    daemon was quick: reopening /dev/hidrawN per call is the slow path.
    """

    def __init__(self, target=None, dry_run=False):
        self.dry_run = dry_run
        self.sent = []          # hex strings, for --dry-run and for tests
        self.fds = []
        self.names = []
        for name, path in find_devices(target):
            if dry_run:
                self.fds.append(None)
                self.names.append(name)
                continue
            try:
                self.fds.append(os.open(path, os.O_RDWR))
                self.names.append(name)
            except Exception as e:
                logger.warning(f"aura: cannot open {path}: {e}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        for fd in self.fds:
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass
        self.fds = []

    @property
    def ok(self):
        return bool(self.names)

    def _write(self, payload):
        """Zero-pad to 64 bytes and write to every open node."""
        buf = bytes(payload) + b"\x00" * (REPORT_SIZE - len(payload))
        self.sent.append(bytes(payload).hex())
        if self.dry_run:
            return True
        good = False
        for fd in self.fds:
            try:
                os.write(fd, buf)
                good = True
            except Exception as e:
                logger.warning(f"aura: write failed: {e}")
        return good

    # ── the four init packets ─────────────────────────────────────────────
    def init(self):
        """Sent before every apply, exactly as z13ctl-plus does.

        Not cached deliberately. The Go daemon re-inits on every apply and has
        run that way in production on this machine, so re-sending is known
        harmless here -- whereas caching would need us to know when the
        firmware forgets, which we cannot observe.
        """
        self._write([AURA_ID, 0xB9])
        # The ']' of "]ASUS Tech.Inc." IS 0x5D, i.e. it is already the report
        # ID. Do not prepend another one.
        self._write(b"]ASUS Tech.Inc.")
        self._write([AURA_ID, 0x05, 0x20, 0x31, 0x00, 0x1A])
        # Z13-specific. Without it the lightbar ignores every SetMode below.
        self._write([AURA_ID, 0xC0, 0x03, 0x01])

    def set_power(self, on=True):
        """Which power states the FIRMWARE keeps each LED group lit in.

        This is not us painting during sleep -- it delegates to the EC, which
        is the only thing still running once userspace is frozen. bar bits are
        awake/boot/awake/sleep/shutdown; on the Z13 lid and logo are merged.
        """
        if on:
            keyb, bar, lid, rear = 0xFF, 0x1F, 0xFF, 0xFF
        else:
            keyb = bar = lid = rear = 0x00
        self._write([AURA_ID, 0xBD, 0x01, keyb, bar, lid, rear, 0xFF])

    def set_brightness(self, level):
        """level 0..3.

        Report ID 0x5D here. hhd's driver sends the SAME payload under 0x5A
        instead; both apparently work, but 0x5D is what has actually been
        running on this hardware, so it is the one to trust.
        """
        self._write([AURA_ID, 0xBA, 0xC5, 0xC4, max(0, min(3, int(level)))])

    def set_mode(self, zone, mode, rgb, rgb2=(0, 0, 0), speed=0xEB):
        r, g, b = rgb
        r2, g2, b2 = rgb2
        # randFlag: 0xFF hands colour choice to the DEVICE when the primary
        # colour is all-zero. So 000000 does NOT mean "dark" -- it means
        # "surprise me". Anything wanting black must use brightness off.
        if (r, g, b) == (0, 0, 0):
            rand = 0xFF
        elif mode == MODES["breathe"]:
            rand = 0x01
        else:
            rand = 0x00
        self._write([AURA_ID, 0xB3, zone, mode, r, g, b, speed,
                     0x00,          # direction
                     rand, r2, g2, b2])
        # Commit. Without both of these the device accepts the packet and
        # displays nothing.
        self._write([AURA_ID, 0xB5, 0x00, 0x00, 0x00])
        self._write([AURA_ID, 0xB4])


def _rgb(color):
    """'0a0014' or 'RRGGBB' -> (r, g, b). Raises ValueError on nonsense."""
    c = str(color).strip().lstrip("#")
    if len(c) != 6:
        raise ValueError(f"colour must be 6 hex digits, got {color!r}")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def apply(color="ff0000", brightness="high", mode="static", speed="normal",
          color2="000000", device=None, dry_run=False, brightness_only=False):
    """Set the LEDs. Returns True if anything was written.

    brightness_only omits the SetMode packets, so the current colour and
    effect survive. That is what a bare brightness change has to do -- sending
    a colour along with it is how the old shell helper silently overrode every
    `led_brightness` that preceded a `led_color`.

    Never raises -- callers are a plugin loop that would drop the plugin on an
    exception, and a sleep hook that must not abort a hibernate over an LED.
    """
    try:
        lvl = BRIGHTNESS.get(str(brightness).lower())
        if lvl is None:
            raise ValueError(
                f"unknown brightness {brightness!r}; have {sorted(BRIGHTNESS)}")
        if not brightness_only:
            rgb = _rgb(color)
            rgb2 = _rgb(color2)
            m = MODES.get(str(mode).lower())
            if m is None:
                raise ValueError(f"unknown mode {mode!r}; have {sorted(MODES)}")
            sp = SPEEDS.get(str(speed).lower(), SPEEDS["normal"])

        with Aura(target=device, dry_run=dry_run) as a:
            if not a.ok:
                logger.warning("aura: no Aura device found")
                return False
            a.init()
            a.set_power(True)
            a.set_brightness(lvl)
            if not brightness_only:
                # Both zones always, as g-helper does: each physical device
                # ignores the byte that is not its own.
                for z in ZONES:
                    a.set_mode(z, m, rgb, rgb2, sp)
            if dry_run:
                for pkt in a.sent:
                    print(f"  {pkt}")
                print(f"  ({len(a.sent)} packets to {', '.join(a.names)})")
            return True
    except Exception as e:
        logger.warning(f"aura: apply failed: {e}")
        return False


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="python3 -m hhd_z13.aura",
        description="Direct ASUS Aura RGB control (no daemon, no external tool)")
    p.add_argument("--color", default="ff0000", help="RRGGBB hex")
    p.add_argument("--color2", default="000000", help="RRGGBB, breathe only")
    p.add_argument("--brightness", default="high", choices=sorted(BRIGHTNESS))
    p.add_argument("--mode", default="static", choices=sorted(MODES))
    p.add_argument("--speed", default="normal", choices=sorted(SPEEDS))
    p.add_argument("--device", default=None, choices=sorted(AURA_IDS),
                   help="default: both")
    p.add_argument("--dry-run", action="store_true",
                   help="print packets, touch no hardware")
    p.add_argument("--brightness-only", action="store_true",
                   help="change brightness, leave colour and effect alone")
    p.add_argument("--list", action="store_true", help="list Aura devices")
    a = p.parse_args(argv)

    if a.list:
        devs = find_devices()
        for name, path in devs:
            print(f"  {name:9s} {path}")
        if not devs:
            print("  (none found)")
        return 0 if devs else 1

    ok = apply(color=a.color, brightness=a.brightness, mode=a.mode,
               speed=a.speed, color2=a.color2, device=a.device,
               dry_run=a.dry_run, brightness_only=a.brightness_only)
    if not ok:
        print("aura: nothing written", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
