#!/usr/bin/env bash
# Run every test. No pytest, no root, no hardware writes.
#
# The suites are plain scripts rather than pytest cases on purpose: they are
# meant to be runnable on the machine that is misbehaving, over ssh, without
# installing anything. Each exits non-zero on failure.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

rc=0
for f in tests/test_*.py; do
  printf '\n\033[1m== %s\033[0m\n' "$f"
  if python3 "$f"; then :; else rc=1; fi
done

printf '\n\033[1m%s\033[0m\n' "$([ $rc = 0 ] && echo 'ALL SUITES PASS' || echo 'SOME SUITES FAILED')"
exit $rc
