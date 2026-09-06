#!/usr/bin/env bash
# update-all.sh
# Runs every docs updater. A single failure does NOT abort the others — cron
# will still see a non-zero overall exit code so the failure surfaces.

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

scripts=(
  update-swiftlys2.sh
  update-source2.sh
  update-cs2-dumps.sh
)

fail=0
for s in "${scripts[@]}"; do
  echo "===== $s ====="
  if ! "$SCRIPT_DIR/$s"; then
    echo "!!! $s FAILED (continuing)" >&2
    fail=1
  fi
  echo
done

if [ "$fail" -ne 0 ]; then
  echo "one or more updaters failed" >&2
  exit 1
fi
echo "all updaters succeeded"
