#!/usr/bin/env bash
# v0.2.0 "Stability" system remediation for the ROG Flow Z13 (GZ302EA).
#
#   sudo ./v020-stability.sh              # DRY RUN -- prints every change, writes nothing
#   sudo ./v020-stability.sh --apply      # make the changes
#   sudo ./v020-stability.sh --revert     # restore every backup this script made
#
# Every file is backed up to <file>.z13-v020-orig on first modification, and
# each section is independently skippable with the DO_* switches below. Nothing
# reboots; §4 rebuilds the initramfs and VERIFIES it, but leaves rebooting to you.
#
# WHAT THIS DOES NOT DO
#   It does not touch the RTC. An earlier draft of the plan wanted
#   `timedatectl set-local-rtc 0` (RTC in UTC). That was wrong for this machine:
#   you are deliberately on LOCAL for the BitLocker'd Windows install on
#   nvme0n1p3, and the justification -- kernel/userspace timestamps disagreeing
#   within a boot -- does not reproduce here (zero clock-jump events). The
#   retention problem it was blamed for is §1b below.

set -uo pipefail

# ---------------------------------------------------------------- switches
DO_SURROUND=1     # §1a  stop the virtual-surround-sound restart loop
DO_JOURNALD=1     # §1b  give journald a retention policy it can actually satisfy
DO_AFFINITY=1     # §1c  slow the 3-second GPU-affinity timer
DO_PERMS=1        # §3   drop the dangerous sysfs grants
DO_MKINITCPIO=1   # §4   one MODULES= line, drop the dead hook, rebuild + verify
DO_TDP=0          # §5   OFF BY DEFAULT -- see the section, the premise was wrong

TDP_VALUE=45      # §5   only used if you deliberately set DO_TDP=1
AFFINITY_SEC=30   # §1c  new timer interval (was 3s)

TARGET_USER=termis
BAK=.z13-v020-orig

# ---------------------------------------------------------------- plumbing
MODE=${1:---dry-run}
case "$MODE" in
  --apply|--revert|--dry-run) ;;
  *) echo "usage: $0 [--apply|--revert]  (default: dry run)"; exit 2 ;;
esac
[ "$(id -u)" -eq 0 ] || { echo "ERROR: needs root"; exit 1; }

CHANGED=0
say()  { printf '  %s\n' "$*"; }
head_() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
skip() { printf '  \033[2m- %s\033[0m\n' "$*"; }
did()  { printf '  \033[32m+ %s\033[0m\n' "$*"; CHANGED=1; }
would(){ printf '  \033[33m~ would %s\033[0m\n' "$*"; }

# act <description> <command...>
act() {
  local desc=$1; shift
  if [ "$MODE" = "--apply" ]; then "$@" && did "$desc"; else would "$desc"; fi
}

backup() {  # backup <file>
  [ -e "$1" ] || return 0
  [ -e "$1$BAK" ] && return 0
  [ "$MODE" = "--apply" ] || return 0
  cp -a "$1" "$1$BAK"
}

restore() { # restore <file>
  if [ -e "$1$BAK" ]; then
    cp -a "$1$BAK" "$1" && rm -f "$1$BAK" && did "restored $1"
  else
    skip "no backup for $1"
  fi
}

# Run a systemctl --user command as TARGET_USER, which needs their bus.
user_systemctl() {
  local uid; uid=$(id -u "$TARGET_USER") || return 1
  sudo -u "$TARGET_USER" \
    XDG_RUNTIME_DIR="/run/user/$uid" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
    systemctl --user "$@"
}

# ============================================================ REVERT
if [ "$MODE" = "--revert" ]; then
  head_ "Reverting everything this script changed"
  restore /etc/systemd/system/z13ctl-plus-perms.service
  restore /etc/mkinitcpio.conf
  restore /etc/hhd/state.yml
  for f in /etc/systemd/journald.conf.d/99-z13-retention.conf \
           /etc/systemd/system/z13-gpu-compute-affinity.timer.d/99-z13-interval.conf; do
    [ -e "$f" ] && rm -f "$f" && did "removed $f"
  done
  if user_systemctl is-enabled virtual-surround-sound.service >/dev/null 2>&1; then
    skip "virtual-surround-sound already enabled"
  else
    user_systemctl unmask virtual-surround-sound.service >/dev/null 2>&1 && \
      did "unmasked virtual-surround-sound (still disabled; enable it yourself if you want it back)"
  fi
  systemctl daemon-reload
  systemctl restart systemd-journald
  say ""
  say "mkinitcpio.conf may have been restored -- rerun 'mkinitcpio -P' if §4 had applied."
  exit 0
fi

[ "$MODE" = "--dry-run" ] && printf '\n\033[1mDRY RUN\033[0m -- nothing will be written. Re-run with --apply.\n'

# ============================================================ §1a surround sound
if [ "$DO_SURROUND" = 1 ]; then
  head_ "§1a  virtual-surround-sound restart loop"
  UNIT=/home/$TARGET_USER/.config/systemd/user/virtual-surround-sound.service
  EXEC=/home/$TARGET_USER/homebrew/plugins/decky-virtual-surround-sound/service.sh
  N=$(user_systemctl show virtual-surround-sound.service -p NRestarts --value 2>/dev/null || echo "?")
  say "restarts so far: $N"
  if [ -e "$EXEC" ]; then
    skip "ExecStart target still exists -- NOT touching it, the service may be wanted"
  elif [ ! -e "$UNIT" ]; then
    skip "unit file already gone"
  else
    say "ExecStart target is missing: $EXEC"
    say "the Decky plugin was removed but the unit was left enabled, and its"
    say "StartLimitInterval/StartLimitBurst sit in [Service], where systemd"
    say "ignores them -- so the rate limiter never trips and it respawns forever."
    act "stop virtual-surround-sound"    user_systemctl stop    virtual-surround-sound.service
    act "disable virtual-surround-sound" user_systemctl disable virtual-surround-sound.service
    act "mask virtual-surround-sound"    user_systemctl mask    virtual-surround-sound.service
    say "(the unit file itself is left on disk; --revert unmasks it)"
  fi
fi

# ============================================================ §1b journald
if [ "$DO_JOURNALD" = 1 ]; then
  head_ "§1b  journald retention"
  read -r SIZE AVAIL < <(df -B1 --output=size,avail / | tail -1)
  KEEPFREE=$(( SIZE * 15 / 100 ))
  say "filesystem $(numfmt --to=iec "$SIZE"), available $(numfmt --to=iec "$AVAIL")"
  say "journald's default SystemKeepFree is 15% = $(numfmt --to=iec "$KEEPFREE")"
  if [ "$AVAIL" -lt "$KEEPFREE" ]; then
    say ""
    say "AVAILABLE IS BELOW THE THRESHOLD. This is the actual retention bug:"
    say "journald deletes journals until free space reaches SystemKeepFree, and"
    say "it can never get there by deleting a 46M journal on a 90%-full disk --"
    say "so it vacuums almost everything, continuously. MaxRetentionSec and"
    say "SystemMaxUse alone would NOT have fixed this."
    say "Evidence: the whole system journal, across all boots, spans ~1.5 hours."
  else
    say "(threshold currently satisfied -- setting it explicitly anyway, since"
    say " the margin here is thin and it silently vacuums when crossed)"
  fi
  D=/etc/systemd/journald.conf.d
  F=$D/99-z13-retention.conf
  if [ -e "$F" ]; then
    skip "$F already present"
  elif [ "$MODE" = "--apply" ]; then
    mkdir -p "$D"
    cat > "$F" <<'EOF'
# z13 v0.2.0: give journald limits it can actually satisfy.
#
# SystemKeepFree is the important one. Its default is 15% of the filesystem;
# on a 313G disk that is 47G, and with ~34G free journald vacuums continuously
# trying to reach a threshold it cannot reach by deleting journal files. That
# is why journal history collapsed to ~1.5 hours despite the journal itself
# being only ~46M. Set it to something absolute and small.
[Journal]
SystemKeepFree=2G
SystemMaxUse=2G
MaxRetentionSec=1month
EOF
    did "wrote $F"
    systemctl restart systemd-journald && did "restarted systemd-journald"
  else
    would "write $F with SystemKeepFree=2G, SystemMaxUse=2G, MaxRetentionSec=1month"
  fi
fi

# ============================================================ §1c gpu affinity timer
if [ "$DO_AFFINITY" = 1 ]; then
  head_ "§1c  z13-gpu-compute-affinity timer"
  if ! systemctl list-unit-files z13-gpu-compute-affinity.timer >/dev/null 2>&1; then
    skip "timer not installed"
  else
    say "currently OnUnitActiveSec=3s -> ~28,800 runs/day, 3 journal lines each"
    say "(~86,000 lines/day; it was 540 of the last 567 system-journal entries)"
    D=/etc/systemd/system/z13-gpu-compute-affinity.timer.d
    F=$D/99-z13-interval.conf
    if [ -e "$F" ]; then
      skip "$F already present"
    elif [ "$MODE" = "--apply" ]; then
      mkdir -p "$D"
      cat > "$F" <<EOF
# z13 v0.2.0: 3s was pointlessly aggressive for a process-affinity sweep and
# dominated the system journal. Drop-in so the packaged unit stays untouched.
[Timer]
OnUnitActiveSec=${AFFINITY_SEC}s
AccuracySec=5s
EOF
      did "wrote $F (interval ${AFFINITY_SEC}s)"
      systemctl daemon-reload && systemctl restart z13-gpu-compute-affinity.timer \
        && did "reloaded and restarted the timer"
    else
      would "write $F setting OnUnitActiveSec=${AFFINITY_SEC}s"
    fi
  fi
fi

# ============================================================ §3 sysfs grants
if [ "$DO_PERMS" = 1 ]; then
  head_ "§3  z13ctl-plus-perms.service grants"
  U=/etc/systemd/system/z13ctl-plus-perms.service
  if [ ! -e "$U" ]; then
    skip "unit not present"
  else
    say "Two grants are removed, and the rest are KEPT because ~/.local/bin/z13cpu"
    say "writes them as your user:"
    say "  keep    scaling_min_freq, scaling_governor, energy_performance_preference, boost"
    say "  REMOVE  scaling_max_freq   -- the one that races an amd_pstate teardown"
    say "                                and re-bakes the nominal ceiling"
    say "  REMOVE  ryzen_smu mailbox  -- raw SMU command interface, group-writable;"
    say "                                z13cpu never touches it (0 references)"
    if ! grep -q "scaling_max_freq\|ryzen_smu_drv" "$U"; then
      skip "already stripped"
    elif [ "$MODE" = "--apply" ]; then
      backup "$U"
      # drop scaling_max_freq from the cpufreq glob list, keep the rest
      sed -i 's#/sys/devices/system/cpu/cpufreq/policy\*/scaling_max_freq ##' "$U"
      # drop the whole ryzen_smu ExecStart line
      sed -i '\#ryzen_smu_drv#d' "$U"
      # keep the description honest
      sed -i 's#^\(Description=.*\) + ryzen_smu)#\1)#' "$U"
      if grep -q "scaling_max_freq\|ryzen_smu_drv" "$U"; then
        echo "  ERROR: strip did not take, restoring"; cp -a "$U$BAK" "$U"; exit 1
      fi
      did "stripped scaling_max_freq and the ryzen_smu grants"
      systemctl daemon-reload && did "daemon-reload"
      say "NOTE: existing permissions persist until reboot. To apply now:"
      say "  chmod g-w /sys/devices/system/cpu/cpufreq/policy*/scaling_max_freq"
      say "  chmod g-w /sys/kernel/ryzen_smu_drv/{smu_args,mp1_smu_cmd,rsmu_cmd}"
    else
      would "strip scaling_max_freq and the ryzen_smu ExecStart from $U"
    fi
  fi
fi

# ============================================================ §4 mkinitcpio
if [ "$DO_MKINITCPIO" = 1 ]; then
  head_ "§4  mkinitcpio: duplicate MODULES= and a dead hook"
  M=/etc/mkinitcpio.conf
  NMOD=$(grep -cE '^MODULES=' "$M")
  say "MODULES= lines found: $NMOD"
  grep -nE '^MODULES=' "$M" | sed 's/^/    /'
  if [ "$NMOD" -gt 1 ]; then
    say ""
    say "MODULES= is a shell array assignment, so the second line REPLACES the"
    say "first -- amdgpu is silently discarded. It is masked today only because"
    say "the 'kms' hook pulls amdgpu in independently."
    say "pinctrl_tigerlake is the Intel Tiger Lake PCH GPIO driver: loaded, usage"
    say "count 0, nothing uses it. The AMD equivalent is CONFIG_PINCTRL_AMD=y,"
    say "built into the kernel, so it never needed a MODULES= entry."
  fi
  NEEDS_HOOK_FIX=0
  grep -qE '^HOOKS=.*hib-resume-prep' "$M" && NEEDS_HOOK_FIX=1
  if [ "$NEEDS_HOOK_FIX" = 1 ]; then
    say ""
    say "hib-resume-prep: build() only calls add_runscript, and HOOKS uses"
    say "'systemd', whose initrd does not execute legacy run_hook scripts."
    say "It has never run. The hook files stay on disk; only the HOOKS entry goes."
  fi
  if [ "$NMOD" -le 1 ] && [ "$NEEDS_HOOK_FIX" = 0 ]; then
    skip "already clean"
  elif [ "$MODE" = "--apply" ]; then
    backup "$M"
    # collapse every MODULES= line into one, preserving order, dropping the Intel driver
    MODS=$(grep -hE '^MODULES=' "$M" | sed -E 's/^MODULES=\(([^)]*)\)/\1/' | tr ' ' '\n' \
           | grep -v '^$' | grep -v '^pinctrl_tigerlake$' | awk '!seen[$0]++' | paste -sd' ')
    say "merged MODULES=($MODS)"
    awk -v mods="$MODS" '
      /^MODULES=/ { if (!done) { print "MODULES=(" mods ")"; done=1 } next }
      { print }
    ' "$M" > "$M.z13-tmp" && mv "$M.z13-tmp" "$M"
    sed -i -E 's/^(HOOKS=.*) hib-resume-prep/\1/' "$M"
    did "rewrote MODULES= and dropped hib-resume-prep from HOOKS"
    grep -nE '^MODULES=|^HOOKS=' "$M" | sed 's/^/    /'

    say ""
    say "rebuilding initramfs..."
    if mkinitcpio -P; then
      did "mkinitcpio -P succeeded"
      # verify the images exist, are non-trivial, and contain amdgpu
      OK=1
      for img in /boot/initramfs-*.img; do
        [ -e "$img" ] || continue
        SZ=$(stat -c %s "$img")
        [ "$SZ" -gt 1000000 ] || { echo "  ERROR: $img is only $SZ bytes"; OK=0; }
      done
      if lsinitcpio -a /boot/initramfs-linux-cachyos.img 2>/dev/null | grep -qi amdgpu; then
        did "amdgpu present in the rebuilt image"
      else
        say "WARNING: could not confirm amdgpu in the image -- check manually with"
        say "  lsinitcpio -l /boot/initramfs-linux-cachyos.img | grep amdgpu"
      fi
      [ "$OK" = 1 ] || { echo "  ERROR: image verification failed. Restoring."; \
                         cp -a "$M$BAK" "$M"; mkinitcpio -P; exit 1; }
      say ""
      say "DO NOT REBOOT until you are happy with the above."
      say "To undo: sudo $0 --revert && sudo mkinitcpio -P"
    else
      echo "  ERROR: mkinitcpio failed. Restoring $M and rebuilding."
      cp -a "$M$BAK" "$M"; mkinitcpio -P; exit 1
    fi
  else
    would "merge MODULES= (dropping pinctrl_tigerlake), drop hib-resume-prep, then mkinitcpio -P"
  fi
fi

# ============================================================ §5 custom.tdp
if [ "$DO_TDP" = 1 ]; then
  head_ "§5  hhd custom.tdp"
  S=/etc/hhd/state.yml
  CUR=$(grep -A3 '^      custom:' "$S" 2>/dev/null | grep -m1 '^        tdp:' | awk '{print $2}')
  say "stored custom.tdp: ${CUR:-<none>}"
  say ""
  say "THIS SECTION IS OFF BY DEFAULT. The plan called for raising custom.tdp"
  say "to 45 because 9 W looked like an accident below a 28 W floor. That was"
  say "wrong -- 28 comes only from asus-armoury's own min_value, which is that"
  say "interface's advertised minimum, not a hardware limit. Every other source"
  say "disagrees:"
  say "  adjustor Z1_DATA (this exact model)   min_tdp: 5"
  say "  adjustor's own TDP slider              min: 0, max: 30"
  say "  asus-nb-wmi (z13ctl-plus + adjustor)   exposes no bounds files at all"
  say "  z13ctl-plus                            documents only upper caps"
  say "  measured                               9 W, 15 W and 20 W all worked"
  say ""
  say "So 9 is a deliberate, valid, mid-slider value. Leave it alone unless you"
  say "actually want a different number, in which case set DO_TDP=1 above."
  if [ -z "$CUR" ]; then
    skip "no custom.tdp key found"
  elif [ "$CUR" = "$TDP_VALUE" ]; then
    skip "already $TDP_VALUE"
  elif [ "$MODE" = "--apply" ]; then
    backup "$S"
    # hhd owns this file; stop it so our write is not clobbered on its next flush
    WAS_ACTIVE=$(systemctl is-active "hhd@$TARGET_USER" 2>/dev/null || true)
    [ "$WAS_ACTIVE" = active ] && systemctl stop "hhd@$TARGET_USER" && say "stopped hhd@$TARGET_USER"
    # Range-addressed sed, not awk: state.yml has no trailing newline and awk
    # would silently add one. hhd owns this file; the edit must be byte-exact
    # apart from the value itself.
    sed -i "/^      custom:\$/,+2 s/^        tdp: .*/        tdp: $TDP_VALUE/" "$S"
    NEW=$(grep -A3 '^      custom:' "$S" | grep -m1 '^        tdp:' | awk '{print $2}')
    if [ "$NEW" = "$TDP_VALUE" ]; then
      did "custom.tdp $CUR -> $TDP_VALUE"
    else
      echo "  ERROR: edit did not take (got '$NEW'), restoring"; cp -a "$S$BAK" "$S"
    fi
    [ "$WAS_ACTIVE" = active ] && systemctl start "hhd@$TARGET_USER" && say "started hhd@$TARGET_USER"
  else
    would "set custom.tdp $CUR -> $TDP_VALUE"
  fi
fi

# ============================================================ done
head_ "Done"
if [ "$MODE" = "--dry-run" ]; then
  say "This was a dry run. Re-run with --apply to make the changes."
elif [ "$CHANGED" = 1 ]; then
  say "Changes applied. Backups are alongside each file as *$BAK."
  say "Undo everything: sudo $0 --revert"
else
  say "Nothing to do -- everything was already in the desired state."
fi
