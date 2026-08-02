# Wiring Hermes to the queue

What Hermes needs in order to use the Mac worker: three tools, a block of instructions,
and a few VPS-side checks.

Written from the Mac side — I can see the worker, the queue's HTTP surface, and the skill
manifests, but not Hermes' own code. Adapt the tool bodies to whatever framework it uses.

---

## 1. Prompt block for Hermes

Paste into Hermes' system prompt.

```
## Running work on the Mac

You cannot run scripts, transcribe video, or read the knowledge vault yourself. Those
live on a Mac that is not always online. You reach them by putting a job on a queue; a
worker on the Mac picks it up and does the work.

You have three tools for running work: enqueue_job, job_status, list_jobs. You have three
more for reading what has already been saved: search_notes, list_notes, get_note.

Search before you transcribe. If the user asks about a video, or gives you a URL, call
search_notes first — re-transcribing something already in the vault wastes minutes of GPU
and produces the note that is already there.

The catalog holds each note's metadata and its summary. It does NOT hold the transcript
itself. So you can say which note covers a topic and what its summary says, and you can
quote the summary. You cannot quote what was said in the video. If the user needs exact
wording, say the transcript is on the Mac and is not searchable yet — do not paraphrase
and present it as a quote.

A result with has_summary false has never been summarised, so only its title was
searchable. If it looks relevant, offer to summarise it: enqueue summarize_transcript
with its path.

The catalog lives on this machine, so search works even when the Mac is offline. It is
refreshed when the Mac is awake, so a note added in the last hour while the Mac was
asleep may not be there yet.

Job types are declared by the worker, not by you. Call list_jobs (or read the published
manifest) rather than guessing a type or a parameter name. Currently:

  transcribe_video       source (URL, required), language, force_whisper, allow_long
                         -> transcript_path, method, title, channel, duration_s, chars

  summarize_transcript   path (vault-relative, required), style, model, write_back, force
                         -> summary, summary_path, source_chars, chunks, regenerated
                         style is one of: brief, detailed, bullets

After enqueueing, poll job_status for about 20 seconds.

  Finished in time  -> report the result. For a summary, show the summary text itself.
  Still running     -> tell the user the job id and that you will not wait. Do not
                       pretend it finished, and never invent a result.

If the Mac is offline the job simply stays queued, and the worker runs it when the Mac
comes back. That is normal operation, not an error. Say "queued — it will run when the
Mac is next awake", not "failed".

A failed job comes back with an error. If it is marked permanent, do not re-enqueue the
same job: it will fail identically. Report the error and stop. Transient failures are
retried by the queue on their own; do not enqueue a duplicate.

Both skills are safe to run twice, so re-asking is cheap. In particular, asking to
summarize a note that already has a summary in that style returns the existing text in
milliseconds without re-running the model. Only pass force=true when the user explicitly
asks to redo or regenerate a summary. Asking for a different style is not a redo — pass
the style and let the skill decide.

To transcribe and then summarize, enqueue transcribe_video first, wait for it to finish,
then enqueue summarize_transcript with the transcript_path it returned. Do not construct
that path yourself.

Paths are confined to the vault. A request that points outside it will be rejected, which
is intended — do not try to work around it.
```

---

## 2. Tools to expose

Hermes has no queue tools at all today, which is why it correctly refused to act on the
prompt above. It registers MCP servers through its own `config.yml`, so that is where this
goes. The server is `queue_mcp.py`, which lives in the queue repo at `/home/dev/queue` —
it is a client of that service's HTTP API, so the two version together.

**1. Drop it in the repo and install the dependency.** Reuse the queue's existing venv so
the repo has one environment and one requirements file:

```bash
/home/dev/queue/.venv/bin/pip install mcp
```

Add `mcp` to the repo's `requirements.txt` while you're there. The rest of the server is
stdlib.

**2. Put the token OUTSIDE the repo.** This is the one thing that must not live in the
folder you're pushing to GitHub:

```bash
install -m 600 /dev/null ~/.hermes/credentials/queue_token
```

Then paste the token into that file. If the queue service already reads the token from an
env file, point at that instead of keeping a second copy — set `HERMES_CRED_DIR`, or export
`QUEUE_TOKEN` into Hermes' environment.

**3. Register it in `config.yml`.** Both paths must be absolute — YAML does not expand `~`:

```yaml
mcp_servers:
  fi_tools_mcp:
    command: "/home/dev/queue/.venv/bin/python"
    args: ["/home/dev/queue/queue_mcp.py"]
```

No `env:` key is needed. The server reads `QUEUE_URL` / `QUEUE_TOKEN` from the environment
when Hermes provides them and falls back to `~/.hermes/credentials/{queue_url,queue_token}`
otherwise, so it works whether or not that schema supports environment variables. If the
token is missing from both, every tool fails with a message naming the file to create,
rather than an opaque 401.

**Before pushing the repo**, confirm the token isn't in it:

```bash
cd /home/dev/queue && git grep -nI -e "$(cat ~/.hermes/credentials/queue_token)" || echo "clean"
```

And make sure `.gitignore` covers `.venv/`, `*.db` (the SQLite queue), `.env`, and
`*.token`.

The tool bodies below are the same logic in plain Python, for reference. Keep the surface
typed and narrow either way: an agent that can submit one of a few declared job types has a
far smaller blast radius than one holding a shell, and nothing is lost — the job types are
known in advance.

```python
QUEUE = "http://127.0.0.1:8080"      # Hermes runs on the same box as the queue
TOKEN = os.environ["QUEUE_TOKEN"]
HEAD  = {"Authorization": f"Bearer {TOKEN}"}


def enqueue_job(type: str, params: dict) -> dict:
    """Queue a job for the Mac worker. Returns {"id": ..., "status": "queued"}.

    type must be one of the declared job types; params must match its schema.
    """
    r = requests.post(f"{QUEUE}/jobs", json={"type": type, "params": params}, headers=HEAD)
    r.raise_for_status()
    return r.json()


def job_status(id: str) -> dict:
    """Current state of one job: status, result, error, attempts."""
    r = requests.get(f"{QUEUE}/jobs/{id}", headers=HEAD)
    r.raise_for_status()
    return r.json()


def list_jobs(status: str = "queued") -> list:
    """Jobs in a given state: queued, leased, done, failed, dead."""
    r = requests.get(f"{QUEUE}/jobs", params={"status": status}, headers=HEAD)
    r.raise_for_status()
    return r.json()
```

**Do not give Hermes a tool that runs shell commands on the Mac.** The whole point of the
typed job types is that it cannot.

### Letting Hermes discover skills

The worker already pushes its manifests to `POST /manifest` on startup (confirmed working
— it published 2 skills). If the queue stores them, expose a `list_skills()` tool reading
that back, and drop the hardcoded list from the prompt above. Then adding a skill on the
Mac makes it available to Hermes with no prompt edit — restart the worker and it
republishes.

---

## 2b. The catalog — what to build on the VPS

The Mac worker already pushes this; the queue side does not exist yet. Until it does, the
worker gets a 404, logs one line, and carries on — nothing breaks, search just does not
work.

**Check FTS5 first.** The whole design assumes it:

```bash
/home/dev/queue/.venv/bin/python3 -c "import sqlite3;sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE t USING fts5(a)');print('ok')"
```

Present on the Mac (sqlite 3.50.4, porter + bm25 + snippet). If the VPS build lacks it,
fall back to `LIKE` scanning — at a few hundred rows nothing is lost but ranking and
snippets.

### Schema — same SQLite file as `jobs`, no second datastore

```sql
CREATE TABLE IF NOT EXISTS notes (
  path              TEXT PRIMARY KEY,   -- vault-relative: transcripts/abc-title.md
  worker            TEXT,
  title             TEXT,
  channel           TEXT,
  video_id          TEXT,
  url               TEXT,
  upload_date       TEXT,
  duration_s        INTEGER,
  transcript_method TEXT,
  chars             INTEGER,            -- transcript length, so Fi can say how big it is
  summary           TEXT,
  summary_style     TEXT,
  has_summary       INTEGER NOT NULL DEFAULT 0,
  sha256            TEXT,               -- of the note file; lets pushes skip no-ops
  modified_at       TEXT,               -- note mtime; order "recent notes" by this
  updated_at        TEXT                -- when this row was last written
);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
  title, channel, summary,
  content='notes', content_rowid='rowid',
  tokenize='porter unicode61'
);
```

Add the three standard external-content sync triggers (`ai`/`ad`/`au` on `notes`).
Without them the index silently stops matching new rows — nothing errors, searches just
come back empty for anything added after you created the table.

Those column names are exactly the keys the worker sends. It never sends the transcript
body; there is no column for it because there is no field for it.

### `POST /catalog`

```json
{"worker": "mac", "full": true, "notes": [{"path": "...", "title": "...", ...}]}
```

- Upsert each row by `path`. Skip the write when `sha256` matches what you have.
- `full: true` means *this is the complete set for that worker*: delete rows for it that
  are not in the payload. That is the only way a note deleted in Obsidian leaves the index.
- `full: false` is a single-note update after a job — upsert only, never delete.
- Same bearer token as everything else.

### `GET /catalog/search?q=...&limit=5`

```sql
SELECT n.path, n.title, n.channel, n.url, n.upload_date, n.has_summary,
       snippet(notes_fts, 2, '**', '**', '…', 20) AS snip,
       bm25(notes_fts, 5.0, 2.0, 1.0)             AS score
FROM notes_fts JOIN notes n ON n.rowid = notes_fts.rowid
WHERE notes_fts MATCH ?
ORDER BY score LIMIT ?;
```

Weights: title 5, channel 2, summary 1. **bm25 returns negative numbers — smaller is
better.** Ascending `ORDER BY score` is correct; `DESC` silently returns the worst matches
and looks like the search is bad rather than backwards.

**Sanitise `q` before it reaches MATCH.** Raw user text containing `"`, `*`, `AND`, `NEAR`
or an unbalanced paren is an FTS5 *syntax error*, which reaches Fi as a 500:

```python
terms = [t.replace('"', "") for t in q.split() if t.strip()]
match = " OR ".join(f'"{t}"' for t in terms) or '""'
```

### `GET /catalog?limit=20` and `GET /catalog/note?path=...`

Recent notes ordered by `modified_at`, and one full row including the summary text.

### Three more MCP tools in `queue_mcp.py`

```python
def search_notes(query: str, limit: int = 5) -> list:
    """Search saved transcripts by topic. Returns ranked notes with snippets.

    Covers titles and summaries, not transcript text. Call this before
    enqueueing transcribe_video, to avoid re-transcribing what is already saved.
    """

def list_notes(limit: int = 20) -> list:
    """Most recently added or updated notes."""

def get_note(path: str) -> dict:
    """One note's metadata and its full summary."""
```

None of them touch the Mac, so they answer while it is asleep.

---

## 3. VPS checks

### Verify the queue is not on the public internet

**Do this first.** This is a Hetzner box with a public IP.

```bash
ss -ltnp | grep 8080
```

- `<tailnet-ip>:8080` — correct, tailnet only.
- `0.0.0.0:8080` or `*:8080` — **the queue is exposed to the internet**, protected only by
  the bearer token. Fix by binding to the Tailscale address:

  ```
  uvicorn queue:app --host <tailnet-ip> --port 8080
  ```

  and deny it at the firewall as well: `sudo ufw deny 8080`.

### Lease duration vs heartbeat

The Mac worker heartbeats every 30s. The lease must be comfortably longer, or a job that
is running fine gets requeued underneath it and you get duplicate work.

**Lease ≥ 120s.** Do not set it near 30s.

Separately, `transcribe_video` can legitimately run up to 1800s on the whisper path — the
heartbeat is what keeps that alive, so heartbeat handling must actually extend
`lease_until`, not just record a timestamp.

### Run the queue under systemd

If it is still a foreground `uvicorn` in a shell, it dies with the SSH session.

```ini
[Unit]
Description=Hermes job queue
After=network-online.target

[Service]
ExecStart=/path/to/venv/bin/uvicorn queue:app --host <tailnet-ip> --port 8080
EnvironmentFile=/etc/hermes/queue.env     # chmod 600, holds QUEUE_TOKEN
Restart=always
RestartSec=5
User=hermes

[Install]
WantedBy=multi-user.target
```

### SQLite

`PRAGMA journal_mode=WAL` — the long-poller and the writer will otherwise block each
other. Set it once; it persists on the database file.

### Dead jobs need to be visible

`attempts` hits its ceiling and the job goes to `dead`. Nothing surfaces that on its own.
Either have Hermes check `list_jobs("dead")` when asked about a job, or send an ntfy on
transition to dead. Silent dead-lettering is how a request disappears with no one noticing.

---

## 4. Test order

1. **From Telegram: summarize the note already in the vault.** Expect a result in about a
   second, with `regenerated: false` — proves enqueue → lease → run → complete without
   spending GPU.
2. **A short captioned video.** First real exercise of the launchd `PATH`, and where the
   Chrome-cookie problem shows up if it is going to.
3. **Enqueue while the Mac is asleep.** The job should sit in `queued`, and drain when the
   Mac wakes. This is the behaviour the whole design exists for.

4. **Ask about a note that already exists** — "do I have anything about pausing AI
   development?" Fi should call `search_notes` and name the note without enqueueing
   anything.
5. **Put the Mac to sleep and ask again.** Same answer, same latency. This is the whole
   reason the catalog exists.

Watch from the Mac side with:

```bash
tail -f ~/Library/Logs/hermes-worker.log
```

Once `/catalog` exists, the worker logs `published catalog (N note(s), full)` on startup.
Before it exists it logs `queue has no /catalog endpoint — skipping`, which is harmless.
