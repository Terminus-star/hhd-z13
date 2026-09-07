#!/usr/bin/env bash
# Switch hhd-z13 between the pinned stable release and the current branch.
#
#   sudo ./z13-version.sh                 # show where you are
#   sudo ./z13-version.sh v0.3.0          # go to the pinned stable
#   sudo ./z13-version.sh latest          # go back to the branch tip
#   sudo ./z13-version.sh v0.3.0 -y       # skip the confirmation
#
# STRANDED? If a switch removed this script, nothing is lost -- it is still in
# git, on the branch you came from:
#
#   cd /home/termis/Projects/hhd-z13 && git checkout master
#
# The tag is only a checkout; every commit and tag is still there.
#
# WHY THIS EXISTS
#   v0.3.0 is the last release that did not touch TDP. Everything after it
#   takes PPT ownership away from z13ctl-plus and retires z13-hibernate's
#   resume restore, so "go back to v0.3.0" is not just a git checkout -- the
#   system changes have to come back with it or the machine is left with
#   nobody writing TDP at all.
#
#   So this does three things in the right order, and the order matters:
#     1. undo the v0.4.0 system changes WHILE the current tree still has the
#        script that knows how (v0.3.0's tree does not contain v040-tdp.sh)
#     2. move the checkout
#     3. re-apply the patcher for whatever tree is now checked out, since the
#        patch tables differ between versions
#
# The plugin's own state (/etc/hhd/z13-tdp-presets.json) is left alone. It is
# inert on v0.3.0 and is waiting intact if you come back.

set -uo pipefail

REPO=${Z13_REPO:-/home/termis/Projects/hhd-z13}
STABLE_TAG=v0.3.0
# master is what the editable install tracks and what feature branches are
# merged into, so "latest" means master, not the branch they were built on.
LATEST_REF=${Z13_LATEST:-master}
TARGET_USER=${SUDO_USER:-termis}
UNIT="hhd@${TARGET_USER}"

say()  { printf '  %s\n' "$*"; }
head_(){ printf '\n\033[1m== %s\033[0m\n' "$*"; }
did()  { printf '  \033[32m+ %s\033[0m\n' "$*"; }
warn() { printf '  \033[33m! %s\033[0m\n' "$*"; }
die()  { printf '  \033[31mERROR: %s\033[0m\n' "$*"; exit 1; }

TARGET=${1:-}
ASSUME_YES=0
[ "${2:-}" = "-y" ] && ASSUME_YES=1

# ── do not let the switch delete the switcher ─────────────────────────────
# This script lives inside the tree it checks out, so switching to a tag that
# predates it removes it -- and with it the only way back. That happened:
# a switch to v0.3.0 left a checkout with no system/z13-version.sh, no way to
# run `latest`, and no obvious clue where it had gone.
#
# Same answer the patcher already uses: run from a stable copy outside the
# repo. hhd-patches installs itself to /usr/local/lib/z13/, which is exactly
# why `--check` kept working across every checkout tonight.
STABLE=/usr/local/lib/z13/z13-version.sh
SELF=$(readlink -f "$0" 2>/dev/null || echo "$0")

if [ "$SELF" != "$STABLE" ]; then
  case "$SELF" in
    "$REPO"/*)
      # Running from inside the repo about to move. Stage a copy and hand
      # over to it, so the rest of this run cannot be pulled out from under
      # us mid-checkout.
      if [ "$(id -u)" -eq 0 ]; then
        mkdir -p "$(dirname "$STABLE")" 2>/dev/null
        if cp -f "$SELF" "$STABLE" 2>/dev/null; then
          chmod 755 "$STABLE"
          printf '  \033[32m+ staged a copy at %s\033[0m\n' "$STABLE"
          printf '  %s\n' "re-running from there so the checkout cannot remove it"
          exec "$STABLE" "$@"
        fi
      else
        printf '  \033[33m! running from inside %s\033[0m\n' "$REPO"
        printf '  %s\n' "Switching away can delete this script. Run with sudo so it"
        printf '  %s\n' "can stage a copy at $STABLE first."
      fi
      ;;
  esac
fi

# ── where are we ──────────────────────────────────────────────────────────
[ -d "$REPO/.git" ] || die "no git checkout at $REPO (set Z13_REPO)"
CUR=$(git -C "$REPO" describe --tags --always --dirty 2>/dev/null)
BRANCH=$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null)
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$REPO/pyproject.toml" 2>/dev/null)

head_ "Current"
say "checkout   : $CUR  (on $BRANCH)"
say "version    : ${VERSION:-unknown}"
if grep -q 'asus-nb-wmi/ppt_' /etc/systemd/system/z13ctl-plus-perms.service 2>/dev/null; then
  say "z13ctl-plus: CAN write PPT  (v0.3.0-style)"
else
  say "z13ctl-plus: cannot write PPT  (v0.4.0-style, hhd owns TDP)"
fi
if grep -q 'z13-v040-retired' /usr/lib/z13-hibernate/tdp-restore.sh 2>/dev/null; then
  say "resume TDP : retired, hhd re-applies it"
else
  say "resume TDP : z13-hibernate restores it"
fi

if [ -z "$TARGET" ]; then
  printf '\n%s\n' "  Pass $STABLE_TAG or latest to switch. Nothing changed."
  exit 0
fi

case "$TARGET" in
  "$STABLE_TAG"|stable) REF=$STABLE_TAG; GOING_BACK=1 ;;
  latest|"$LATEST_REF") REF=$LATEST_REF; GOING_BACK=0 ;;
  *) die "unknown target '$TARGET' (use $STABLE_TAG or latest)" ;;
esac

[ "$(id -u)" -eq 0 ] || die "needs root (it edits systemd units and restarts hhd)"

# A dirty tree would be silently stashed or block the checkout halfway
# through. Refuse up front rather than leave the machine between versions.
if [ -n "$(git -C "$REPO" status --porcelain 2>/dev/null)" ]; then
  die "$REPO has uncommitted changes — commit or stash them first"
fi
git -C "$REPO" rev-parse --verify --quiet "$REF" >/dev/null \
  || die "ref '$REF' not found in $REPO (fetch first?)"

# ── plan ──────────────────────────────────────────────────────────────────
head_ "Plan"
if [ "$GOING_BACK" = 1 ]; then
  say "1. restore z13ctl-plus's PPT access and z13-hibernate's resume restore"
  say "   (run from the CURRENT tree — $STABLE_TAG does not ship that script)"
  say "2. check out $REF"
  say "3. re-apply hhd-patches for that tree"
  say "4. restart $UNIT"
  warn "TDP goes back to being shared with z13ctl-plus. That is the point of"
  warn "$STABLE_TAG, but it is also the ambiguity v0.4.0 exists to remove."
else
  say "1. check out $REF"
  say "2. re-apply hhd-patches for that tree"
  say "3. restart $UNIT"
  say "NOT re-run automatically: system/v040-tdp.sh --apply, which retires"
  say "z13ctl-plus's PPT access. Run it yourself once you have confirmed the"
  say "resume gate still passes — see the banner at the top of that file."
fi

if [ "$ASSUME_YES" != 1 ]; then
  printf '\n  Proceed? [y/N] '
  read -r reply
  case "$reply" in y|Y|yes) ;; *) echo "  Aborted."; exit 0 ;; esac
fi

# ── 1. system changes, while the current tree can still undo them ─────────
if [ "$GOING_BACK" = 1 ]; then
  head_ "Restoring the v0.4.0 system changes"
  if [ -x "$REPO/system/v040-tdp.sh" ]; then
    "$REPO/system/v040-tdp.sh" --revert && did "reverted via system/v040-tdp.sh"
  else
    warn "system/v040-tdp.sh not in the current tree; restoring backups by hand"
    for f in /etc/systemd/system/z13ctl-plus-perms.service \
             /usr/lib/z13-hibernate/tdp-restore.sh; do
      [ -e "$f.z13-v040-orig" ] && cp -a "$f.z13-v040-orig" "$f" \
        && rm -f "$f.z13-v040-orig" && did "restored $f"
    done
    systemctl daemon-reload
    systemctl start z13ctl-plus-perms.service 2>/dev/null \
      && did "re-granted PPT access to z13ctl-plus"
  fi
fi

# ── 2. move the checkout ──────────────────────────────────────────────────
head_ "Checkout"
git -C "$REPO" checkout --quiet "$REF" || die "checkout failed"
did "now at $(git -C "$REPO" describe --tags --always) ($REF)"
say "version: $(sed -n 's/^version = "\(.*\)"/\1/p' "$REPO/pyproject.toml")"
say "the editable install points here, so this is what hhd will import"

# ── 3. patches for THIS tree ──────────────────────────────────────────────
head_ "Patches"
if [ -x "$REPO/hhd-patches/install.sh" ]; then
  # install.sh reverts before applying, so entries that only exist in the
  # other version are cleared rather than left behind as dead code.
  "$REPO/hhd-patches/install.sh" >/dev/null 2>&1 \
    && did "re-applied hhd-patches for this tree" \
    || warn "hhd-patches/install.sh reported a problem — run it manually"
  python3 /usr/local/lib/z13/hhd-qam-toggle-patch.py --check 2>/dev/null | sed 's/^/    /'
else
  warn "hhd-patches/install.sh missing in this tree"
fi

# ── 4. restart ────────────────────────────────────────────────────────────
head_ "Restart"
systemctl restart "$UNIT" && did "restarted $UNIT"
sleep 2
systemctl is-active --quiet "$UNIT" && did "$UNIT is active" \
  || warn "$UNIT did not come back — journalctl -u $UNIT -n50"

head_ "Done"
say "Switch back with: $0 $([ "$GOING_BACK" = 1 ] && echo latest || echo "$STABLE_TAG")"
