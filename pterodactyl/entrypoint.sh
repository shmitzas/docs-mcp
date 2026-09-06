#!/bin/bash
# /app/entrypoint.sh — invoked by the egg's `startup` string on every boot.
#
# Pterodactyl mounts the persistent server volume at /home/container, so
# anything the update scripts write there survives restarts. This entrypoint
# just guarantees the directories exist, then execs the MCP server — which
# kicks off the docs refresh in the background on every start (see the
# UPDATE_ON_STARTUP block in doc-server.py). A daily "Send power action:
# Restart" schedule in the panel is therefore all you need to keep docs
# fresh; no console commands required.
set -e

: "${DOCS_ROOT:=/home/container/docs}"
: "${CACHE_ROOT:=/home/container/.cache}"

mkdir -p "$DOCS_ROOT" "$CACHE_ROOT"

if [ -z "$(ls -A "$DOCS_ROOT" 2>/dev/null)" ]; then
    echo "[entrypoint] $DOCS_ROOT is empty — first-boot fetch has been queued"
    echo "[entrypoint] and may take a minute or two (CS2-Dumps + Source 2 wiki"
    echo "[entrypoint] shallow clones). The MCP server is available now;"
    echo "[entrypoint] the index will hot-reload as each source finishes."
fi

cd /home/container

# `exec` replaces the shell with python so SIGINT from Pterodactyl reaches
# the server directly (otherwise config.stop=^C would time out).
exec python3 /app/doc-server.py
