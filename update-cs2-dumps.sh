#!/usr/bin/env bash
# update-cs2-dumps.sh
# Sync curated slices of https://github.com/Swiftly-Tracker/CS2-Dumps into
# docs/counter-strike-2/CS2-Dumps/. Replaces the older GameTracking-CS2
# mirror for day-to-day SwiftlyS2 dev queries — commands, convars, entity
# schemas, protobufs and binary strings all live here in cleaner JSON.
#
# We skip install/ (extracted depot content, hundreds of MB of KV3/VPK
# assets) because it rarely helps with plugin questions. Add "install" to
# CONTENT_ITEMS below if you need it.

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# Overridable via DOCS_ROOT / CACHE_ROOT env vars (see Pterodactyl deployment).
DOCS_ROOT="${DOCS_ROOT:-$SCRIPT_DIR/docs}"
CACHE_ROOT="${CACHE_ROOT:-$SCRIPT_DIR/.cache}"
CACHE_DIR="$CACHE_ROOT/cs2-dumps"
OUT_DIR="$DOCS_ROOT/counter-strike-2/CS2-Dumps"
REPO_URL="https://github.com/Swiftly-Tracker/CS2-Dumps.git"

# Whitelist. Add "install" here if you want extracted depot content too.
CONTENT_ITEMS=(dump protobufs strings manifests README.md)

LOCK="/tmp/docs-mcp.$(basename "$0").lock"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[cs2-dumps] another instance is running; exiting" >&2
  exit 0
fi

mkdir -p "$(dirname "$CACHE_DIR")"

if [ -d "$CACHE_DIR/.git" ]; then
  echo "[cs2-dumps] fetching latest into $CACHE_DIR"
  git -C "$CACHE_DIR" fetch --depth 1 origin HEAD
  git -C "$CACHE_DIR" reset --hard FETCH_HEAD
  git -C "$CACHE_DIR" clean -fdx
else
  echo "[cs2-dumps] cloning $REPO_URL"
  git clone --depth 1 "$REPO_URL" "$CACHE_DIR"
fi

# Fresh mirror. Removes upstream-deleted files; small enough that a full
# recopy is simpler than rsync --delete gymnastics.
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

for item in "${CONTENT_ITEMS[@]}"; do
  src="$CACHE_DIR/$item"
  if [ -e "$src" ]; then
    cp -a "$src" "$OUT_DIR/"
  else
    echo "[cs2-dumps] warning: upstream missing '$item' (skipped)" >&2
  fi
done

# Smoke check: dump/ should have the canonical JSON files.
if [ ! -f "$OUT_DIR/dump/commands.json" ] || [ ! -f "$OUT_DIR/dump/convars.json" ]; then
  echo "[cs2-dumps] ERROR: dump/commands.json or dump/convars.json missing" >&2
  exit 1
fi
count=$(find "$OUT_DIR" -type f | wc -l)
echo "[cs2-dumps] OK — $count file(s) in $OUT_DIR ($(git -C "$CACHE_DIR" rev-parse --short HEAD))"
