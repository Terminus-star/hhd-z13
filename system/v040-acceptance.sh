#!/usr/bin/env bash
# v0.4.0 acceptance tests for hhd-z13's TDP presets on the ROG Flow Z13.
#
#   ./v040-acceptance.sh          # run the automated checks
#   ./v040-acceptance.sh --manual # also print the interactive checklist
#
# READ-ONLY. Writes nothing, restarts nothing, changes no hardware state.
# Safe mid-game. Root is not required.
#
# THE ONE RULE THIS FILE ENFORCES ABOVE ALL OTHERS
#   Read back through the interface you WROTE. adjustor writes asus-nb-wmi.
#   asus-armoury reports the firmware profile's own numbers (60/75/86 here),
#   not the live override, so verifying there shows a failure that did not
#   happen. It is printed below as INFORMATIONAL and is never a pass/fail.

set -uo pipefail

PASS=0; FAIL=0; WARN=0; SKIP=0
p() { printf '  \033[32mPASS\033[0m  %-42s %s\n' "$1" "${2-}"; PASS=$((PASS+1)); }
f() { printf '  \033[31mFAIL\033[0m  %-42s %s\n' "$1" "${2-}"; FAIL=$((FAIL+1)); }
w() { printf '  \033[33mWARN\033[0m  %-42s %s\n' "$1" "${2-}"; WARN=$((WARN+1)); }
s() { printf '  \033[2mSKIP\033[0m  %-42s %s\n' "$1" "${2-}"; SKIP=$((SKIP+1)); }
i_() { printf '  \033[2minfo\033[0m  %-42s %s\n' "$1" "${2-}"; }
hd() { printf '\n\033[1m%s\033[0m\n' "$1"; }

USER_NAME=${SUDO_USER:-$(id -un)}
UNIT="hhd@${USER_NAME}"
NBWMI=/sys/devices/platform/asus-nb-wmi
ARMOURY=/sys/class/firmware-attributes/asus-armoury/attributes
PATCHER=/usr/local/lib/z13/hhd-qam-toggle-patch.py
ASUS_DRV=$(ls -d /usr/lib/python3*/site-packages/adjustor/drivers/asus/__init__.py 2>/dev/null | tail -1)
HIB_SCRIPT=/usr/lib/z13-hibernate/tdp-restore.sh
PERMS_UNIT=/etc/systemd/system/z13ctl-plus-perms.service

SINCE=$(systemctl show "$UNIT" -p ActiveEnterTimestamp --value 2>/dev/null)
[ -z "$SINCE" ] && SINCE="today"
jrn() { journalctl -u "$UNIT" --since "$SINCE" --no-pager 2>/dev/null; }

echo "hhd-z13 v0.4.0 acceptance — $(date '+%F %T')"
echo "  window: since hhd started ($SINCE)"

# ───────────────────────────────────────────────────── 1. the patch is live
hd "1. adjustor carries the z13-tdp-presets patch"

if [ ! -e "$PATCHER" ]; then
  f "patcher marks" "not installed at $PATCHER"
else
  N=$(python3 "$PATCHER" --check 2>/dev/null | grep -c PATCHED)
  T=$(python3 "$PATCHER" --check 2>/dev/null | grep -c .)
  # Counted against the rows the patcher REPORTS, never a hardcoded total --
  # hardcoding is what made a green machine fail at 7/6 when a mark was added.
  if [ "$T" != 0 ] && [ "$N" = "$T" ]; then p "patcher marks" "$N/$T applied"
  else f "patcher marks" "$N/$T applied — run hhd-patches/install.sh"; fi
fi

if [ -z "$ASUS_DRV" ]; then
  f "adjustor asus driver" "not found"
else
  grep -qF 'time.perf_counter() + SLEEP_DELAY' "$ASUS_DRV" \
    && p "resume re-apply clock" "perf_counter (was the Unix epoch: never fired)" \
    || f "resume re-apply clock" "still time.time() — resume will not re-apply TDP"
  grep -qF 'time.perf_counter() + APPLY_DELAY' "$ASUS_DRV" \
    && p "AC/DC re-apply clock" "perf_counter" \
    || f "AC/DC re-apply clock" "still time.time()"
  grep -qF '_z13_a = _z13_json.load(_z13_fd)' "$ASUS_DRV" \
    && p "manual PL2/PL3 override" "present" \
    || f "manual PL2/PL3 override" "a preset set to Manual will have PL2/PL3 derived"

  # Presets became stored value sets applied through adjustor's own Custom
  # mode, so the entries that widened its mode dispatch were dropped. The
  # patcher only adds what the current table names and never removes what an
  # older one applied, so a superseded widening can linger. Harmless -- with
  # no preset modes it can never match -- but it is dead code between this
  # machine and upstream.
  grep -qF 'if mode == "custom" or (mode and mode not in' "$ASUS_DRV" \
    && w "superseded mode widening" "still installed; hhd-patches/install.sh --revert then re-apply" \
    || p "superseded mode widening" "absent"
fi

# ─────────────────────────────────────────── 2/3. the tree, and the live PPT
hd "2. Presets are in adjustor's dropdown, and applied"

# Read hhd's own API rather than re-deriving defaults from the YAML: it hands
# back the MERGED tree with defaults already resolved, which is the same thing
# the QAM shows. Both files it needs are world-readable.
EVAL=$(python3 - <<'PY' 2>/dev/null
import json, urllib.request
try:
    tok = open("/etc/hhd/.token").read().strip()
    def get(ep):
        r = urllib.request.Request(f"http://localhost:5335/api/v1/{ep}",
                                   headers={"Authorization": f"Bearer {tok}"})
        return json.load(urllib.request.urlopen(r, timeout=5))
    st, se = get("state"), get("settings")
except Exception as e:
    print(f"ERR api unreachable: {e}")
    raise SystemExit

try:
    asus = se["tdp"]["asus"]["children"]
    tv = asus["tdp_v2"]
except Exception:
    print("ERR no tdp.asus.tdp_v2 in the settings tree")
    raise SystemExit

def stval(path, default=None):
    d = st
    for k in path.split("."):
        if not isinstance(d, dict) or k not in d:
            return default
        d = d[k]
    return d

# adjustor's own dropdown must be UNTOUCHED. Presets are stored value sets
# applied through its Custom mode, not extra modes -- that is what makes them
# survive KDE's power-profile change on unplug.
print("STOCKMODES " + ",".join(sorted(tv.get("modes", {}))))
# Presets live INSIDE Custom, not beside it -- one menu, not two. A
# z13_presets container at the container level would mean the superseded
# layout is back.
cust = tv.get("modes", {}).get("custom", {}).get("children", {})
print("PRESETCTL " + str("z13_preset" in cust))
# The power-change policy is its own area, not nested in Custom -- it has to
# be reachable from any mode, including the one KDE flips you to on unplug.
print("POWERCTL " + str("z13_power" in asus))
print("POWERNEST " + str("z13_power" in cust))
# There must be exactly ONE TDP control in this menu. Two means the preset
# grew its own slider back alongside adjustor's.
print("TDPCTLS " + ",".join(sorted(k for k in cust if "tdp" in k.lower())))
# ...and none hiding inside a slot. The duplicate that prompted this layout
# was a per-slot slider, not a sibling, so counting siblings alone would miss
# its return.
_sl = (cust.get("z13_preset", {}) or {}).get("modes", {}) or {}
print("SLOTKIDS " + str(sum(len((v or {}).get("children", {}) or {}) for v in _sl.values())))
# Power Limits is gated on TDP Boost: with boost off all three limits are the
# same number, so the control is dropped from the tree rather than shown inert.
print("BOOST " + str(stval("tdp.asus.tdp_v2.custom.boost")))
print("PPTCTL " + str("z13_ppt" in cust))
print("APPLIEDTYPE " + str(cust.get("z13_applied", {}).get("type")))

slots = sorted(cust.get("z13_preset", {}).get("modes", {}))
print("SLOTS " + ",".join(slots))

mode = stval("tdp.asus.tdp_v2.mode") or tv.get("default")
print("MODE " + str(mode))

# Only Custom is driven by a preset. On any other mode adjustor owns the
# limits outright and there is nothing of ours to assert.
if mode == "custom":
    print("ACTIVE " + str(stval("tdp.asus.tdp_v2.custom.z13_preset.mode") or "p1"))
    # adjustor's own slider is the single editor, so it IS the target.
    print("WATTS " + str(stval("tdp.asus.tdp_v2.custom.tdp")))
    # The PL2/PL3 targets live in the plugin's store, which is also what the
    # patch reads -- deliberately not in the settings tree, because anything
    # declared there renders as an editable second source of truth.
    try:
        with open("/etc/hhd/z13-tdp-presets.json") as f:
            st_j = json.load(f)
        ap = st_j.get("applied") or {}
        print("TARGETS %s %s" % (ap.get("sppt", 0), ap.get("fppt", 0)))
        print("NSTORED " + str(len(st_j.get("presets") or [])))
    except Exception as e:
        print("TARGETS unreadable " + str(e))
print("APPLIED " + str(stval("tdp.asus.tdp_v2.custom.z13_applied") or ""))
print("STATUS " + str(stval("tdp.asus.tdp_v2.custom.z13_status") or ""))
PY
)

if echo "$EVAL" | grep -q '^ERR'; then
  s "settings tree" "$(echo "$EVAL" | sed -n 's/^ERR //p')"
else
  # adjustor's dropdown must be exactly its own four modes. A preset mode
  # appearing here would mean the superseded design is back, and with it the
  # bug where unplugging landed on Silent.
  [ "$(echo "$EVAL" | sed -n 's/^STOCKMODES //p')" = "balanced,custom,performance,quiet" ] \
    && p "adjustor's dropdown untouched" "balanced,custom,performance,quiet" \
    || f "adjustor's dropdown untouched" "$(echo "$EVAL" | sed -n 's/^STOCKMODES //p')"

  [ "$(echo "$EVAL" | sed -n 's/^PRESETCTL //p')" = "True" ] \
    && p "presets inside Custom" "present" \
    || f "presets inside Custom" "z13_preset missing from custom"

  # Exactly one TDP control. Two means a preset grew its own slider back
  # beside adjustor's, which is the duplication this layout exists to remove.
  TDPCTLS=$(echo "$EVAL" | sed -n 's/^TDPCTLS //p')
  SLOTKIDS=$(echo "$EVAL" | sed -n 's/^SLOTKIDS //p')
  if [ "$TDPCTLS" = "tdp" ] && [ "${SLOTKIDS:-0}" = "0" ]; then
    p "one TDP control in the menu" "tdp; slots carry no controls"
  else
    f "one TDP control in the menu" "siblings=$TDPCTLS, controls inside slots=${SLOTKIDS:-?}"
  fi

  [ "$(echo "$EVAL" | sed -n 's/^POWERCTL //p')" = "True" ] \
    && p "power-change policy" "in its own area" \
    || f "power-change policy" "z13_power missing"
  [ "$(echo "$EVAL" | sed -n 's/^POWERNEST //p')" = "False" ] \
    && p "policy not buried in Custom" "sibling of the TDP Mode selector" \
    || f "policy not buried in Custom" "still nested inside custom"

  # Boost off means PL1 = PL2 = PL3, so Power Limits is dropped from the tree
  # rather than left sitting there doing nothing.
  BOOST=$(echo "$EVAL" | sed -n 's/^BOOST //p')
  PPTCTL=$(echo "$EVAL" | sed -n 's/^PPTCTL //p')
  case "$BOOST:$PPTCTL" in
    True:True|False:False) p "Power Limits gated on TDP Boost" "boost=$BOOST, shown=$PPTCTL" ;;
    *) f "Power Limits gated on TDP Boost" "boost=$BOOST but shown=$PPTCTL" ;;
  esac

  [ "$(echo "$EVAL" | sed -n 's/^APPLIEDTYPE //p')" = "display" ] \
    && p "Applied is read-only telemetry" "display" \
    || f "Applied is read-only telemetry" "$(echo "$EVAL" | sed -n 's/^APPLIEDTYPE //p')"

  SLOTS=$(echo "$EVAL" | sed -n 's/^SLOTS //p')
  NSTORED=$(echo "$EVAL" | sed -n 's/^NSTORED //p')
  NSLOT=$(echo "$SLOTS" | tr ',' '\n' | grep -c .)
  # The slot list is generated from the store, so a disagreement means a
  # rebuild was missed and Add/Remove has silently stopped working.
  if [ -z "$NSTORED" ]; then s "slots match the store" "not on Custom"
  elif [ "$NSLOT" = "$NSTORED" ]; then p "slots match the store" "$NSLOT ($SLOTS)"
  else f "slots match the store" "$NSLOT slots vs $NSTORED stored"; fi

  BANNER=$(echo "$EVAL" | sed -n 's/^STATUS //p')
  [ -z "$BANNER" ] && p "preset status banner" "clear" \
                   || f "preset status banner" "$BANNER"

  MODE=$(echo "$EVAL" | sed -n 's/^MODE //p')
  WATTS=$(echo "$EVAL" | sed -n 's/^WATTS //p')
  hd "3. Live power limit — read through the interface we wrote"
  i_ "active TDP mode" "$MODE"
  [ -n "$(echo "$EVAL" | sed -n 's/^ACTIVE //p')" ] \
    && i_ "active preset" "$(echo "$EVAL" | sed -n 's/^ACTIVE //p')"
  SPL=$(cat "$NBWMI/ppt_pl1_spl" 2>/dev/null)
  if [ -z "${WATTS:-}" ]; then
    # Expected whenever KDE has flipped the label: the mode reads Silent while
    # the preset's wattage is still in force, because adjustor's quiet branch
    # writes a fan curve and never touches PPT. Not a failure.
    s "PL1 matches the preset" "not on Custom — adjustor owns the limit here"
    i_ "asus-nb-wmi ppt_pl1_spl" "${SPL:-unreadable} W"
  elif [ "$SPL" = "$WATTS" ]; then
    p "PL1 matches the preset" "ppt_pl1_spl=$SPL W, expected $WATTS W"
  else
    f "PL1 matches the preset" "ppt_pl1_spl=${SPL:-?} W, expected $WATTS W"
  fi
  TGT=$(echo "$EVAL" | sed -n 's/^TARGETS //p')
  [ -n "$TGT" ] && i_ "PL2/PL3 targets in the store" "$TGT  (0 0 = derived by adjustor)"
  APP=$(echo "$EVAL" | sed -n 's/^APPLIED //p')
  [ -n "$APP" ] && i_ "Applied (as the QAM shows it)" "$APP"

  # Informational only. With boost on, adjustor derives these as
  # min(max(spl,35), spl*1.4) -- a no-op at >=35 W, a partial boost below --
  # so asserting on them invites false failures.
  i_ "asus-nb-wmi ppt_pl2_sppt" "$(cat "$NBWMI/ppt_pl2_sppt" 2>/dev/null) W  (informational)"
  i_ "asus-nb-wmi ppt_fppt" "$(cat "$NBWMI/ppt_fppt" 2>/dev/null) W  (informational)"
  i_ "asus-armoury ppt_pl1_spl" \
     "$(cat "$ARMOURY/ppt_pl1_spl/current_value" 2>/dev/null) W  (informational — reports the"
  printf '        %s\n' "firmware profile, NOT the live override. Never ground truth.)"
fi

# ──────────────────────────────────────────────────── 4. one writer, not four
hd "4. Single writer"

if [ ! -e "$PERMS_UNIT" ]; then
  s "z13ctl-plus PPT grant" "perms unit not installed"
elif grep -q 'asus-nb-wmi/ppt_' "$PERMS_UNIT"; then
  f "z13ctl-plus PPT grant" "still granted — run system/v040-tdp.sh --apply"
else
  p "z13ctl-plus PPT grant" "revoked from the unit"
fi

NW=$(find $NBWMI/ppt_* -perm -g=w 2>/dev/null | wc -l)
# WARN not FAIL: sysfs modes live as long as the device node, so a machine
# that has not rebooted since --apply is correct-but-stale, not broken.
[ "$NW" = 0 ] && p "live group-write on ppt_*" "none" \
              || w "live group-write on ppt_*" "$NW file(s) still g+w (clears on reboot)"

if [ ! -e "$HIB_SCRIPT" ]; then
  s "z13-hibernate TDP restore" "not installed"
elif grep -q 'z13-v040-retired' "$HIB_SCRIPT"; then
  p "z13-hibernate TDP restore" "retired"
else
  # Most likely cause: `make install` in z13-hibernate-plus put the original
  # back. Re-running v040-tdp.sh --apply fixes it idempotently.
  f "z13-hibernate TDP restore" "still active — re-run system/v040-tdp.sh --apply"
fi

# Sanity, not a gate. An oscillating mirror would show as tens per minute;
# normal use is a handful.
NA=$(jrn | grep -c "Setting tdp value 'steady'")
[ "$NA" -lt 30 ] && p "TDP applies since hhd started" "$NA (an oscillation would be 10s per minute)" \
                 || w "TDP applies since hhd started" "$NA — suspiciously many, check for a mirror loop"

# ────────────────────────────────────────────────────────── 5. what KDE sees
hd "5. KDE power profile follows the wattage"

AP=$(busctl get-property net.hadess.PowerProfiles /net/hadess/PowerProfiles \
       net.hadess.PowerProfiles ActiveProfile 2>/dev/null | awk '{gsub(/"/,"");print $2}')
SPL=${SPL:-$(cat "$NBWMI/ppt_pl1_spl" 2>/dev/null)}
if [ -z "$AP" ] || [ -z "$SPL" ]; then
  s "PPD active profile" "could not read one of the two"
else
  # adjustor's own derivation, from Z1_DATA balanced_min=42 / performance_min=50.
  if   [ "$SPL" -lt 42 ]; then EXP=power-saver
  elif [ "$SPL" -lt 50 ]; then EXP=balanced
  else EXP=performance; fi
  [ "$AP" = "$EXP" ] && p "PPD active profile" "$AP (from ${SPL}W)" \
                     || w "PPD active profile" "$AP, expected $EXP from ${SPL}W"
fi

# ───────────────────────────────────────────────────────────── 6. the plugin
hd "6. Plugin health"

OUT=$(cd /tmp && python3 -c "
import hhd_z13
print(hhd_z13.__file__)
print(','.join(type(p).__name__ for p in hhd_z13.autodetect([])))
" 2>&1)
if echo "$OUT" | grep -q "Z13TdpPlugin"; then
  p "plugin autodetect" "Z13TdpPlugin present"
  # Printed because the editable install points at the main checkout, not a
  # worktree -- chasing a change that never loaded has cost real time here.
  echo "$OUT" | head -1 | sed 's|^|        loaded from: |'
else
  f "plugin autodetect" "$(echo "$OUT" | tail -1)"
fi

N=$(jrn | grep -c "not carrying the z13-tdp-presets patch")
[ "$N" = 0 ] && p "patch warnings" "0" || f "patch warnings" "$N"

N=$(jrn | grep -c "TDP preset prepare failed\|TDP preset update failed")
[ "$N" = 0 ] && p "preset plugin errors" "0" || f "preset plugin errors" "$N"

N=$(jrn | grep -ciE "Traceback|Unhandled exception")
[ "$N" = 0 ] && p "plugin exceptions" "0" || f "plugin exceptions" "$N tracebacks"

# ───────────────────────────────────────────────────────────────── verdict
hd "Result"
printf '  %d passed, %d failed, %d warnings, %d skipped\n' "$PASS" "$FAIL" "$WARN" "$SKIP"
[ "$FAIL" = 0 ] && printf '  \033[32mv0.4.0 automated checks pass.\033[0m\n' \
               || printf '  \033[31m%d check(s) failed.\033[0m\n' "$FAIL"

# ─────────────────────────────────────────────────────────────── manual set
if [ "${1-}" = "--manual" ]; then
hd "Manual checklist"
cat <<'EOF'
  A. Pick each of the four presets in the QAM.
     -> ppt_pl1_spl follows within ~4s; KDE's battery applet moves tier.

  B. Drag a preset's TDP slider.
     -> the value lands, and `journalctl -fu hhd@$USER | grep "Setting tdp"`
        does NOT produce a continuous stream. A stream means the mirror is
        fighting adjustor's clamp.

  C. Enable "Preset on Power Change", unplug, replug.
     -> the mode follows. Then pick a DIFFERENT mode by hand and wait:
        it must STAY until the next plug/unplug. Edge-triggered, not
        continuous, is the whole contract.

  D. Suspend, wait 30s, resume.   *** THE GATE FOR system/v040-tdp.sh ***
     -> "Waking up from sleep, resetting TDP after 4 seconds." followed
        WITHIN ~10s by "Setting tdp value 'steady' to <N>", and PL1
        unchanged from before the suspend.
     -> If the wake line appears with no write after it, do NOT retire
        z13-hibernate's tdp-restore.sh.

  E. Hibernate and resume.
     -> same as D, and the retired tdp-restore.sh logs
        "retired (z13-v040-retired)" without writing anything.

  F. Switch to stock Custom, then back to a preset.
     -> Custom's slider now shows the last preset's watts. Expected: the
        mirror writes into custom.tdp by design. If 9 W mattered there,
        make it a preset slot instead.

  G. sudo pacman -S hhd
     -> the pacman hook re-applies the patch; `--check` returns to N/N.
        Restart hhd and re-run A.

  H. sudo system/v040-tdp.sh --revert, then reboot, then re-run this script.
     -> section 4 flips to FAIL/WARN cleanly and nothing else regresses.
EOF
fi

exit $([ "$FAIL" = 0 ] && echo 0 || echo 1)
