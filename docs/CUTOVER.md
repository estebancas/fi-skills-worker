# Cutover: Mac worker from the VPS queue to fi-hub

**Status: in progress (end of Milestone 3).** Milestone 4, the end-to-end acceptance and the
second-account refusal gate, has not been done. This file is completed then. No secrets are in
it, only their names.

| | |
|---|---|
| Date | 2026-11-01 |
| Branch | `feat/fi-hub-backend` (not merged, not pushed) |
| Rollback tag | `pre-fi-hub` on `main` at `3b43969` (local only) |
| Old installed plist | `~/Library/LaunchAgents/com.hermes.worker.plist.pre-fi-hub` (mode 600) |
| Queue | `https://fi-hub.REPLACE_ME.workers.dev/worker` |
| Soak start | not started (starts when Milestone 4 passes) |

## What was done

1. **Baseline** (216 tests, pyright and pyflakes clean) measured before any change. The local
   edits to `hermes-integration.md` were preserved on `wip/hermes-integration-edits`.
2. **Transport and polling.** HTTPS with `urllib` and `SSL_CERT_FILE`; TLS failures, refused
   tokens (401) and lost leases (409) are told apart from outages; an empty poll sleeps
   `POLL_INTERVAL` (15 s); `--probe` added; ntfy removed from the Mac.
3. **Notes push replaces the catalog push.** Full markdown, `kind` from the raw frontmatter
   keys, batches of at most 200,000 bytes and 10 notes, `limits` honoured, 413 skipped, reconcile
   only after every batch landed, manifest retried until it lands and republished hourly.
   Paths are confined to the vault before they are read.
4. **Install.** The old installed plist was backed up, then rebuilt from the template with the
   rotated token written in-process (never in an argument, output or repo file). `NTFY_TOPIC` is
   removed. The old worker was stopped and the new one started.
5. **A live defect was found and fixed** (below).

## Discoveries

### Cloudflare rejects Python's default User-Agent (error 1010)
At the first reload TLS worked but every call failed with `HTTP 403 ... Error 1010: Access
denied`. That is Cloudflare's edge refusing the client's browser signature **before the request
reaches fi-hub**; the token was never checked. With no token, `GET /worker/healthz` gave:

| User-Agent | Result |
|---|---|
| `curl/8.7.1` | 401 from fi-hub (`bad or missing token`) |
| `fi-skills-worker/1.0` | 401 from fi-hub |
| `Python-urllib/3.14` | **403 `error code: 1010`** from the edge |

Fix: every request sends `User-Agent: fi-skills-worker/1.0`. A 403 whose body names a
Cloudflare 1xxx error is classified `EdgeBlocked` and reported as "blocked at the Cloudflare
edge (User-Agent?)" in the loop and in `--probe`. The test suite could not have caught it, since
its stub is not Cloudflare; the stub now simulates the edge, and dropping the header fails two
tests. **If a future change makes the Mac fail with a 403 while `curl` works, look here first.**
Stage 2's checks used curl, which is why they passed.

### Reloading takes a moment
`launchctl bootout` returns before the old process exits; the worker finishes its current
request after SIGTERM. A `bootstrap` issued at once fails with `Input/output error` (exit 5).
Wait until the old pid is gone, then bootstrap. Nothing is wrong when that happens.

### A sleeping Mac still polls: DarkWake windows (found in scenario 2)
The Mac does not stay silent while asleep. About every 15 minutes it wakes for roughly 45 s
(`pmset -g log`: Wi-Fi TCP-keepalive offload, `E_TKO_TCP_DATA` / `E_TKO_UNEXPECT_TCP_FLAG`;
plus scheduled `rtc/Maintenance` from `mDNSResponder` and `dasd`). Settings: `powernap 1`,
`tcpkeepalive 1`, `womp 0`. Which connection causes the keepalive wakes is not known.

What happened: job `459ce7db…` was enqueued at 14:42:31 inside a DarkWake, leased at
14:42:37, and the Mac went back to sleep at 14:42:44, **seven seconds into the job**, with
`caffeinate`'s assertions (including `PreventSystemSleep`) in place. `-s` is honoured only on
AC power; this was battery. The job's process was frozen for 15 min and could not heartbeat.
At the next DarkWake it finished ("done in 913s"), `complete` answered 409 because the lease
had expired and the job was requeued, the worker logged `lease lost, result not recorded`,
**still pushed the note**, re-leased the job (attempt 2), re-ran it in 9 s and completed it.
Outcome: one note, one job `done` with `attempts 2`, no duplicate, no leftover process.

Consequence: a job that needs more awake time than a window offers, queued while the Mac
sleeps, can lose its lease on every attempt and go `dead` after 3, with the work done up to 3
times. Short jobs fit. This is a property of the platform, not of the worker; delivery
being at-least-once is what makes it survivable. Recorded in `CLAUDE.md`.

Open items: the hub should count lease expiries separately from failures (to be queued for
the fi-hub session). No power setting was changed and `caffeinate -u` is untested.

### CRLF notes are not indexed (known limit, predates this work)
`skilllib.notefmt.FRONTMATTER` requires `---\n`. A note saved with `\r\n` line endings has no
recognised frontmatter and is skipped by the notes push. The skills only write LF, so this
matters only if a note is edited somewhere that rewrites line endings. Fixing it means changing
`skilllib`, shared by every skill, so it was left alone. Recorded in `CLAUDE.md`.

### Smaller findings
- `catalog_after` never read `note_path`, the key `summarize_url` returns, so article notes
  waited for the next full push. `notes_after` reads it.
- `transcribe_video/run.py` still contains its own ntfy code. It does nothing unless
  `NTFY_TOPIC` is set, and the installed plist no longer sets it. Skills were not touched.
- The installed plist differed from the template only in `QUEUE_URL`, `QUEUE_TOKEN`, the empty
  `NTFY_TOPIC`, and the keys the template added (`POLL_INTERVAL`, `PUBLISH_CATALOG`,
  `CATALOG_RECONCILE_S`, `FETCH_*`).

## What passed (Milestone 3)

- `worker.py --check` passed under the installed plist's environment.
- `worker.py --probe` passed: `GET /worker/healthz` returned 200 with the token and 401 without,
  TLS verified through `SSL_CERT_FILE`, and the three local manifests validate.
- Log after the second reload: `published manifest (3 skills)`; startup push of the 8 notes
  `inserted 0, updated 0, unchanged 4` twice (two batches of four); `reconciled (deleted 0,
  pending 0, failed 0)`.
- Suite: 291 tests pass, pyright 0 errors, pyflakes clean.

The first successful poll is not logged (a successful poll logs nothing). It is evidenced by
the absence of errors after the reload and is confirmed by `queue_status`
(`worker_last_seen_s` a number), which is checked from a Claude session.

## What was not performed

- Milestone 4: the end-to-end acceptance from the phone (link to job to note, asleep and woken,
  a failing job, a killed worker, `worker_last_seen_s`).
- **The second-account refusal check.** It is a hard gate before anyone touches the VPS.
- Anything on the VPS: Hermes, `queue.service`, Telegram or API keys, OpenClaw. Not decommissioned.
- The branch is not merged or pushed, and the `pre-fi-hub` tag is not pushed.

## Hermes is not served

Since the worker points at fi-hub, the old VPS queue is not polled. Hermes jobs enqueued there
stall for the whole soak. That is expected.

## Rollback

Both plists run the same `~/skills-worker/worker.py`, so the code must roll back with the plist:
the new worker against the old queue would get a 404 on `/notes`.

```
launchctl bootout gui/$UID/com.hermes.worker
# wait until the old process has exited, then:
cp -p ~/Library/LaunchAgents/com.hermes.worker.plist.pre-fi-hub ~/Library/LaunchAgents/com.hermes.worker.plist
chmod 600 ~/Library/LaunchAgents/com.hermes.worker.plist
cd ~/skills-worker && git switch --detach pre-fi-hub
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.hermes.worker.plist
.venv/bin/python3 worker.py --check
# to return to the new code afterwards:
git switch feat/fi-hub-backend
```
