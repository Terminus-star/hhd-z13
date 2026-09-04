#!/usr/bin/env bash
# v0.2.0 acceptance tests for hhd-z13 on the ROG Flow Z13 (GZ302EA).
#
#   ./v020-acceptance.sh          # run the automated checks
#   ./v020-acceptance.sh --manual # also print the interactive checklist
#
# READ-ONLY. Writes nothing, restarts nothing, changes no hardware state.
# Safe to run at any time, including mid-game. Root is not required; the two
# checks that would need it degrade to SKIP rather than failing.
#
# Every check states the value it expects, so a FAIL tells you what "good"
# looks like without going back to the docs.

set -uo pipefail

PASS=0; FAIL=0; WARN=0; SKIP=0
p() { printf '  \033[32mPASS\033[0m  %-42s %s\n' "$1" "${2-}"; PASS=$((PASS+1)); }
f() { printf '  \033[31mFAIL\033[0m  %-42s %s\n' "$1" "${2-}"; FAIL=$((FAIL+1)); }
w() { printf '  \033[33mWARN\033[0m  %-42s %s\n' "$1" "${2-}"; WARN=$((WARN+1)); }
s() { printf '  \033[2mSKIP\033[0m  %-42s %s\n' "$1" "${2-}"; SKIP=$((SKIP+1)); }
hd() { printf '\n\033[1m%s\033[0m\n' "$1"; }

USER_NAME=${SUDO_USER:-$(id -un)}
UNIT="hhd@${USER_NAME}"

# Window = since hhd last started. Counting from boot would blame this release
# for noise produced before the fixes were applied.
SINCE=$(systemctl show "$UNIT" -p ActiveEnterTimestamp --value 2>/dev/null)
[ -z "$SINCE" ] && SINCE="today"
jrn() { journalctl -u "$UNIT" --since "$SINCE" --no-pager 2>/dev/null; }

echo "hhd-z13 v0.2.0 acceptance — $(date '+%F %T')"
echo "  window: since hhd started ($SINCE)"

# ─────────────────────────────────────────────── 1. the conflict is closed
hd "1. Writer conflicts (the core of v0.2.0)"

N=$(python3 /usr/local/lib/z13/hhd-qam-toggle-patch.py --check 2>/dev/null | grep -c PATCHED)
T=$(python3 /usr/local/lib/z13/hhd-qam-toggle-patch.py --check 2>/dev/null | grep -c .)
if [ "$N" = 6 ]; then p "patcher marks" "6/6 PATCHED"
elif [ "$T" = 0 ]; then f "patcher marks" "patcher not installed at /usr/local/lib/z13/"
else f "patcher marks" "$N/6 PATCHED — expected 6; run hhd-patches/install.sh"; fi

N=$(jrn | grep -c "Setting CPU frequency scaling")
[ "$N" = 0 ] && p "adjustor cpufreq writes" "0 (expected 0)" \
             || f "adjustor cpufreq writes" "$N — expected 0; adjustor is still writing cpufreq"

N=$(jrn | grep -c "Failed to set CPU boost")
[ "$N" = 0 ] && p "adjustor boost errors" "0 (expected 0)" \
             || w "adjustor boost errors" "$N — supports_boost may have been restored"

# The 3000 MHz bug: a policy's max is fixed when it is created. If boost is on,
# scaling_max_freq must equal cpuinfo_max_freq; anything lower means a policy
# was built while boost was off and the nominal ceiling got baked in.
MAXF=$(cat /sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq 2>/dev/null)
HWMAX=$(cat /sys/devices/system/cpu/cpufreq/policy0/cpuinfo_max_freq 2>/dev/null)
BOOST=$(cat /sys/devices/system/cpu/cpufreq/boost 2>/dev/null)
# Tolerance, not equality: cpufreq rounds the policy max down to the driver's
# frequency step, so scaling_max legitimately sits a few hundred kHz below
# cpuinfo_max (here 5187000 vs 5187500). The bug being tested for is the
# NOMINAL ceiling getting baked in -- 3000000 against a 5187500 hw max, a 42%
# gap. Exact equality flags the harmless rounding and prints "5187 < 5187".
if [ -z "$MAXF" ]; then s "cpufreq ceiling" "cpufreq not readable"
elif [ "$BOOST" != 1 ]; then
  w "cpufreq ceiling" "$((MAXF/1000)) MHz, boost off — re-check with boost on"
elif [ "$(( MAXF * 100 / HWMAX ))" -ge 95 ]; then
  p "cpufreq ceiling" "$((MAXF/1000)) of $((HWMAX/1000)) MHz hw max (boost on)"
else
  f "cpufreq ceiling" "$((MAXF/1000)) MHz vs hw $((HWMAX/1000)) MHz — nominal ceiling baked in"
fi

# ─────────────────────────────────────────────── 2. privilege surface
hd "2. Privilege surface"

PERMS=/etc/systemd/system/z13ctl-plus-perms.service
if [ ! -e "$PERMS" ]; then s "perms unit" "not installed"
else
  grep -q scaling_max_freq "$PERMS" \
    && f "scaling_max_freq grant" "still granted — expected removed" \
    || p "scaling_max_freq grant" "removed (expected)"
  grep -q ryzen_smu_drv "$PERMS" \
    && f "ryzen_smu grant" "still granted — expected removed" \
    || p "ryzen_smu grant" "removed (expected)"
  M=0
  for k in scaling_min_freq scaling_governor energy_performance_preference "cpufreq/boost"; do
    grep -q "$k" "$PERMS" || { f "z13cpu grant: $k" "MISSING — z13cpu will break"; M=1; }
  done
  [ "$M" = 0 ] && p "z13cpu grants intact" "min_freq, governor, EPP, boost"
fi

# live permissions (these persist until reboot even after the unit is fixed)
BAD=$(find /sys/devices/system/cpu/cpufreq/policy*/scaling_max_freq -perm -g=w 2>/dev/null | wc -l)
[ "$BAD" = 0 ] && p "live scaling_max_freq perms" "no group-write (expected)" \
               || w "live scaling_max_freq perms" "$BAD group-writable — clears on reboot"

if [ -e /sys/kernel/ryzen_smu_drv/smu_args ]; then
  find /sys/kernel/ryzen_smu_drv/ -perm -g=w 2>/dev/null | grep -q . \
    && w "live ryzen_smu perms" "group-writable — clears on reboot" \
    || p "live ryzen_smu perms" "no group-write (expected)"
else s "live ryzen_smu perms" "module not loaded"; fi

# ─────────────────────────────────────────────── 3. boot safety
hd "3. Boot safety"

N=$(grep -c '^MODULES=' /etc/mkinitcpio.conf 2>/dev/null)
[ "$N" = 1 ] && p "MODULES= lines" "1 (expected 1; 2 silently discards the first)" \
             || f "MODULES= lines" "$N — expected 1"
grep -qE '^MODULES=.*amdgpu' /etc/mkinitcpio.conf 2>/dev/null \
  && p "amdgpu in MODULES" "present (expected)" \
  || f "amdgpu in MODULES" "absent — expected present"
grep -qE '^MODULES=.*pinctrl_tigerlake' /etc/mkinitcpio.conf 2>/dev/null \
  && w "pinctrl_tigerlake" "still listed — Intel driver, unused here" \
  || p "pinctrl_tigerlake" "removed (expected)"
grep -qE '^HOOKS=.*hib-resume-prep' /etc/mkinitcpio.conf 2>/dev/null \
  && f "hib-resume-prep hook" "still in HOOKS — expected removed (it never runs)" \
  || p "hib-resume-prep hook" "removed (expected)"
for h in systemd sd-encrypt resume filesystems; do
  grep -qE "^HOOKS=.*\b$h\b" /etc/mkinitcpio.conf 2>/dev/null \
    || f "HOOKS contains $h" "MISSING — boot may fail"
done
grep -qE "^HOOKS=.*\bsd-encrypt\b" /etc/mkinitcpio.conf 2>/dev/null \
  && p "critical HOOKS intact" "systemd, sd-encrypt, resume, filesystems"

# images must be newer than the config that produced them
MID=$(cat /etc/machine-id 2>/dev/null)
if [ -r "/boot/$MID" ]; then
  STALE=$(find "/boot/$MID" -name 'initramfs*' -not -newer /etc/mkinitcpio.conf 2>/dev/null | wc -l)
  N=$(find "/boot/$MID" -name 'initramfs*' 2>/dev/null | wc -l)
  if [ "$N" = 0 ]; then w "initramfs images" "none found under /boot/$MID"
  elif [ "$STALE" = 0 ]; then p "initramfs freshness" "$N image(s), all newer than mkinitcpio.conf"
  else f "initramfs freshness" "$STALE of $N older than mkinitcpio.conf — run sudo limine-update"; fi
else s "initramfs freshness" "/boot not readable (run with sudo to check)"; fi

# ─────────────────────────────────────────────── 4. journal health
hd "4. Journal health (forensics for the next problem)"

N=$(jrn | grep -c "Detected locale")
[ "$N" = 0 ] && p "kscreen locale spam" "0 (expected 0)" \
             || f "kscreen locale spam" "$N — expected 0; display.py locale fix missing"

if [ -e /etc/systemd/journald.conf.d/99-z13-retention.conf ]; then
  grep -q "SystemKeepFree" /etc/systemd/journald.conf.d/99-z13-retention.conf \
    && p "journald SystemKeepFree" "set (the real retention fix)" \
    || f "journald SystemKeepFree" "drop-in exists but lacks SystemKeepFree"
else f "journald retention drop-in" "missing — journald will vacuum aggressively"; fi

# how much history actually survives — the point of the retention fix
# Grace period: history cannot exceed the age of the retention fix itself, so
# do not call it a failure until the drop-in has had 24h to accumulate.
OLDEST=$(journalctl --system -o short-unix --no-pager 2>/dev/null | head -1 | cut -d. -f1)
DROPIN=/etc/systemd/journald.conf.d/99-z13-retention.conf
FIXAGE=99
[ -e "$DROPIN" ] && FIXAGE=$(( ( $(date +%s) - $(stat -c %Y "$DROPIN") ) / 3600 ))
if [ -n "$OLDEST" ]; then
  H=$(( ( $(date +%s) - OLDEST ) / 3600 ))
  if [ "$H" -ge 24 ]; then
    p "system journal history" "${H}h retained (want >=24h)"
  elif [ "$FIXAGE" -lt 24 ]; then
    w "system journal history" "${H}h, retention fix is only ${FIXAGE}h old — recheck after 24h"
  else
    f "system journal history" "${H}h — expected >=24h; journald is still vacuuming"
  fi
fi

N=$(systemctl show z13-gpu-compute-affinity.timer -p TimersMonotonic --value 2>/dev/null \
    | grep -o 'OnUnitActiveUSec=' | wc -l)
if [ "$N" = 1 ]; then p "affinity timer intervals" "1 (expected 1; 2 means the drop-in appended)"
elif [ "$N" = 0 ]; then s "affinity timer" "not installed"
else f "affinity timer intervals" "$N — drop-in appended instead of replacing"; fi

systemctl --user is-enabled virtual-surround-sound.service >/dev/null 2>&1 \
  && f "virtual-surround-sound" "still enabled — respawn loop active" \
  || p "virtual-surround-sound" "not enabled (expected)"

# ─────────────────────────────────────────────── 5. the plugin itself
hd "5. Plugin"

systemctl is-active --quiet "$UNIT" && p "hhd service" "active" || f "hhd service" "not active"

# from a neutral cwd: running inside the repo shadows the install
OUT=$(cd /tmp && python3 -c "
import hhd_z13
print(hhd_z13.__file__)
print(','.join(type(p).__name__ for p in hhd_z13.autodetect([])))
" 2>&1)
if echo "$OUT" | grep -q "Z13CpuPlugin" && echo "$OUT" | grep -q "Z13DisplayPlugin"; then
  p "plugin autodetect" "Z13CpuPlugin + Z13DisplayPlugin"
  echo "$OUT" | head -1 | sed 's|^|        loaded from: |'
else f "plugin autodetect" "$(echo "$OUT" | tail -1)"; fi

N=$(jrn | grep -ciE "Traceback|Unhandled exception")
[ "$N" = 0 ] && p "plugin exceptions" "0 (expected 0)" \
             || f "plugin exceptions" "$N tracebacks in the journal"

# ─────────────────────────────────────────────── 6. the ship gate
hd "6. Ship gate — overlay stability"

GEST=$(jrn | grep -c "Executing overlay command")
DIED=$(jrn | grep -c "Overlay thread died")
EXT=$(jrn | grep -c "steam may have restarted")
echo "        overlay opens: $GEST   thread deaths: $DIED   steam-caused: $EXT"
echo "        (a death per close is BY DESIGN on the desktop path: hhd-z13's"
echo "         toggle terminates hhd-ui, and upstream logs that as a fault)"
UNEXP=$(( DIED - EXT ))
[ "$UNEXP" -lt 0 ] && UNEXP=0
if [ "$DIED" = 0 ]; then p "unexpected overlay deaths" "0"
elif [ "$UNEXP" -le "$GEST" ]; then
  p "unexpected overlay deaths" "$UNEXP <= $GEST opens — all accounted for"
else
  w "unexpected overlay deaths" "$UNEXP deaths vs $GEST opens — investigate"
fi

# ─────────────────────────────────────────────── 7. no side effects
hd "7. No side effects from testing"

N=$(pgrep -c gamescope 2>/dev/null || echo 0)
[ "$N" -le 2 ] && p "stray gamescope processes" "$N (0 on desktop, 2 in game mode)" \
               || w "stray gamescope processes" "$N — check for leaked sessions"

UNALLOC=$(btrfs filesystem usage / 2>/dev/null | grep -oP 'Device unallocated:\s+\K[0-9.]+[A-Za-z]+' | head -1)
case "$UNALLOC" in
  *GiB) p "btrfs unallocated" "$UNALLOC (want GiB, not MiB)" ;;
  "")   s "btrfs unallocated" "not readable without root" ;;
  *)    f "btrfs unallocated" "$UNALLOC — metadata cannot grow; run btrfs balance" ;;
esac

TDP=$(grep -A3 '^      custom:' /etc/hhd/state.yml 2>/dev/null | grep -m1 '^        tdp:' | awk '{print $2}')
[ -n "$TDP" ] && p "custom.tdp preserved" "$TDP (floor is 5 W, not 28)" \
              || s "custom.tdp" "key not found"

# ─────────────────────────────────────────────── verdict
hd "Verdict"
printf '  %d passed, %d failed, %d warnings, %d skipped\n' "$PASS" "$FAIL" "$WARN" "$SKIP"
if [ "$FAIL" -gt 0 ]; then
  printf '  \033[31mNOT READY\033[0m — fix the FAILs above.\n'
elif [ "$WARN" -gt 0 ]; then
  printf '  \033[33mREADY, with caveats\033[0m — review the WARNs; most clear on reboot or with time.\n'
else
  printf '  \033[32mREADY\033[0m — automated checks all pass. Finish the manual list below.\n'
fi

[ "${1-}" = "--manual" ] || { echo; echo "  Run with --manual for the interactive checklist."; exit $((FAIL>0)); }

cat <<'EOF'

────────────────────────────────────────────────────────────────────────
MANUAL CHECKS — these need a human; nothing above can prove them
────────────────────────────────────────────────────────────────────────

A. QAM, desktop            Press the Armoury button.
   expect                  QAM opens. Press again: it closes. Each cycle
                           relaunches hhd-ui (~1-1.5s) — that is by design
                           on the desktop path, not a fault.

B. QAM, game mode          In gamescope, press Ctrl+4 (Ctrl+3 = expanded).
   expect                  QAM opens; Ctrl+4 again closes it. Reopening is
                           INSTANT here — gamescope uses the graceful close
                           and the process stays resident.
   note                    Xbox chords will NOT work. Steam holds an
                           exclusive grab on the pad. Expected, not a bug.

C. Refresh rate            hhd QAM -> Refresh Rate: Manual, 60, then 180.
   expect                  kscreen-doctor -j reflects each change within
                           ~1s. No flicker, no black frame.

D. AC/battery              Set Refresh Rate and CPU Boost to Auto. Unplug.
   expect                  Drops to 60 Hz and boost off. Replug: back to
                           180 Hz and boost on. Both within a few seconds.

E. Boost -> ceiling        Toggle CPU Boost off, then on.
   expect                  cat .../policy0/scaling_max_freq tracks it, and
                           returns to the full hw max when back on. If it
                           sticks at 3000000, the adjustor patch regressed.

F. pstate switch           hhd QAM -> change amd_pstate mode.
   expect                  Completes without a hang. Machine stays
                           responsive. This is the path that could hard-lock
                           when two writers raced; it must be boring now.

G. Suspend/resume          Suspend, wait 30s, resume. Then check A-E again.
   expect                  hhd still active, no new "Setting CPU frequency
                           scaling" lines, refresh/boost still correct.
                           Re-run this script: still all PASS.

H. Reboot                  Only after section 3 passes. Reboot once.
   expect                  Boots normally. This is the first boot on the
                           merged MODULES= line and without hib-resume-prep.
                           If it fails: pick an older limine entry, then
                           sudo ./v020-stability.sh --revert && sudo limine-update

I. A normal day            Use the machine as usual for a day.
   expect                  Re-run this script. Section 6 shows no unexpected
                           overlay deaths, section 4 shows >=24h of journal
                           history. THAT is the ship gate.

Tag when H and I both hold:
  git checkout master && git merge v0.2.0-stability
  git tag -a v0.2.0 -m "Stability" && git push --tags gitea && git push --tags github
EOF
exit $((FAIL>0))
