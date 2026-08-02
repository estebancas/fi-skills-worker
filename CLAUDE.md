# skills-worker

Skills that a worker on this Mac runs on behalf of the **Hermes** agent.

Each subdirectory is one skill: a `run.py` that does the work and a `skill.yaml` that
declares it to the worker's manifest.

## Why this exists

Hermes runs on a VPC so it is always reachable. The VPC is slow and memory-constrained,
and the knowledge vault plus the heavy tooling (yt-dlp, whisper, Ollama) live on this
Mac, which is not always on.

Mounting the Mac's filesystem into the VPC was tried first and abandoned: a filesystem
transport moves bytes, not processes. The script still ran on the VPC's CPU and RAM, and
per-syscall round-trips over the tunnel made it slower than before.

The working design inverts the direction — **the Mac pulls**:

```
You → Hermes (VPC, always up) → enqueue_job() → SQLite queue (VPC)
                                                      ↑ long-poll over Tailscale
                                              Mac worker (launchd, KeepAlive)
                                                      ↓
                                        runs the skill, writes the vault
                                                      ↓
                                          POST /jobs/{id}/complete
```

Consequences that shape the code here:

- The Mac being offline is not a special case. It is a job that has not been leased yet.
- Work happens on local disk. No NFS, no SSHFS, no UID mapping, no `server-filesystem`.
- Only JSON crosses the tunnel. The Mac never accepts an inbound connection, and the VPC
  holds no credentials into it.
- Hermes gets typed job types from an allowlist, not a shell.

## Skill contract

- Params arrive as a **single JSON object on stdin**.
- The result is a **JSON object on the last line of stdout**. Nothing else goes to stdout.
- Logs and progress go to **stderr**.
- Exit codes: `0` success · `1` transient, the worker retries · `2` permanent, it does not.

Env the worker supplies: `VAULT_DIR` (default `~/vault`), `JOB_TIMEOUT` (seconds, default
1800). Respect the timeout — budget against it and fail early rather than being killed
mid-write.

### Delivery is at-least-once

Leases expire and requeue, and the dedupe index only covers live jobs, so **every skill
must be safe to run twice**. In practice:

- Write files atomically: temp file in the destination directory, then `os.replace`.
- Never clobber what another skill added to a shared file — read it back and preserve it.
- Skip work that is already done, and take a `force` param to override the skip.

### Untrusted input

`path` and `source` originate from a Telegram message via an LLM. Resolve paths and
verify they sit under `VAULT` before reading or writing (see `read_note`). Without that
check, `../../.ssh/id_ed25519` is a readable file.

## Vault note format

One note per video, holding both the transcript and its summary. Two skills write to the
same file, so the summary is delimited by sentinels rather than by position:

```markdown
---
title: "..."
video_id: ...
transcript_method: captions:auto
---

<!-- summary:begin -->
## Summary

<!-- generated 2026-08-01 by gpt-oss:latest style=detailed -->

...summary text...
<!-- summary:end -->

...transcript...
```

- The summary sits **above** the transcript. Reading a note in Obsidian should not mean
  scrolling past 17k characters of unpunctuated captions.
- `transcribe_video` lifts the block out before overwriting the note, so re-transcribing
  keeps the summary.
- `summarize_transcript` strips the block before summarising, so it never summarises its
  own output, and replaces rather than stacks.
- Notes written before the sentinels existed have a trailing `## Summary` heading; both
  skills fall back to that and migrate the note on the next run.
- The `style=` stamp is what makes the skip decision possible — see below.

## The worker

[worker.py](worker.py) is what pulls the queue. It runs under launchd via
[com.hermes.worker.plist](com.hermes.worker.plist), installed to `~/Library/LaunchAgents/`.

Loop: long-poll `GET /jobs/next` → run the skill → `POST /jobs/{id}/complete`, one job at
a time, heartbeating every 30s while a job runs so a slow job is not requeued under it.

- **The manifests are the allowlist.** A job type with no `skill.yaml` is rejected as a
  permanent failure. Hermes cannot name a script that was not declared.
- **`caffeinate -dims` wraps the job, not the worker** — this is a laptop. It stays awake
  exactly while a job runs.
- **An unreachable queue is not an error.** Backoff 1s → 60s, because being off the
  network is this machine's normal state.
- **Bare `python3` in a manifest's `run` is rewritten to the worker's own interpreter**
  (`.venv/bin/python3`), so a skill never depends on what PATH resolves to.
- On SIGTERM it stops polling and exits; an in-flight job dies and its lease lapses, which
  is what requeues it.

```bash
.venv/bin/python3 worker.py --check    # validate config + skills, no network
.venv/bin/python3 worker.py --once     # handle a single job, then exit
tail -f ~/Library/Logs/hermes-worker.log

launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.hermes.worker.plist
launchctl bootout    gui/$UID/com.hermes.worker      # stop / before reinstall
launchctl print      gui/$UID/com.hermes.worker      # state, exit codes, env
```

Config is entirely environment, set in the plist's `EnvironmentVariables` block:
`QUEUE_URL`, `QUEUE_TOKEN`, `QUEUE_AUTH_HEADER`/`QUEUE_AUTH_SCHEME`, `WORKER_NAME`,
`POLL_WAIT`, `JOB_TIMEOUT`, `HEARTBEAT_INTERVAL`, `PUBLISH_MANIFEST`, `NTFY_TOPIC`, plus
everything the skills read. **The plist is the config file** — there is no dotenv loading,
and no third-party dependency to add it.

For running by hand, copy `.env.example` to `.env` (gitignored) and source it:

```bash
set -a; . ./.env; set +a
.venv/bin/python3 worker.py --check
```

Nothing reads `.env` automatically. It duplicates the plist rather than feeding it, so a
value changed in one does not reach the other.

The committed plist is a template: `QUEUE_URL` and `QUEUE_TOKEN` are `REPLACE_ME`, and the
real values go in the installed copy under `~/Library/LaunchAgents/`, which stays
`chmod 600` and out of version control. Fill in the installed copy, never this one.

If the queue's `/complete` payload shape ever differs from
`{status, result, error, permanent}`, `complete()` in `worker.py` is the single place to
change.

## Dependencies

`.venv/` holds the worker's only third-party dependency, `pyyaml`. The system `python3`
(3.14.5, `/usr/local/bin`) does not have it. The skills themselves are stdlib-only and
must stay that way — anything they need goes in this venv and gets documented here.

```bash
python3 -m venv .venv && .venv/bin/pip install pyyaml
```

## Skills

### `transcribe_video`

YouTube URL → note in `$VAULT/transcripts/`. Prefers published captions (manual over
auto-generated); falls back to local whisper only when there are none. Refuses videos
whose estimated whisper time exceeds `JOB_TIMEOUT * 0.8` unless `allow_long` is set —
better to fail in 2 seconds than at the timeout after 30 wasted minutes.

### `summarize_transcript`

Note → summary, via local Ollama. Long transcripts are chunked and map-reduced; at the
current context size most fit in a single pass, which produces a noticeably better
summary than merging partials.

Re-running is a no-op when a summary in the requested style already exists — it returns
the existing text with `regenerated: false` in ~50ms, without even probing Ollama. A
different `style`, or `force: true`, regenerates. This is what makes chaining
`transcribe_video → summarize_transcript` cheap to re-deliver.

## Local environment

Facts about this machine that the code is tuned to. Re-check them before changing sizing.

- **24 GB unified memory**, so roughly a 16 GB Metal budget.
- `gpt-oss:latest` (20.9B MoE, MXFP4) — 13.8 GB on disk, ~12.8 GB resident at 32k context.
  General-purpose and instruction-tuned; `qwen3-coder:30b` is also installed but is
  code-specialised and worse at prose, and at 18.6 GB it alone exceeds the budget. Do not
  expect both to be resident.
- `OLLAMA_NUM_CTX` defaults to **32768**. Ollama silently truncates to a couple of
  thousand tokens if `num_ctx` is unset — the single most common cause of "it only
  summarised the beginning". `CHUNK_CHARS` derives from it, so raising one adjusts the other.
- `OLLAMA_KEEP_ALIVE` is **30m**, sent per-request as well as set in the environment,
  because the worker runs under launchd and may not inherit a GUI-session env. The worker
  is the only Ollama consumer, so nothing contends for the 12.8 GB it pins.
- Ollama listens on **127.0.0.1 only**, and must stay that way. `~/.zshrc` used to set
  `OLLAMA_HOST=0.0.0.0` and `OLLAMA_ORIGINS=*`; both are removed and unset. Binding it to
  the tailnet would hand every peer — including `ubuntu-8gb-hel1-2` — an unauthenticated
  inference endpoint on the machine holding the vault and the SSH keys.
- Vault at `~/vault`, whisper models at `~/models`.
- `yt-dlp` and `ffmpeg` must be on `PATH`. Under launchd that is not the login shell's
  `PATH`, so Homebrew paths need setting explicitly in the plist.

## Adding a skill

1. `mkdir <type>/` with `run.py` and `skill.yaml`; `type:` must match the directory name.
2. Read params from stdin, print one JSON object to stdout, log to stderr, use the exit
   codes above.
3. Make it re-runnable. Assume it will be delivered twice.
4. Confine any caller-supplied path to the vault.
5. Keep `skill.yaml`'s `params` and `produces` in step with `run.py` — the worker
   republishes the manifest to Hermes, and that is what Hermes plans against. Stale
   manifests mean Hermes cannot call a param that exists.
6. Reload the worker so it picks up the new manifest:
   `launchctl bootout gui/$UID/com.hermes.worker && launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.hermes.worker.plist`

## Testing

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python3 -m pytest          # whole suite, ~13s
.venv/bin/python3 -m pytest -k skip  # one behaviour
.venv/bin/pyright                    # type check — same engine as the editor
.venv/bin/python3 -m pyflakes worker.py */run.py tests/*.py
```

All three are currently clean, so CI can gate on all three. Keep it that way: a
ratchet from a dirty baseline never gets tightened.

`pyright` is configured by `pyrightconfig.json` at default strictness. It has caught
things the tests and pyflakes both missed — an unbound `MODEL` global, a `method` that
was `str | None` at a call site typed `str`. `vps/` is excluded because `mcp` is not
installed here; that file is type-checked in the queue repo where it runs.

`tests/` covers the three scripts. It is hermetic — no network, no Ollama, no yt-dlp, no
real vault — so it is safe to run anywhere and suitable for CI:

- **Ollama** is mocked at `ollama()`, the single function wrapping the HTTP call. Transport
  behaviour (num_ctx, keep_alive, 404 → permanent, unreachable → transient) is tested by
  replacing `urllib.request.urlopen`, not by running a model.
- **yt-dlp and whisper** are mocked at `run()` / `probe()` / `whisper_transcribe()`.
- **The queue** is a real stdlib `http.server` bound to port 0 in-process (`StubQueue` in
  `conftest.py`), so lease → run → complete is exercised over real HTTP.
- **Skills the worker runs** are real subprocesses writing to `tmp_path`, so exit-code
  handling is genuinely tested rather than stubbed.

The modules read their config from the environment *at import time*, so the `load` fixture
imports a fresh module per test with the environment already patched. Do not `import` these
scripts directly in a test — use the fixture, or you get another test's configuration.

What the suite is really protecting, in order of how much a regression would cost:

1. A re-run must never clobber or stack a summary — silent data loss.
2. A caller-supplied path must not escape the vault — arbitrary file read.
3. Exit 2 stays permanent and exit 1 stays transient — a poisoned job must not cycle
   forever, and a transient one must not dead-letter.
4. An existing same-style summary is reused — ~30s of GPU per redundant job.
5. A long job heartbeats — otherwise the lease lapses and the queue reissues it underneath
   the worker.

For manual one-offs against the real vault, copy the note aside first and diff — the failure
mode to watch for is a clobbered summary, which is silent.

## Not built yet

- **Chaining** `transcribe_video → summarize_transcript` as one Hermes job. If the worker
  enqueues the summarize step after transcribe completes, give the child its own
  idempotency key (`hash("summarize_transcript" + path)`) or a lapsed parent lease will
  enqueue a duplicate. Both steps are already idempotent, so re-delivering the pair is
  cheap.
- **Ollama has no LaunchAgent** — it starts with the app. A summarize job that lands while
  Ollama is down fails transient and retries, which recovers but is noisy.
