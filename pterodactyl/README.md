# Pterodactyl deployment

Package the CS2 Docs MCP server as a [Pterodactyl](https://pterodactyl.io/)
egg so it runs alongside your game servers, gets managed by wings, and
refreshes its docs automatically every night.

Contents:

- `Dockerfile.pterodactyl` — image that stages the app in `/app/` (persistent
  volume at `/home/container` won't hide it) and runs as UID 988.
- `entrypoint.sh` — creates `/home/container/{docs,.cache}` on first boot,
  then execs `python3 /app/doc-server.py`.
- `cs2-docs-mcp.egg.json` — the importable egg (`PTDL_v2`).

**Docs refresh automatically on every container start.** The server kicks
off `/app/update-all.sh` in a background thread as it comes up, so the MCP
port is available immediately and the index hot-reloads as each source
(swiftlys2 / source2 / CS2-Dumps) finishes downloading. Set up a
daily **Restart** schedule (section 5 below) and you're done — no console
commands required.

Two **console commands** are still available for ad-hoc use:

| Command       | What it does                                                |
| ------------- | ----------------------------------------------------------- |
| `update-docs` | Runs `/app/update-all.sh` then hot-reloads the index.       |
| `reindex`     | Rebuilds the index only (use after uploading manual docs).  |

Reindex swaps the in-memory metadata dict atomically and drops the file-content
LRU cache, so new / renamed / deleted markdown files become visible to MCP
clients within seconds. **No container restart is required.**

---

## 1. Build & push the image

The egg's `docker_images` entry points at `ghcr.io/shmitzas/cs2-docs-mcp:latest`.
Change that key/value in `cs2-docs-mcp.egg.json` if you're pushing somewhere else.

### CI (recommended)

[`.github/workflows/build-image.yml`](../.github/workflows/build-image.yml)
already does the multi-arch build + push to `ghcr.io/<owner>/cs2-docs-mcp` on
every push to `main` that touches the image (and on published releases).
Nothing to run locally — just merge to `main` and the new tag lands.

Two prerequisites the first time you set the repo up:

- **Enable GHCR write** — Repository → Settings → Actions → General → Workflow
  permissions → "Read and write permissions" (so `GITHUB_TOKEN` can push).
- **Package visibility** — after the first push, https://github.com/users/&lt;owner&gt;/packages/container/cs2-docs-mcp/settings
  → set to **Public** so Pterodactyl nodes can pull without credentials.

### Manual build (only if you can't use CI)

From the repo root:

```bash
# One-off local build (works for a same-arch node)
docker build -f pterodactyl/Dockerfile.pterodactyl -t ghcr.io/shmitzas/cs2-docs-mcp:latest .
docker push ghcr.io/shmitzas/cs2-docs-mcp:latest

# Or multi-arch via buildx (recommended — covers x86 + ARM nodes)
docker buildx build \
    -f pterodactyl/Dockerfile.pterodactyl \
    --platform linux/amd64,linux/arm64 \
    -t ghcr.io/shmitzas/cs2-docs-mcp:latest \
    --push .
```

---

## 2. Import the egg

1. **Admin → Nests** → pick or create a nest (e.g. "MCP servers").
2. **Import Egg** → upload `pterodactyl/cs2-docs-mcp.egg.json`.
3. The `CS2 Docs MCP Server` egg now appears under that nest.

---

## 3. Create the server

1. **Admin → Servers → Create New Server**.
2. Pick the nest and the `CS2 Docs MCP Server` egg.
3. Allocation: assign one port (the port number becomes the `PORT` variable's
   default; if you allocate `:9000` you must also set the `Port` variable to
   `9000` on the server's Startup tab so `EXPOSE`/`--host 0.0.0.0` match).
4. Resources: 256 MB RAM / 0.5 CPU is enough for a small deployment; bump to
   1 GB / 1 CPU if you enable the optional `install/` mirror in
   `update-cs2-dumps.sh` (hundreds of extra files).
5. Disk: allow **at least 1 GB** — the default CS2-Dumps mirror
   (`dump/`, `protobufs/`, `strings/`, `manifests/`) plus the Source 2
   wiki and SwiftlyS2 docs together sit under ~200 MB. Bump to 3 GB if
   you also enable the optional `install/` mirror.
6. **Create Server** and let the install script finish.

---

## 4. First boot

Hit **Start**. On the console you'll see something like:

```
[entrypoint] /home/container/docs is empty — first-boot fetch has been queued
[entrypoint] and may take a minute or two (CS2-Dumps + Source 2 wiki
[entrypoint] shallow clones). The MCP server is available now;
[entrypoint] the index will hot-reload as each source finishes.
[startup] auto-refresh: docs update queued in the background
Documentation MCP server ready on port 8080
[console] update-docs: running /app/update-all.sh
===== update-swiftlys2.sh =====
...
[console] update-docs: reindex complete (N documents)
```

Pterodactyl marks the server **Running** as soon as it sees
`Documentation MCP server ready on port` — which happens within a few
seconds, before the update finishes. Clients can already connect; they'll
see an empty index at first, and each source's docs appear as its updater
completes and the atomic reindex fires.

First-boot total time is typically 1–3 minutes (dominated by the
CS2-Dumps clone). Subsequent restarts do delta fetches and finish
in under a minute.

Any markdown you drop into `/home/container/docs/<category>/*.md` via the
File Manager becomes browsable after a `reindex` console command (or the
next restart).

To turn the auto-refresh off (e.g. you want the server to boot without
touching the network), set the server variable `UPDATE_ON_STARTUP` to `0`
on the Startup tab. The `update-docs` and `reindex` console commands still
work manually.

---

## 5. Daily refresh — Pterodactyl Schedule

Since every restart auto-refreshes the docs, a scheduled **restart** is
all you need to keep them fresh. Pterodactyl **schedules** are per-server
config, not baked into the egg export — set this up once per server:

1. Server → **Schedules** tab → **Create Schedule**.
2. Fill in:
   - **Name:** `Daily docs refresh`
   - **Cron:** minute `0`, hour `4`, day `*`, month `*`, weekday `*`
   - **Only when server is online:** ✔
3. Save, then **Create Task** on the new schedule:
   - **Action:** `Send power action`
   - **Payload:** `Restart`
   - **Time offset:** `0`
4. That's it. At 04:00 every day, wings restarts the container; the
   entrypoint boots, the background updater fetches deltas, and the
   index reindexes inline. Clients experience a couple of seconds of
   downtime while the container respawns.

Don't want to restart at all? Skip the schedule and just refresh manually
by sending the `update-docs` console command from the panel.

---

## 6. Point MCP clients at it

The server speaks SSE on `http://<node-ip>:<PORT>/sse`. Add to your client
config exactly as the top-level [README](../README.md#configuration)
describes — the URL is the only thing that changes.

---

## 7. Troubleshooting

| Symptom                                     | Fix                                                                                                                                 |
| ------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| Server stuck on `Starting`                  | The `done` string isn't printing. Check console for a Python traceback — usually a missing dependency (rebuild the image).          |
| `update-docs` says "script not found"       | Verify `/app/update-all.sh` exists in the image. If you built from a fork that renamed it, set the `UPDATE_SCRIPT` env var.         |
| `update-docs` runs but no new docs show up  | Check the console for errors from the individual `update-*.sh` scripts. `git clone` often fails on tight disk quotas — bump disk.   |
| Console shows `[console] unknown command`   | Only `update-docs` and `reindex` are recognised. Typos won't do anything harmful, just get logged.                                  |
| MCP clients see stale content after upload  | The `_load_content` LRU cache holds file bodies. Send `reindex` — it clears the cache as well as rebuilding the metadata index.     |
| Stop button times out to SIGKILL            | Only happens if you replaced `entrypoint.sh` with one that doesn't `exec` the python process. Keep the `exec` on the last line.     |
| Two update runs at the same time            | The stdin handler serialises `update-docs` with a lock — a second invocation logs "another update is already running; ignoring".    |
