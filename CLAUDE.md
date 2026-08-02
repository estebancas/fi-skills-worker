# skills-worker

Skills that a worker on this Mac runs on behalf of the **Hermes** agent.

Each skill subdirectory is a `run.py` that does the work and a `skill.yaml` that declares
it to the worker's manifest. [skilllib/](skilllib/) is not a skill — it is the code they
share.

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
                                          POST /catalog   ← index of the vault,
                                                            so Hermes can search
                                                            it while the Mac sleeps
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

`path` and `source` originate from a Telegram message via an LLM.

- **Paths**: resolve and verify they sit under `VAULT` before reading or writing (see
  `read_note`). Without that check, `../../.ssh/id_ed25519` is a readable file.
- **URLs**: resolve the host and refuse loopback, RFC-1918, link-local, and `100.64/10`
  before fetching, and again after redirects (see `guard_host`). Without that check,
  `http://127.0.0.1:11434` is Ollama and the whole tailnet is reachable.

## Vault note format

One note per source — a video transcript or an article — holding both the source text and
its summary. Several skills write to the same file, so the summary is delimited by
sentinels rather than by position:

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

- The summary sits **above** the source text. Reading a note in Obsidian should not mean
  scrolling past 17k characters of unpunctuated captions.
- Articles use the same layout with `site`/`url_id`/`published`/`fetch_method` in place of
  `channel`/`video_id`/`upload_date`/`transcript_method`.
- All of this lives in [skilllib/notefmt.py](skilllib/notefmt.py). Do not re-implement it.
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
`POLL_WAIT`, `JOB_TIMEOUT`, `HEARTBEAT_INTERVAL`, `PUBLISH_MANIFEST`, `PUBLISH_CATALOG`,
`CATALOG_RECONCILE_S`, `NTFY_TOPIC`, plus everything the skills read. **The plist is the
config file** — there is no dotenv loading, and no third-party dependency to add it.

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

## `skilllib/`

Stdlib-only package at the repo root, imported by the skills and by `worker.py`. It exists
because the note format had been written three times and the Ollama client was about to be
written twice.

- **`notefmt.py`** — sentinels, `split_summary`, `make_block`, `parse_block`, frontmatter
  splitting, `slugify`, `atomic_write`, `compose`. Getting this wrong destroys a summary
  another skill wrote, so it lives in one place.
- **`llm.py`** — `Config`, `generate`, `check_model`, `chunk`, `summarise`, `STYLES`,
  `SYSTEMS`.

Two rules it follows, both load-bearing:

- **Nothing reads the environment at import time.** The test suite re-imports skills to
  pick up patched environments; a shared module is cached in `sys.modules`, so import-time
  config would freeze the first test's settings into every later one. Configuration is an
  explicit `llm.Config` built inside `main()`.
- **It raises `LLMError` rather than calling `sys.exit`.** A library should not decide a
  process's exit code. Each skill catches it and maps `.permanent` onto the 2/1 contract.

Skills run with their own directory as cwd, so each `run.py` starts with:

```python
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from skilllib import llm, notefmt
```

`load_skills` globs `*/skill.yaml`, so `skilllib/` is never mistaken for a skill.

`SYSTEMS` holds one system prompt per source kind. The transcript prompt tells the model to
read through caption noise; the article prompt tells it to ignore navigation and cookie
banners. Using the transcript prompt on clean prose makes the model hedge, and vice versa.

## The catalog

The vault is here; Fi is on the VPS and cannot read it. Without something in between, Fi
can create knowledge and never find it again — it has no tool that looks, only tools that
enqueue more work.

So the worker replicates an **index** of the vault to the queue, where SQLite FTS5 makes it
searchable. What crosses the tunnel:

```
path, title, channel, video_id, url, upload_date, duration_s,
transcript_method, chars, summary, summary_style, has_summary,
sha256, modified_at
```

Articles carry `site`, `url_id`, `published`, and `fetch_method` where videos carry
`channel`, `video_id`, `upload_date`, and `transcript_method`. `note_row` falls back
between them, so both shapes land in one index and the VPS schema never learned there are
two kinds of note.

**The transcript body is not in that list, deliberately.** Fi can find the right note and
report its summary; it cannot quote what was actually said. Quoting needs a `search_vault`
skill running here — see *Not built yet*.

Why this and not a synced folder on the VPS: an S3 (or SSHFS) mount is a filesystem
transport, and a grep across it is one round-trip per file — the same mistake documented
at the top of this file. It also makes freshness depend on Obsidian being open, which
turns "the Mac is asleep" from a visibly queued job into a silently stale answer.

Three triggers, all in `worker.py`:

| When | Payload | Why |
|---|---|---|
| Startup, after the manifest push | full snapshot, `full: true` | reconciles anything missed while off |
| After a job returning `transcript_path` / `summary_path` | that one note | new work is searchable immediately |
| Every `CATALOG_RECONCILE_S` (3600) | full snapshot | a note edited in Obsidian is reported by no job |

`full: true` also means "delete rows you don't see here", which is how a note deleted in
Obsidian leaves the index. Deletions therefore propagate on a full push, not instantly.

Failure is never fatal: a 404 disables catalog pushes for the process and logs once, and
`catalog_after()` swallows everything — it runs *after* the job has been reported done, so
nothing it does may turn a success into a failure.

**`worker.py` parses the note format a third time** (both skills already do). That is
deliberate, and the reason it is acceptable is that this copy only ever reads: its worst
failure is a missing catalog row, fixed by the next reconcile. The skills' copies guard
against clobbering a summary, which is not recoverable. Do not merge the two without
moving all three onto a shared module.

A note with `has_summary: false` has never been summarised, so only its title and channel
are indexed. That is the strongest argument for chaining the two skills.

## Dependencies

`.venv/` holds the worker's only third-party dependency, `pyyaml`. The system `python3`
(3.14.5, `/usr/local/bin`) does not have it. The skills and `skilllib/` are stdlib-only
and must stay that way — anything they need goes in this venv and gets documented here.

External binaries they shell out to: `yt-dlp`, `ffmpeg`, `whisper-cli` (Homebrew),
and `/usr/bin/curl` (system, and the only working HTTPS client here — see *Local
environment*).

```bash
python3 -m venv .venv && .venv/bin/pip install pyyaml
```

## Skills

What the worker can currently do. `skill.yaml` is the authoritative copy of each
signature — the worker republishes it to the queue, and that is what Hermes plans
against, so keep this table and the manifests in step.

| Skill | Description | Params |
|---|---|---|
| `transcribe_video` | YouTube URL → note in `$VAULT/transcripts/`. Prefers published captions (manual over auto-generated) and falls back to local whisper only when there are none. Returns `transcript_path`, `method`, `title`, `channel`, `duration_s`, `video_id`, `chars`. | `source` *(required)* — video URL<br>`language` — ISO-639-1 or `auto` *(default `auto`)*<br>`force_whisper` — ignore published captions *(default `false`)*<br>`allow_long` — bypass the duration guard *(default `false`)* |
| `summarize_transcript` | Vault note → summary, via local Ollama, written back above the transcript. Long transcripts are chunked and map-reduced. Returns `summary`, `summary_path`, `source_path`, `source_chars`, `model`, `chunks`, `style`, `regenerated`. | `path` *(required)* — vault-relative note path<br>`style` — `brief` \| `detailed` \| `bullets` *(default `detailed`)*<br>`model` — override `OLLAMA_MODEL` for one job<br>`write_back` — write into the note *(default `true`)*<br>`force` — redo an existing summary *(default `false`)* |
| `summarize_url` | Article or blog URL → note in `$VAULT/transcripts/`, summary above the extracted article text. Fetches with curl, extracts with `html.parser`, summarises with Ollama. Returns `note_path`, `url`, `title`, `site`, `chars`, `summary`, `model`, `chunks`, `style`, `regenerated`, `fetched`. | `source` *(required)* — article URL<br>`style` — `brief` \| `detailed` \| `bullets` *(default `detailed`)*<br>`model` — override `OLLAMA_MODEL` for one job<br>`force` — redo an existing summary *(default `false`)*<br>`refetch` — re-download instead of re-using stored text *(default `false`)* |

### `transcribe_video` — notes

Refuses videos whose estimated whisper time exceeds `JOB_TIMEOUT * 0.8` unless
`allow_long` is set — better to fail in 2 seconds than at the timeout after 30 wasted
minutes. The estimate is `duration * WHISPER_COST + 60`.

### `summarize_transcript` — notes

At the current context size most transcripts fit in a single pass, which produces a
noticeably better summary than merging partials.

Re-running is a no-op when a summary in the requested style already exists — it returns
the existing text with `regenerated: false` in ~50ms, without even probing Ollama. A
different `style`, or `force: true`, regenerates. This is what makes chaining
`transcribe_video → summarize_transcript` cheap to re-deliver.

### `summarize_url` — notes

**It fetches with `/usr/bin/curl`, not `urllib`, and that is not a style choice.**
This machine's Python has no root certificates —
`ssl.get_default_verify_paths().cafile` is `None` and the framework's cert directory is
empty — so *every* HTTPS request from it fails `CERTIFICATE_VERIFY_FAILED`. It has never
surfaced because `transcribe_video` shells out to yt-dlp, which carries its own certs,
and both Ollama and the queue are plain HTTP. curl uses the macOS system trust store.
Running `/Applications/Python 3.14/Install Certificates.command` would fix the underlying
gap, but nothing here depends on it.

No HTML extractor exists on this machine — no pandoc, lynx, trafilatura — so extraction
is a `html.parser` subclass: drop the furniture (`script`, `nav`, `header`, `footer`,
`aside`, …), then prefer the **longest** `<article>`, falling back to `<main>` and then
the whole body. Longest, because index pages wrap every card in its own `<article>`.

Three things worth knowing:

- **`<meta http-equiv="refresh">` is followed manually.** curl's `-L` does not see it, and
  it usually hides inside `<noscript>` — which the extractor otherwise drops wholesale, so
  meta tags are read even inside dropped elements. Without this, a moved article extracts
  to "Click here to be redirected" and gets reported as needing JavaScript.
- **Under 500 extracted characters is a permanent failure**, not a summary. A single-page
  app otherwise produces a confident summary of its navigation bar.
- **The URL is guarded before it is fetched, and again after redirects.** It arrives from
  a Telegram message via an LLM, so a host resolving to loopback, RFC-1918, link-local, or
  `100.64/10` is refused — `127.0.0.1:11434` is Ollama and the tailnet is everything else.
  `ALLOW_PRIVATE_URLS=1` opens it for testing.

X/Twitter is rejected with an explanation rather than half-supported: the free syndication
endpoint returns a single post with no replies, and the thread needs an authenticated API.

Identity is `sha256(normalised URL)[:11]`, where normalising strips the fragment and
tracking parameters — so the same article pasted with different campaign tags is one note,
not three. A style change re-summarises from the stored article text without re-fetching;
only `refetch: true` goes back to the network.

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
- **Python has no root certificates.** `ssl.get_default_verify_paths().cafile` is `None`
  and `/Library/Frameworks/Python.framework/Versions/3.14/etc/openssl/certs` is empty, so
  every HTTPS request from the venv fails `CERTIFICATE_VERIFY_FAILED`. Anything here that
  reaches the public web goes through `/usr/bin/curl` (system trust store) or a tool that
  carries its own certs. `/Applications/Python 3.14/Install Certificates.command` fixes it
  at the source; nothing currently depends on that having been run.
- No HTML-to-text tool is installed — no pandoc, lynx, w3m, trafilatura, readability.

## Adding a skill

1. `mkdir <type>/` with `run.py` and `skill.yaml`; `type:` must match the directory name.
2. Read params from stdin, print one JSON object to stdout, log to stderr, use the exit
   codes above.
3. Make it re-runnable. Assume it will be delivered twice.
4. Confine any caller-supplied path to the vault, and any caller-supplied URL to public
   addresses (`guard_host` in [summarize_url/run.py](summarize_url/run.py)).
5. Reuse `skilllib` for the note format and the model — do not write a fourth copy. Add
   the two-line `sys.path` bootstrap at the top of `run.py`.
6. Keep `skill.yaml`'s `params` and `produces` in step with `run.py` — the worker
   republishes the manifest to Hermes, and that is what Hermes plans against. Stale
   manifests mean Hermes cannot call a param that exists.
7. Reload the worker so it picks up the new manifest:
   `launchctl bootout gui/$UID/com.hermes.worker && launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.hermes.worker.plist`

## Testing

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python3 -m pytest          # whole suite, 216 tests, ~20s
.venv/bin/python3 -m pytest -k skip  # one behaviour
.venv/bin/pyright                    # type check — same engine as the editor
.venv/bin/python3 -m pyflakes worker.py skilllib/*.py */run.py tests/*.py
```

All three are currently clean, so CI can gate on all three. Keep it that way: a
ratchet from a dirty baseline never gets tightened.

`pyright` is configured by `pyrightconfig.json` at default strictness. It has caught
things the tests and pyflakes both missed — an unbound `MODEL` global, a `method` that
was `str | None` at a call site typed `str`. `vps/` is excluded because `mcp` is not
installed here; that file is type-checked in the queue repo where it runs.

`tests/` covers the three skills, the worker, and `skilllib`. It is hermetic — no network, no Ollama, no yt-dlp, no
real vault — so it is safe to run anywhere and suitable for CI:

- **Ollama** is mocked at `llm.generate()`, the single function wrapping the HTTP call.
  Transport behaviour (num_ctx, keep_alive, 404 → permanent, unreachable → transient) is
  tested by replacing `urllib.request.urlopen`, not by running a model.
- **yt-dlp and whisper** are mocked at `run()` / `probe()` / `whisper_transcribe()`.
- **curl** is mocked at `fetch()`. The extractor is exercised against fixture HTML, which
  is where the value is — that is the part with no library behind it.
- **The queue** is a real stdlib `http.server` bound to port 0 in-process (`StubQueue` in
  `conftest.py`), so lease → run → complete is exercised over real HTTP.
- **Skills the worker runs** are real subprocesses writing to `tmp_path`, so exit-code
  handling is genuinely tested rather than stubbed.

The skills read their config from the environment *at import time*, so the `load` fixture
imports a fresh module per test with the environment already patched. Do not `import` these
scripts directly in a test — use the fixture, or you get another test's configuration.
`skilllib` is exempt and imported normally, because it deliberately reads nothing at import
time; that is what makes it safe to share across a cached `sys.modules`.

What the suite is really protecting, in order of how much a regression would cost:

1. A re-run must never clobber or stack a summary — silent data loss.
2. A caller-supplied path must not escape the vault, and a caller-supplied URL must not
   reach this network — arbitrary file read, and SSRF onto Ollama and the tailnet.
3. Exit 2 stays permanent and exit 1 stays transient — a poisoned job must not cycle
   forever, and a transient one must not dead-letter.
4. An existing same-style summary is reused — ~30s of GPU per redundant job.
5. The catalog never sends the transcript body, and a catalog failure never fails a job.
6. A long job heartbeats — otherwise the lease lapses and the queue reissues it underneath
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
- **Transcript-body search.** A `search_vault` skill here, SQLite FTS5 over note bodies,
  returning ranked snippets. Needed the first time Fi has to quote something the summary
  left out. The catalog answers "which note", not "what exactly did it say".
- **Embeddings.** Worth it once the catalog passes ~200 notes, or when recall gets
  complaints: `nomic-embed-text` (274 MB) in Ollama, vectors beside the FTS5 rows, fused
  by reciprocal rank. Not before — bm25 over titles and summaries is not the bottleneck at
  this size.
- **X/Twitter.** `summarize_url` rejects it. The free syndication endpoint gives one post
  with no replies; the thread needs the paid API or a logged-in session, and the
  Chrome-cookie path is already unreliable under launchd.
- **PDFs.** Rejected by content type. No PDF text extractor is installed either.
