# fi-skills-worker

Skills that a Mac runs on behalf of **Fi**, a Claude project connected to the `fi-hub`
Cloudflare Worker.

Fi is always reachable but has no CPU, no GPU, and no access to the knowledge vault. The
Mac has all three but is frequently asleep. This repo is the half that runs on the Mac: a
worker that pulls jobs from fi-hub's queue and the skills it executes.

## Why it pulls instead of being pushed to

Mounting the Mac's filesystem into a remote host was tried first and abandoned. A filesystem
transport moves bytes, not processes — the script still ran on the remote CPU and RAM, and
per-syscall round-trips over the tunnel made it slower than running nothing at all.

Inverting the direction fixed it:

```
You → Fi (Claude) → enqueue_job() → fi-hub (Cloudflare Worker + SQLite Durable Object)
                                                  ↑ HTTPS short-poll, outbound only
                                          worker (Mac, launchd)
                                                  ↓
                                    runs the skill, writes the vault
                                                  ↓
                                      POST /jobs/{id}/complete
                                      POST /notes     ← the notes, in full, searchable
```

Four consequences, and most of the design follows from them:

- **The Mac being offline is not a special case.** It is a job that has not been leased
  yet. It runs when the Mac next wakes.
- **Work happens on local disk.** No NFS, no SSHFS, no UID mapping. Only JSON crosses the
  wire.
- **The Mac never accepts an inbound connection**, and fi-hub holds no credentials into
  it. Nothing internet-facing has a shell on the machine holding the vault and the SSH
  keys.
- **Fi gets typed job types from an allowlist, not a shell.** The manifests below are that
  allowlist — a job type with no `skill.yaml` is rejected before anything executes.

## Capabilities

| Skill | Description | Params |
|---|---|---|
| `transcribe_video` | YouTube URL → note in `$VAULT/transcripts/`. Prefers published captions (manual over auto-generated) and falls back to local whisper only when there are none. Returns `transcript_path`, `method`, `title`, `channel`, `duration_s`, `video_id`, `chars`. | `source` *(required)* — video URL<br>`language` — ISO-639-1 or `auto` *(default `auto`)*<br>`force_whisper` — ignore published captions *(default `false`)*<br>`allow_long` — bypass the duration guard *(default `false`)* |
| `summarize_transcript` | Vault note → summary, via local Ollama, written back above the transcript. Long transcripts are chunked and map-reduced. Returns `summary`, `summary_path`, `source_path`, `source_chars`, `model`, `chunks`, `style`, `regenerated`. | `path` *(required)* — vault-relative note path<br>`style` — `brief` \| `detailed` \| `bullets` *(default `detailed`)*<br>`model` — override `OLLAMA_MODEL` for one job<br>`write_back` — write into the note *(default `true`)*<br>`force` — redo an existing summary *(default `false`)* |
| `summarize_url` | Article or blog URL → note in `$VAULT/transcripts/`, summary above the extracted article text. Fetched with curl, extracted with `html.parser`, summarised locally. Returns `note_path`, `url`, `title`, `site`, `chars`, `summary`, `model`, `chunks`, `style`, `regenerated`, `fetched`. | `source` *(required)* — article URL<br>`style` — `brief` \| `detailed` \| `bullets` *(default `detailed`)*<br>`model` — override `OLLAMA_MODEL` for one job<br>`force` — redo an existing summary *(default `false`)*<br>`refetch` — re-download instead of re-using the stored text *(default `false`)* |

Each skill's `skill.yaml` is the authoritative signature. The worker republishes those
manifests to fi-hub on startup, so the agent plans against what is actually installed
rather than against a hardcoded list.

## Finding things again

Creating notes is only half of it — the agent also has to be able to find them, and the
vault is on a machine it cannot read. So the worker pushes **every note, in full**, to
fi-hub: the text, the frontmatter and the summary. fi-hub stores it and indexes it for
hybrid (semantic and keyword) search, which means "do I have anything about X?" is answered
from the cloud, **including while the Mac is asleep** — the one class of request the pull
design otherwise handles badly — and the agent can read a note back to quote it exactly. A
new note is searchable about 90 seconds after it is pushed.

The vault stays canonical: the cloud copy is rebuilt, never merged. A full push runs on
startup and hourly, followed by a reconcile that drops notes deleted here, and a single note
is pushed after each job that writes one. A push that fails is logged and dropped; it can
never turn a job that succeeded into one that failed.

The consequence to know about: **the text of every note leaves this machine.** Anything
saved to the vault is stored on Cloudflare, so do not put in the vault what should not be
there.

## Skill contract

- Params arrive as a **single JSON object on stdin**.
- The result is a **JSON object on the last line of stdout**. Nothing else goes to stdout.
- Logs and progress go to **stderr**.
- Exit `0` success · `1` transient, the worker retries · `2` permanent, it does not.

Delivery is **at-least-once** — leases expire and requeue, and the dedupe index only
covers live jobs — so every skill must be safe to run twice. In practice that means atomic
writes, never clobbering what another skill added to a shared file, and skipping work that
is already done unless `force` says otherwise.

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

```bash
.venv/bin/python3 worker.py --check    # validate config and skills, no network
```

Configuration is entirely environment variables. Under launchd they come from the
`EnvironmentVariables` block in `com.hermes.worker.plist`; for manual runs, copy
`.env.example` to `.env` and source it:

```bash
set -a; . ./.env; set +a && .venv/bin/python3 worker.py --once
```

Install as a LaunchAgent (fill in `QUEUE_URL` and `QUEUE_TOKEN` in the installed copy, not
the committed template):

```bash
cp com.hermes.worker.plist ~/Library/LaunchAgents/ && chmod 600 ~/Library/LaunchAgents/com.hermes.worker.plist
```

```bash
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.hermes.worker.plist
```

A single skill can also be run directly:

```bash
echo '{"path":"transcripts/some-note.md"}' | .venv/bin/python3 summarize_transcript/run.py
```

## Tests

```bash
.venv/bin/python3 -m pytest    # 284 tests, ~1 min
```

The suite is hermetic — no network, no Ollama, no yt-dlp, no curl, no real vault — so it
runs anywhere and gates CI alongside `pyflakes` and `pyright`. Ollama and curl are each
mocked at the single function wrapping their call; the queue is a real `http.server` on an
ephemeral port; the skills the worker executes are real subprocesses, so exit-code handling
is genuinely exercised rather than stubbed.

## Layout

```
worker.py                 poll → lease → run → heartbeat → complete, and the notes push
com.hermes.worker.plist   LaunchAgent template (token-free)
skilllib/                 note format + Ollama client, shared by the skills
transcribe_video/         run.py + skill.yaml
summarize_transcript/     run.py + skill.yaml
summarize_url/            run.py + skill.yaml
tests/                    pytest suite + fixtures
CLAUDE.md                 design notes, environment specifics, conventions
```

The queue and its MCP server are the `fi-hub` Worker, in a separate repo. Fi's project
instructions are its `docs/CLAUDE-PROJECT.md`.
