# skills-worker

Skills that a worker on this Mac runs on behalf of **Fi**, the Claude project that talks to
the **fi-hub** Cloudflare Worker.

Each skill subdirectory is a `run.py` that does the work and a `skill.yaml` that declares
it to the worker's manifest. [skilllib/](skilllib/) is not a skill — it is the code they
share.

## Why this exists

Fi runs in Claude (phone and desktop), so it is always reachable. The knowledge vault and the
heavy tooling (yt-dlp, whisper, Ollama) live on this Mac, which is not always on.

Mounting the Mac's filesystem into a remote host was tried first and abandoned: a filesystem
transport moves bytes, not processes. The script still ran on the remote CPU and RAM, and
per-syscall round-trips over the tunnel made it slower than before.

The working design inverts the direction — **the Mac pulls**. The queue is `fi-hub`, a
Cloudflare Worker (queue and MCP server in one, with a SQLite Durable Object). It replaced
a Hermes agent and a queue on a VPS, reached over Tailscale; that path is kept only as a
rollback (tag `pre-fi-hub`).

```
You → Fi (Claude) ──MCP, OAuth──→ fi-hub (Cloudflare) ← enqueue_job, search_notes, get_note
                                      ↑ HTTPS short-poll, bearer token, outbound only
                              Mac worker (launchd, KeepAlive)
                                      ↓
                        runs the skill, writes the vault
                                      ↓
                          POST /jobs/{id}/complete
                          POST /notes  ← the FULL text of each note, so Fi can search
                                         and quote it while the Mac sleeps
```

Consequences that shape the code here:

- The Mac being offline is not a special case. It is a job that has not been leased yet.
- Work happens on local disk. No NFS, no SSHFS, no UID mapping, no `server-filesystem`.
- Only JSON crosses the wire. The Mac never accepts an inbound connection, and fi-hub holds
  no credentials into it.
- Fi gets typed job types from an allowlist (the manifests this worker publishes), not a
  shell.
- The queue is a *short* poll: it answers an empty queue at once. The worker sleeps between
  polls (`POLL_INTERVAL`); without that it would spin and exhaust the Free plan's request
  budget.

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

Loop: `GET /jobs/next` (sleep `POLL_INTERVAL` on 204) → run the skill →
`POST /jobs/{id}/complete`, one job at a time, heartbeating every 30s while a job runs so a
slow job is not requeued under it. The default lease is 120s.

- **The manifests are the allowlist.** A job type with no `skill.yaml` is rejected as a
  permanent failure. Fi cannot name a script that was not declared.
- **`caffeinate -dims` wraps the job, not the worker** — this is a laptop. It stays awake
  exactly while a job runs.
- **An unreachable queue is not an error.** Backoff 1s → 60s, because being off the
  network is this machine's normal state. A failed TLS handshake and a refused token
  (HTTP 401) are *not* outages and are logged as what they are: calling them "queue
  unavailable" hides a missing `SSL_CERT_FILE` or a rotated token behind a normal state.
- **HTTPS needs `SSL_CERT_FILE=/etc/ssl/cert.pem`** (set in the plist). This Python has no
  root certificates; see *Local environment*.
- **Every request sends `User-Agent: fi-skills-worker/1.0`, and that is load-bearing.**
  Cloudflare's edge answers the default `Python-urllib/3.x` agent with `403 error code:
  1010` *before the request reaches fi-hub*, so the token is never even checked. Found at
  the first live reload: TLS worked, every call failed, and the stub-only test suite could
  not see it because the stub is not Cloudflare. `EdgeBlocked` classifies that 403 (a body
  naming a Cloudflare 1xxx error) and the loop and `--probe` report it as "blocked at the
  Cloudflare edge (User-Agent?)". fi-hub itself answers 401, never 403, for a bad token.
  `StubQueue(edge_block=...)` reproduces the edge, so dropping the header fails tests.
- **A lost lease (HTTP 409) is logged with the job id and is not fatal.** The heartbeat
  stops, `complete()` returns False, and the note the job wrote is still pushed: the job
  will run again, which is safe, but the work on disk is real.
- **The manifest is published until it lands**, and again with every hourly push. A launch
  with no network would otherwise leave fi-hub refusing `enqueue_job` until the next
  restart.
- **Bare `python3` in a manifest's `run` is rewritten to the worker's own interpreter**
  (`.venv/bin/python3`), so a skill never depends on what PATH resolves to.
- On SIGTERM/SIGINT it sets a stop flag: it stops polling, but a running job is not
  interrupted — the worker exits when the job returns. launchd follows with SIGKILL after
  its exit timeout, so a long job usually dies mid-run; its lease then lapses, and that is
  what requeues it.

```bash
.venv/bin/python3 worker.py --check    # validate config + skills, no network
.venv/bin/python3 worker.py --probe    # GET /healthz over HTTPS, with and without the token,
                                       # plus offline manifest validation. Reports TLS, auth
                                       # and reachability separately; prints no token, and
                                       # makes no call fi-hub counts as "the Mac was seen"
.venv/bin/python3 worker.py --once     # handle a single job, then exit
tail -f ~/Library/Logs/hermes-worker.log

launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.hermes.worker.plist
launchctl bootout    gui/$UID/com.hermes.worker      # stop / before reinstall
launchctl print      gui/$UID/com.hermes.worker      # state, exit codes, env
```

Config is entirely environment, set in the plist's `EnvironmentVariables` block:
`QUEUE_URL`, `QUEUE_TOKEN`, `QUEUE_AUTH_HEADER`/`QUEUE_AUTH_SCHEME`, `WORKER_NAME`,
`POLL_INTERVAL`, `LEASE_SECONDS`, `POLL_WAIT` (sent as `wait`, which fi-hub ignores),
`JOB_TIMEOUT`, `HEARTBEAT_INTERVAL`, `PUBLISH_MANIFEST`, `PUBLISH_CATALOG` (the notes push),
`CATALOG_RECONCILE_S`, `SSL_CERT_FILE`, plus everything the skills read. There is no ntfy:
pushes are server-side, and the worker sends none. **The plist is the
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

## The notes push

The vault is here; Fi runs in the cloud and cannot read it. Without something in between, Fi
can create knowledge and never find it again — it has no tool that looks, only tools that
enqueue more work.

So the worker pushes **every note, in full**, to fi-hub (`POST /notes`). fi-hub stores the
text, indexes it (Cloudflare AI Search), and serves `search_notes`, `get_note` and
`list_notes`. A note is searchable 70 to 90 seconds after it is pushed. What crosses the wire
per note, built by `note_push_row`:

```
path, markdown, title, url, kind, date, sha256
```

| Field | Value |
|---|---|
| `markdown` | the **full file text**, frontmatter and summary block included |
| `sha256` | hex of the file **bytes**; the same hash again is a no-op on the hub |
| `kind` | `video` if the note has `video_id` or `transcript_method`, else `article` |
| `date` | **upload date for videos, fetch date for articles** (`fetched_at`), `""` if none |
| `title`, `url` | from the frontmatter; the title falls back to the file name |

`kind` is read from the **raw frontmatter keys**. `note_row` folds an article's `url_id` into
`video_id` and its `fetch_method` into `transcript_method` so one shape serves both; deriving
`kind` from that folded row would call every article a video. The date is the fetch date, not
the article's own publication date, because the notes already in the index were pushed that
way and changing it would re-date them.

**The transcript body leaving this machine is now the invariant.** It used to be the reverse
(a summary-only index, on the grounds that quoting needed a search skill here); Fi now quotes
from the body through `get_note`. The rule that replaces it is stricter about *what* is read:
the path is resolved and confined to the vault **before** the file is opened, so a symlink in
the vault pointing at `~/.ssh` is never read, because read now means published.

Three triggers, all in `worker.py`:

| When | Payload | Why |
|---|---|---|
| Startup, after the manifest push | the whole vault, then reconcile | reconciles anything missed while off |
| After a job returning `transcript_path` / `summary_path` / `note_path` | that one note | new work is searchable immediately |
| Every `CATALOG_RECONCILE_S` (3600) | the whole vault, then reconcile; the manifest too | a note edited in Obsidian is reported by no job |

A failed full push is retried after 5 minutes rather than an hour: a launch with no network
would otherwise leave the hub without the vault that long.

**Batching.** At most 200,000 bytes and 10 notes a request, packed in order. A note over the
byte limit goes alone and is accepted (the hub's hard caps are 1,000,000 bytes and 40 notes);
the worker reads `limits` from every response and uses them for the next batch. The request
timeout is 90s. A note that gets HTTP 413 on its own is logged and skipped, never retried in
a loop, and still counts as a note the vault holds. Other HTTP errors fail that batch and the
rest go on; a transport error (unreachable, TLS, 401) ends the push.

**Deletion is a separate call, never a flag on a batch.** After a full push in which *every
batch landed*, the worker sends `POST /notes/reconcile` with `{path, sha256}` for every note
(no text), and repeats until `pending_deletes` is 0 (the hub deletes 20 a call). That is how
a note deleted in Obsidian leaves the index. A crash or failure mid-push therefore deletes
nothing. An empty vault is never reconciled — the hub refuses it, because it looks like a
vault that failed to mount. Whatever reconcile reports `missing` or `stale` is pushed once
more in the same cycle; one pass, no second reconcile.

Failure is never fatal: `notes_after()` swallows everything — it runs *after* the job has
been reported done, so nothing it does may turn a success into a failure. Unlike the old
catalog, a 404 does not switch pushes off for the process: here it means `QUEUE_URL` lost its
`/worker`, and that should stay visible.

**`worker.py` parses the note format a third time** (both skills already do). That is
deliberate, and the reason it is acceptable is that this copy only ever reads: its worst
failure is a missing or stale note on the hub, fixed by the next push. The skills' copies
guard against clobbering a summary, which is not recoverable. Do not merge the two without
moving all three onto a shared module. What has changed is the cost of a *read* going wrong:
it publishes. That is why `load_note` confines the path first.

**Known limit: a note with CRLF line endings is not indexed.** `notefmt.FRONTMATTER`
requires `---\n`, so a file Obsidian (or anything else) saved with `\r\n` has no
recognised frontmatter and `load_note` skips it, silently apart from the "no usable
frontmatter" count. This predates the fi-hub work; the skills only write LF. It matters only
if a note is ever edited somewhere that rewrites line endings. Fixing it means changing
`skilllib`, which every skill shares, so it is left alone.

A note that has never been summarised is still pushed whole; Fi can quote its transcript but
finds no summary at the top. That is the strongest argument for chaining the two skills.

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
signature — the worker republishes it to fi-hub, and that is what Fi plans
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
   republishes the manifest to fi-hub, and that is what Fi plans against. fi-hub's
   `enqueue_job` refuses any parameter a manifest does not declare, with its type and enum, so
   a stale manifest makes a working param unusable. `worker.py --probe` validates them.
7. Reload the worker so it picks up the new manifest:
   `launchctl bootout gui/$UID/com.hermes.worker && launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.hermes.worker.plist`

## Testing

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python3 -m pytest          # whole suite, 284 tests, ~1 min
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
- **The queue** (fi-hub's `/worker` API, shapes from its handoff) is a real stdlib `http.server` bound to port 0 in-process (`StubQueue` in
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
5. What is pushed is exactly the file — the full text and the hash of its bytes — and
   nothing outside the vault is read to get it; a push failure never fails a job. (This
   replaced "the catalog never sends the transcript body", which is intentionally reversed.)
6. A long job heartbeats — otherwise the lease lapses and the queue reissues it underneath
   the worker.
7. An empty poll sleeps, and a full push reconciles only after every batch landed — a spin
   exhausts the Free plan, and an early reconcile deletes notes that were merely not sent yet.

For manual one-offs against the real vault, copy the note aside first and diff — the failure
mode to watch for is a clobbered summary, which is silent.

## Not built yet

- **Chaining** `transcribe_video → summarize_transcript` as one fi-hub job. If the worker
  enqueues the summarize step after transcribe completes, give the child its own
  idempotency key (`hash("summarize_transcript" + path)`) or a lapsed parent lease will
  enqueue a duplicate. Both steps are already idempotent, so re-delivering the pair is
  cheap.
- **Ollama has no LaunchAgent** — it starts with the app. A summarize job that lands while
  Ollama is down fails transient and retries, which recovers but is noisy.
- **Embeddings and search tuning** are fi-hub's, not this repo's: AI Search does hybrid
  (semantic and keyword) search over the pushed text. Tuning it is a follow-up there.
- **X/Twitter.** `summarize_url` rejects it. The free syndication endpoint gives one post
  with no replies; the thread needs the paid API or a logged-in session, and the
  Chrome-cookie path is already unreliable under launchd.
- **PDFs.** Rejected by content type. No PDF text extractor is installed either.
