#!/usr/bin/env bash
# Install the hhd QAM-toggle patch, its pacman hook, and the Wayland wrapper.
#
#   sudo ./install.sh            # install everything
#   sudo ./install.sh --revert   # undo everything
#
# What each piece does:
#   1. hhd-qam-toggle-patch.py  makes the Armoury button TOGGLE the QAM rather
#                               than only open it (hhd has no Wayland layer
#                               shell, so a 2nd press otherwise just minimises)
#   2. pacman hook              re-applies (1) after every hhd upgrade, which
#                               would silently revert it
#   3. hhd-ui-wayland           runs the Electron UI natively on Wayland so it
#                               is sharp instead of blurry XWayland
set -uo pipefail
SRC="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

if [[ "${1:-}" == "--revert" ]]; then
    python3 "$SRC/hhd-qam-toggle-patch.py" --revert
    rm -f /etc/pacman.d/hooks/99-z13-hhd-qam-toggle.hook \
          /usr/local/lib/z13/hhd-qam-toggle-patch.py \
          /usr/local/bin/hhd-ui-wayland \
          /etc/systemd/system/hhd@.service.d/10-wayland-overlay.conf
    rmdir /etc/systemd/system/hhd@.service.d /usr/local/lib/z13 2>/dev/null
    systemctl daemon-reload
    echo "reverted — restart hhd:  systemctl restart hhd@<user>"
    exit 0
fi

install -Dm755 "$SRC/hhd-qam-toggle-patch.py" /usr/local/lib/z13/hhd-qam-toggle-patch.py
install -Dm644 "$SRC/99-z13-hhd-qam-toggle.hook" /etc/pacman.d/hooks/99-z13-hhd-qam-toggle.hook
install -Dm755 "$SRC/hhd-ui-wayland" /usr/local/bin/hhd-ui-wayland
install -Dm644 "$SRC/10-wayland-overlay.conf" /etc/systemd/system/hhd@.service.d/10-wayland-overlay.conf
echo "  installed hook, patcher and Wayland wrapper"

# Revert first: the patcher is idempotent and would otherwise report
# "already patched" and skip, leaving an OLDER patch in place after an update
# to this tooling. Reverting restores the pristine originals, then we re-apply.
python3 /usr/local/lib/z13/hhd-qam-toggle-patch.py --revert >/dev/null 2>&1
python3 /usr/local/lib/z13/hhd-qam-toggle-patch.py || exit 1
systemctl daemon-reload
echo
echo "  Now restart hhd:   systemctl restart hhd@\${SUDO_USER:-\$USER}"
echo "  Then press the Armoury button twice — 2nd press should DISMISS it."
