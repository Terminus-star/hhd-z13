#!/usr/bin/env bash
# v0.4.0 "TDP presets" -- retire the competing PPT writers.
#
#   sudo ./v040-tdp.sh              # DRY RUN -- prints every change, writes nothing
#   sudo ./v040-tdp.sh --apply      # make the changes
#   sudo ./v040-tdp.sh --revert     # restore every backup this script made
#
# Every file is backed up to <file>.z13-v040-orig on first modification.
#
# ══ RUN THE ORDERING GATE FIRST ═════════════════════════════════════════════
# Do NOT --apply until you have confirmed hhd itself re-applies TDP after a
# resume. §2 retires the script that has been doing that, on the strength of a
# static reading of adjustor's debounce clock; z13-hibernate's own
# common.sh:830 records a 2026-08-26 measurement claiming hhd already
# re-asserts at T+4, which contradicts it. One of the two is stale and only
# this machine can say which.
#
#   1. merge and reinstall hhd-z13, run hhd-patches/install.sh, restart hhd
#   2. suspend, wait 30s, resume
#   3. journalctl -u hhd@$TARGET_USER -b | grep -E "Waking up from sleep|Setting tdp value"
#      Expect "Waking up from sleep, resetting TDP after 4 seconds." followed
#      WITHIN ~10s by "Setting tdp value 'steady' to <N>".
#
# If step 3 shows the wake line with no write after it, the clock patch is not
# in place or did not take. Leave §2 alone (DO_HIB=0) -- the presets work
# regardless and you lose nothing by keeping the old restore path.
# ════════════════════════════════════════════════════════════════════════════
#
# WHY THE WRITE GRANT, NOT THE CONFIG
#   z13ctl-plus's resume path re-applies PPT and is NOT gated on its own
#   tdp_active flag -- it force-sets it. So no amount of editing its state.json
#   stops the write. What does stop it: the daemon runs as your USER and can
#   only write those files because this unit chgrp/chmods them. hhd runs as
#   root. Revoking the grant stops z13ctl-plus and leaves adjustor untouched.
#
#   Same shape as v0.2.0 §3, applied to a different file set.

set -uo pipefail

# ---------------------------------------------------------------- switches
DO_PERMS=1     # §1  revoke z13ctl-plus's write grant on the PPT files
DO_HIB=1       # §2  neuter z13-hibernate's tdp-restore.sh
DO_STATE=0     # §3  OFF BY DEFAULT -- clear z13ctl-plus's stored TDP override

TARGET_USER=termis
BAK=.z13-v040-orig

PERMS_UNIT=/etc/systemd/system/z13ctl-plus-perms.service
PPT_GLOB='/sys/devices/platform/asus-nb-wmi/ppt_*'
HIB_SCRIPT=/usr/lib/z13-hibernate/tdp-restore.sh
STATE_JSON=/home/$TARGET_USER/.local/state/z13ctl-plus/state.json
RETIRED_MARK=z13-v040-retired

# ---------------------------------------------------------------- plumbing
MODE=${1:---dry-run}
case "$MODE" in
  --apply|--revert|--dry-run) ;;
  *) echo "usage: $0 [--apply|--revert]  (default: dry run)"; exit 2 ;;
esac
[ "$(id -u)" -eq 0 ] || { echo "ERROR: needs root"; exit 1; }

CHANGED=0
say()   { printf '  %s\n' "$*"; }
head_() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
skip()  { printf '  \033[2m- %s\033[0m\n' "$*"; }
did()   { printf '  \033[32m+ %s\033[0m\n' "$*"; CHANGED=1; }
would() { printf '  \033[33m~ would %s\033[0m\n' "$*"; }
warn()  { printf '  \033[33m! %s\033[0m\n' "$*"; }

backup() {
  [ -e "$1" ] || return 0
  [ -e "$1$BAK" ] && return 0
  [ "$MODE" = "--apply" ] || return 0
  cp -a "$1" "$1$BAK"
}

restore() {
  if [ -e "$1$BAK" ]; then
    cp -a "$1$BAK" "$1" && rm -f "$1$BAK" && did "restored $1"
  else
    skip "no backup for $1"
  fi
}

# ═══════════════════════════════════════════════════════════════════ revert
if [ "$MODE" = "--revert" ]; then
  head_ "Reverting v0.4.0 TDP changes"
  restore "$PERMS_UNIT"
  restore "$HIB_SCRIPT"
  restore "$STATE_JSON"
  systemctl daemon-reload && did "daemon-reload"
  # The unit only runs at boot, and sysfs modes live as long as the device
  # node, so re-grant now rather than making the user reboot to get their
  # z13ctl-plus TDP control back.
  systemctl start z13ctl-plus-perms.service 2>/dev/null \
    && did "re-ran z13ctl-plus-perms.service (grants restored immediately)"
  printf '\n%s\n' "Reverted. A reboot would also have restored the grants."
  exit 0
fi

# ════════════════════════════════════════════════════ §1 z13ctl-plus's grant
if [ "$DO_PERMS" = 1 ]; then
  head_ "§1  Revoke z13ctl-plus's write access to the PPT files"

  if [ ! -e "$PERMS_UNIT" ]; then
    skip "$PERMS_UNIT not installed"
  elif ! grep -q 'asus-nb-wmi/ppt_' "$PERMS_UNIT"; then
    skip "PPT grant already removed from the unit"
  elif [ "$MODE" != "--apply" ]; then
    would "delete the ppt_* ExecStart line from $PERMS_UNIT"
    would "chmod g-w $PPT_GLOB (the live nodes, which the unit edit alone does not change)"
  else
    backup "$PERMS_UNIT"
    sed -i '\#asus-nb-wmi/ppt_#d' "$PERMS_UNIT"
    # Verify the strip, and roll back rather than leave a half-edited unit.
    if grep -q 'asus-nb-wmi/ppt_' "$PERMS_UNIT"; then
      cp -a "$PERMS_UNIT$BAK" "$PERMS_UNIT"
      echo "  ERROR: strip did not take, restored the original"; exit 1
    fi
    sed -i 's/ + PPT + CPU power/ + CPU power/' "$PERMS_UNIT"
    did "removed the ppt_* grant from $PERMS_UNIT"
    systemctl daemon-reload && did "daemon-reload"
    # Sysfs permissions persist for the life of the device node, so the unit
    # edit changes nothing until reboot. Do it live too.
    chmod g-w $PPT_GLOB 2>/dev/null && did "revoked live group-write on ppt_*"
  fi

  say ""
  say "The other four grants in that unit are KEPT -- battery threshold,"
  say "boot_sound/panel_overdrive, and cpufreq are all still used by hhd-z13."
  warn "z13ctl-plus tdp --set / --reset will now FAIL for your user. Reads"
  warn "(--get, status) still work; the files stay world-readable."
  warn "Expect one 'failed to restore tuning' line per resume in its log."
  warn "That is the audit signal, not noise -- do not silence it."
fi

# ═══════════════════════════════════════════════ §2 z13-hibernate's restore
if [ "$DO_HIB" = 1 ]; then
  head_ "§2  Retire z13-hibernate's post-resume TDP restore"

  if [ ! -e "$HIB_SCRIPT" ]; then
    skip "$HIB_SCRIPT not installed"
  elif grep -q "$RETIRED_MARK" "$HIB_SCRIPT" 2>/dev/null; then
    skip "already retired"
  elif [ "$MODE" != "--apply" ]; then
    would "replace $HIB_SCRIPT with a documented no-op"
    say   "  (it is scheduled from TWO sleep hooks; neutering the one script"
    say   "   makes both call sites no-ops without editing either hook)"
  else
    backup "$HIB_SCRIPT"
    cat > "$HIB_SCRIPT" <<EOF
#!/usr/bin/env bash
# $RETIRED_MARK: hhd-z13 v0.4.0 hands post-resume TDP back to hhd/adjustor.
#
# WHY THIS IS NOW A NO-OP
#   This script existed because nothing else re-applied TDP after a resume.
#   adjustor's own wakeup re-apply armed its debounce with time.time() -- the
#   Unix epoch -- while update() drains it against time.perf_counter(),
#   seconds since boot. 1.79e9 is never less than 8e3, so it never fired.
#
#   hhd-z13's hhd-qam-toggle-patch.py z13-tdp-presets table fixes the clock
#   (entries "wakeup-clock" and "acdc-clock"), so adjustor now re-applies TDP
#   ~4s after resume, through asus-nb-wmi, as the single writer.
#
#   Two writers racing the same PPT registers is what that release exists to
#   end. To undo: system/v040-tdp.sh --revert, or restore
#   $(basename "$HIB_SCRIPT")$BAK by hand.
set -uo pipefail
source /usr/lib/z13-hibernate/common.sh 2>/dev/null || true
command -v kmsg >/dev/null 2>&1 && \\
  kmsg "tdp-restore: retired ($RETIRED_MARK); hhd owns post-resume TDP"
# snapshot_tdp() still runs from the suspend hooks and is now unconsumed.
# Clear it so /run does not keep a snapshot nobody reads.
rm -f /run/z13-tdp-snapshot 2>/dev/null || true
exit 0
EOF
    chmod 755 "$HIB_SCRIPT"
    did "retired $HIB_SCRIPT (backup at $HIB_SCRIPT$BAK)"
  fi

  say ""
  warn "Not package-owned: 'make install' in z13-hibernate-plus restores the"
  warn "original. Re-running --apply fixes that idempotently, and"
  warn "system/v040-acceptance.sh checks it every run."
fi

# ═══════════════════════════════════════════ §3 z13ctl-plus's stored override
if [ "$DO_STATE" = 1 ]; then
  head_ "§3  Clear z13ctl-plus's stored TDP override"

  if [ ! -e "$STATE_JSON" ]; then
    skip "$STATE_JSON not present"
  elif ! grep -q '"tdp_active": *true' "$STATE_JSON"; then
    skip "tdp_active already false"
  elif [ "$MODE" != "--apply" ]; then
    would "stop the daemon, set tdp_active=false / tdp=null, restart it"
  else
    backup "$STATE_JSON"
    systemctl --user -M "$TARGET_USER@" stop z13ctl-plus.service 2>/dev/null
    python3 - "$STATE_JSON" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["tdp_active"] = False
d["tdp"] = None
json.dump(d, open(p, "w"), indent=2)
PY
    chown "$TARGET_USER:$TARGET_USER" "$STATE_JSON"
    systemctl --user -M "$TARGET_USER@" start z13ctl-plus.service 2>/dev/null
    did "cleared the stored TDP override"
  fi

  say ""
  say "Cosmetic once §1 has landed -- those writes fail anyway. This only"
  say "stops 'z13ctl-plus status' advertising a stale wattage."
else
  head_ "§3  z13ctl-plus's stored TDP override  (DO_STATE=0, skipped)"
  skip "opt-in: it edits a file the daemon owns and rewrites"
fi

# ══════════════════════════════════════════════════════════════════ verdict
head_ "Result"
if [ "$MODE" != "--apply" ]; then
  say "DRY RUN -- nothing was written. Re-run with --apply once the ordering"
  say "gate at the top of this file has passed."
elif [ "$CHANGED" = 1 ]; then
  say "Applied. Now run: system/v040-acceptance.sh"
  say "Undo at any time with: $0 --revert"
else
  say "Nothing to do -- already in the target state."
fi
