# Cutover: Mac worker from the VPS queue to fi-hub

**Status: soak running since 2026-11-01. Acceptance scenarios 1 to 5 passed. The
second-account refusal gate (scenario 6) is NOT done: the fi-hub session will run it with
option B and report; until it passes, nothing on the VPS may be touched.** No secrets are in this
file, only their names.

| | |
|---|---|
| Date | 2026-11-01 |
| Branch | `feat/fi-hub-backend`, pushed; CI green on its head. `main` needs a pull request (see Publishing). |
| Rollback tag | `pre-fi-hub` on `main` at `3b43969`, pushed |
| Old installed plist | `~/Library/LaunchAgents/com.hermes.worker.plist.pre-fi-hub` (mode 600) |
| Queue | `https://fi-hub.REPLACE_ME.workers.dev/worker` |
| Soak start | **2026-11-01**, by the owner's decision at the end of Milestone 4. The VPS stays up and untouched for the soak. |

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

## Acceptance (Milestone 4, driven from the phone through Fi; checked against the Hub and the log)

| # | Scenario | Result | Evidence |
|---|---|---|---|
| 1 | A YouTube link becomes a job, runs on the Mac, the note is searchable | **Pass** (timed on a second, fresh note: see "Confirmed afterwards") | `transcribe_video` `9602811b…` done in 11 s, pushed `inserted 1`; `summarize_transcript` `648ba59f…` done in 17 s, pushed `updated 1`; one file, one summary block; `search_notes` returned it at score 1 |
| 2 | Mac asleep, then woken: the job completes, no duplicate | **Pass, by a different path than planned** | Job `459ce7db…` was leased in a DarkWake and the Mac slept 7 s into it. Lease lost (HTTP 409), note still pushed, job re-leased and completed; `done`, `attempts 2`, one note. See the DarkWake finding. |
| 3 | A failing job ends `failed`, shows its error, is not retried | **Pass, with `example.com`, not an X link** | `2be15378…` `summarize_url`: `failed`, `attempts 1 of 3`, error "only 156 characters of text extracted…", log `exit 2 after 1s (permanent)`, no retry, no stray note. The X-link rejection (same exit 2, before any network call) is covered by the skill's own tests, not exercised through the Hub. |
| 4 | Kill the worker mid-job: lease expires, job requeues after the retry delay, one note, summary intact | **Pass** | `f2b1750a…` `summarize_transcript` (`force`, `detailed`), Mac on AC power with no Sleep/DarkWake entry in the window. Leased 17:30:23, worker SIGKILLed 17:30:35 (12 s in), `queued` after the lease expired at 17:32:23 with "will retry in about 1m", re-leased 17:34:27, done 17:35:54 (87 s), pushed 17:35:58. `done`, `attempts 2`. One of 10 files; one summary block; the transcript sha256 and the frontmatter sha256 are identical to the copy taken before; the note was byte-identical after the killed attempt (nothing written); no `.tmp`, no orphan process. |
| 5 | `queue_status` shows `worker_last_seen_s` as a number | **Pass** | `2`, `5`, `0`, `15` seen across the session; `mac_status: awake`; `skills_published: 3` |
| 6 | **Second-account refusal (hard gate)** | **NOT DONE** | The owner chose **option B** (temporarily wrong `ALLOWED_GITHUB_ID` on `fi-hub-auth`, then restored and verified as `12057985`), to be run by the fi-hub session. Result: `__ pending __`. Record it here when the owner reports it. |

### Not proven by the acceptance run
- **A job queued through a long, full sleep** (as opposed to a DarkWake). Not observed.
- A transient failure retried twice and then `dead`, and a job or parameter not in the
  manifest being refused at enqueue (HANDOFF 11.12 item 3): not run in this pass.

### Confirmed afterwards
- **Deduplication:** the owner sent the scenario 2 request twice; the Hub holds one job for it.
- **Time until a new note is searchable** (a brand-new note, transcribe only, Mac awake on AC; local
  time, 2026-11-01). Job `522ca7cd…` was created 18:03:35, leased 18:03:38 (+3 s), done 18:04:00
  ("done in 22s"), pushed 18:04:03 (`inserted 1`), and **found by `search_notes` at 18:05:06**
  (`cached: false`, the new note returned with its exact passage): **63 s after the push and 91 s
  after the job was created.** One file, 11 notes on the Hub and in the vault. The first search
  was already at +63 s and found it, so the true figure is at most that; fi-hub documents 70 to
  90 s. A first measurement made earlier, against a note already in the index, proved nothing and
  is not counted. The search quota used for the whole acceptance was 21 of 1000.

### Findings from the acceptance run
- **`launchd` kills the skill with the worker.** Killing only the worker pid left no orphan
  skill or `caffeinate` process; the whole job died. A crash therefore leaves the vault note
  untouched (the skill writes atomically and had not reached the write) and nothing running.
- **The Hub calls a dead worker's job "running on the Mac right now"** until its lease
  expires (here, 2 minutes). Cosmetic, for the fi-hub session.
- **A permanent failure's push can fail.** `queue_status` showed `notifications.failed: 1`: the
  "Job failed: summarize_url" push never reached ntfy (`fetch threw TimeoutError`). Known and
  parked (HANDOFF section 9); the job was unaffected.
- **For the fi-hub session:** count lease expiries separately from real failures, so a job that
  slept through its lease is not reported as a failure or sent to `dead` for it.

## Publishing

The repository is public, so before anything was pushed every line added by the 12 commits was
scanned (secret and private-detail patterns, each with a positive control that had to match its
own synthetic sample, plus a negative control). Result: no secret. Findings and decisions:

- **The account's `workers.dev` subdomain was in the plist template, `.env.example` and this
  file and was not public anywhere.** The unpublished commits were rewritten (a local rewrite,
  not a force push) to use `REPLACE_ME`. The installed plist keeps the real URL; fill in the
  template's placeholder when rebuilding from it. A local backup branch holds the pre-rewrite
  history and was not pushed.
- The git author email was already on published commits; unchanged.
- The numeric GitHub id of the account is public data and was left in this file.
- The one "secret" pattern hit was a fake `NTFY_TOPIC` literal in a test.

Pushed: `feat/fi-hub-backend` and the `pre-fi-hub` tag (no force). CI in this repository runs
only on pushes to `main`, pull requests and manual dispatch, so it was run by dispatch on the
branch head: pyflakes, pyright and pytest passed. **`main` is protected by a ruleset with no
bypass: a direct push was declined, and changes must arrive through a pull request.** `main`
was not changed. Open the pull request and merge it (the repository's history uses merge
commits).

## What passed (Milestone 3)

- `worker.py --check` passed under the installed plist's environment.
- `worker.py --probe` passed: `GET /worker/healthz` returned 200 with the token and 401 without,
  TLS verified through `SSL_CERT_FILE`, and the three local manifests validate.
- Log after the second reload: `published manifest (3 skills)`; startup push of the 8 notes
  `inserted 0, updated 0, unchanged 4` twice (two batches of four); `reconciled (deleted 0,
  pending 0, failed 0)`.
- Suite at the time: 291 tests pass, pyright 0 errors, pyflakes clean.

The first successful poll is not logged (a successful poll logs nothing). It is evidenced by
the absence of errors after the reload and is confirmed by `queue_status`
(`worker_last_seen_s` a number), which is checked from a Claude session.

## What was not performed

- **The second-account refusal check.** A hard gate: it must pass before anyone touches the VPS.
- Anything on the VPS: Hermes, `queue.service`, Telegram or API keys, OpenClaw. Not touched,
  not decommissioned, not rotated. That is a separate stage with a separate go, after the soak.
- The items listed under "Not proven by the acceptance run".
- `feat/fi-hub-backend` is not merged into `main` (needs a pull request).
- The token file `~/.config/fi-hub/worker-token-new` still exists (mode 600), at the owner's
  request. Delete it when it is no longer needed.

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
