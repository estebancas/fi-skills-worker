#!/usr/bin/env python3
"""
~/skills-worker/worker.py

Polls the fi-hub job queue (a Cloudflare Worker), runs the matching skill
locally, and reports the result back. Runs under launchd; see
com.hermes.worker.plist.

The skill manifests in this directory are the allowlist: a job whose type has
no skill.yaml is rejected outright, so Hermes can never name a script that was
not declared.

Config (environment):
    QUEUE_URL            base URL of the queue, https://fi-hub.<account>.workers.dev/worker
    QUEUE_TOKEN          bearer token
    QUEUE_AUTH_HEADER    header carrying it            (default: Authorization)
    QUEUE_AUTH_SCHEME    prefix for the value          (default: Bearer)
    WORKER_NAME          identifies this worker        (default: mac)
    SKILLS_DIR           where the skills live         (default: this directory)
    POLL_INTERVAL        sleep after an empty poll     (default: 15)
    LEASE_SECONDS        lease asked for per poll      (default: 120)
    POLL_WAIT            sent as `wait`, which the queue ignores (default: 30)
    JOB_TIMEOUT          per-job ceiling, seconds      (default: 1800)
    HEARTBEAT_INTERVAL   seconds between heartbeats    (default: 30)
    PUBLISH_MANIFEST     push skills to the queue      (default: auto)
    VAULT_DIR            notes to push                 (default: ~/vault)
    PUBLISH_CATALOG      push notes to fi-hub          (default: auto)
    CATALOG_RECONCILE_S  seconds between full pushes   (default: 3600)

Python's venv has no root certificates on this Mac, so HTTPS needs
SSL_CERT_FILE (the plist sets it to /etc/ssl/cert.pem).

Usage:
    worker.py            run the loop
    worker.py --check    validate config and skills, touch no network
    worker.py --probe    GET /healthz with and without the token, and validate
                         the local manifests. Never makes a call the queue
                         counts as "the Mac was seen".
    worker.py --once     handle a single job, then exit
"""

import argparse
import hashlib
import json
import os
import re
import signal
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

from skilllib import notefmt

HERE = Path(__file__).resolve().parent

# Cloudflare's edge rejects the default `Python-urllib/x.y` User-Agent with a
# 403 "Error 1010" before the request ever reaches fi-hub, so the token is not
# even looked at. Every request names itself.
USER_AGENT = "fi-skills-worker/1.0"

QUEUE_URL = os.environ.get("QUEUE_URL", "").rstrip("/")
QUEUE_TOKEN = os.environ.get("QUEUE_TOKEN", "")
AUTH_HEADER = os.environ.get("QUEUE_AUTH_HEADER", "Authorization")
AUTH_SCHEME = os.environ.get("QUEUE_AUTH_SCHEME", "Bearer")
WORKER_NAME = os.environ.get("WORKER_NAME", "mac")
SKILLS_DIR = Path(os.environ.get("SKILLS_DIR", HERE)).expanduser()
POLL_WAIT = int(os.environ.get("POLL_WAIT", "30"))
# The queue is a short poll: it answers an empty queue at once. Without a sleep
# on 204 this loop would spin and burn the Free plan's daily request budget.
POLL_INTERVAL = max(1.0, float(os.environ.get("POLL_INTERVAL", "15")))
LEASE_SECONDS = int(os.environ.get("LEASE_SECONDS", "120"))
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT", "1800"))
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "30"))
PUBLISH_MANIFEST = os.environ.get("PUBLISH_MANIFEST", "auto").lower()
VAULT = Path(os.environ.get("VAULT_DIR", Path.home() / "vault")).expanduser()
PUBLISH_CATALOG = os.environ.get("PUBLISH_CATALOG", "auto").lower()
CATALOG_RECONCILE_S = int(os.environ.get("CATALOG_RECONCILE_S", "3600"))

OFF = ("0", "no", "false", "off")
CAFFEINATE = "/usr/bin/caffeinate"
BACKOFF_MAX = 60

stop = threading.Event()


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}", flush=True)


# ---------------------------------------------------------------------------
# skills
# ---------------------------------------------------------------------------

def load_skills(root: Path) -> dict[str, dict]:
    """Every */skill.yaml under root, keyed by its declared type."""
    skills: dict[str, dict] = {}
    for manifest in sorted(root.glob("*/skill.yaml")):
        try:
            data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            log(f"WARNING: {manifest} is not valid YAML, skipping: {exc}")
            continue
        job_type = data.get("type")
        if not job_type:
            log(f"WARNING: {manifest} declares no type, skipping")
            continue
        if job_type != manifest.parent.name:
            log(f"WARNING: {manifest} declares type {job_type!r} but sits in "
                f"{manifest.parent.name!r} — registering as {job_type!r}")
        skills[job_type] = {
            "dir": manifest.parent,
            "run": data.get("run") or ["/usr/bin/env", "python3", "run.py"],
            "manifest": data,
        }
    return skills


def resolve_run(run: list[str]) -> list[str]:
    """Pin bare `python3` to the interpreter running this worker.

    launchd does not hand us a login shell, so `env python3` resolves against
    whatever PATH the plist sets. Using our own interpreter removes a whole
    class of "works in my terminal" failures.
    """
    if run[:2] == ["/usr/bin/env", "python3"]:
        return [sys.executable] + run[2:]
    if run[:1] == ["python3"]:
        return [sys.executable] + run[1:]
    return list(run)


# ---------------------------------------------------------------------------
# queue
# ---------------------------------------------------------------------------

class QueueError(Exception):
    """Queue unreachable or misbehaving. Always transient — back off and retry.

    `status` is the HTTP status when the queue answered, None when it did not.
    """

    status: int | None = None


class TLSError(QueueError):
    """The TLS handshake failed. Not an outage: usually missing root certificates."""


class AuthError(QueueError):
    """HTTP 401. The queue is up and refused our token."""


class EdgeBlocked(QueueError):
    """HTTP 403 from Cloudflare's edge (error 1xxx), not from fi-hub.

    fi-hub answers 401 for a bad token, never 403. A 403 with a Cloudflare error
    body means a rule at the edge refused the client before fi-hub saw it:
    usually the User-Agent. Not an outage, and a different fix from a bad token.
    """


class LeaseLost(QueueError):
    """HTTP 409. The job is no longer leased to us: it expired and was requeued."""


# What Cloudflare's own error pages say: "error code: 1010" as plain text, or a
# JSON problem document naming "error-1010" / "Error 1010: Access denied".
EDGE_ERROR = re.compile(r"error code: 1\d{3}|cloudflare-1xxx-errors/error-1\d{3}|Error 1\d{3}:")


def call(method: str, path: str, body=None, query=None, timeout=30, auth=True):
    url = QUEUE_URL + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if data:
        headers["Content-Type"] = "application/json"
    if QUEUE_TOKEN and auth:
        headers[AUTH_HEADER] = f"{AUTH_SCHEME} {QUEUE_TOKEN}".strip()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.status == 204 or not raw.strip():
                return resp.status, None
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                raise QueueError(f"{method} {path} returned non-JSON: {raw[:200]!r}")
    except urllib.error.HTTPError as exc:
        full = exc.read().decode(errors="replace")
        detail = full[:300]
        edge = EDGE_ERROR.search(full) if exc.code == 403 else None
        if edge:
            err: QueueError = EdgeBlocked(
                f"{method} {path} -> HTTP 403 blocked at the Cloudflare edge "
                f"({edge.group(0)}); fi-hub never saw it. {detail[:120]}")
        else:
            err = {401: AuthError, 409: LeaseLost}.get(exc.code, QueueError)(
                f"{method} {path} -> HTTP {exc.code}: {detail}")
        err.status = exc.code
        raise err from exc
    except urllib.error.URLError as exc:
        # urlopen wraps a failed handshake in URLError, so look at the reason.
        if isinstance(exc.reason, ssl.SSLError):
            raise TLSError(_tls_message(method, path, exc.reason)) from exc
        raise QueueError(f"{method} {path} unreachable: {exc}") from exc
    except ssl.SSLError as exc:          # before OSError: SSLError is one
        raise TLSError(_tls_message(method, path, exc)) from exc
    except (TimeoutError, OSError) as exc:
        raise QueueError(f"{method} {path} unreachable: {exc}") from exc


def _tls_message(method: str, path: str, exc: BaseException) -> str:
    return (f"{method} {path} TLS failure: {exc} "
            f"(SSL_CERT_FILE={os.environ.get('SSL_CERT_FILE') or 'unset'})")


def lease_next():
    """Ask for a job. A short poll: an empty queue is answered at once with 204.

    `wait` is still sent, harmlessly: the queue ignores it. The caller sleeps
    POLL_INTERVAL when this returns None.
    """
    _, body = call(
        "GET", "/jobs/next",
        query={"worker": WORKER_NAME, "wait": POLL_WAIT, "lease": LEASE_SECONDS},
        timeout=POLL_WAIT + 15,
    )
    if not body:
        return None
    job = body.get("job", body) if isinstance(body, dict) else None
    if not job or not job.get("id"):
        return None
    return job


def heartbeat(job_id: str) -> None:
    """Extend the lease. The worker name goes in the JSON body."""
    call("POST", f"/jobs/{job_id}/heartbeat", body={"worker": WORKER_NAME},
         query={"lease": LEASE_SECONDS}, timeout=15)


def complete(job_id: str, *, result=None, error=None, permanent=False) -> bool:
    """Report a job. False means the queue did not record it (409: lease lost).

    A lost lease is not fatal: the job was requeued and will run again, which
    is safe because every skill is. The work this attempt did is still on disk.
    """
    try:
        call("POST", f"/jobs/{job_id}/complete", body={
            "worker": WORKER_NAME,
            "status": "done" if error is None else "failed",
            "result": result,
            "error": error,
            "permanent": permanent,
        }, timeout=30)
    except LeaseLost as exc:
        log(f"job {job_id}: lease lost, result not recorded (job was requeued): {exc}")
        return False
    return True


def publish_manifest(skills: dict[str, dict]) -> bool:
    """Tell the queue what this worker can do.

    Returns True when there is nothing left to retry: it was published, or it is
    switched off, or the queue has no such endpoint. False means it did not
    land and the caller should try again — until it does, fi-hub's
    enqueue_job refuses ("no skill manifest"), so one failed attempt at a
    launch with no network must not last until the next restart.
    """
    if PUBLISH_MANIFEST in OFF:
        return True
    payload = {"worker": WORKER_NAME, "skills": [s["manifest"] for s in skills.values()]}
    try:
        call("POST", "/manifest", body=payload, timeout=15)
        log(f"published manifest ({len(skills)} skills)")
        return True
    except QueueError as exc:
        if exc.status in (404, 405):
            log("queue has no /manifest endpoint — skipping (set PUBLISH_MANIFEST=0 to silence)")
            return True
        log(f"manifest publish failed, will retry: {exc}")
        return False


# ---------------------------------------------------------------------------
# notes
#
# The vault lives here and fi-hub lives on Cloudflare, so the worker pushes
# every note's FULL TEXT to it: fi-hub stores it, indexes it, and serves
# search_notes / get_note while this machine sleeps. That reverses the old
# catalog's rule that the transcript body never leaves — Fi now quotes from
# the body, so the body is the point. What did not change: this module only
# ever *reads* notes, never writes one, and reads nothing outside the vault.
#
# The note format itself lives in skilllib.notefmt, shared with the skills.
# ---------------------------------------------------------------------------

# Until a push response says otherwise (see apply_limits). These match what
# fi-hub reports today: 200,000 bytes and 10 notes recommended, 1,000,000
# bytes and 40 notes as hard caps.
LIMITS = {"bytes": 200_000, "notes": 10, "max_bytes": 1_000_000, "max_notes": 40}
NOTES_TIMEOUT = 90            # each upload takes ~1s and the hub does four at a time
RECONCILE_MAX_CALLS = 100     # 20 deletions a call, so 2000 notes; a runaway guard
NOTES_RETRY_S = 300           # retry a failed full push sooner than the hourly timer
MANIFEST_RETRY_S = 60         # between attempts while the manifest is unpublished


def split_front(text: str) -> tuple[dict | None, str]:
    """Return (parsed frontmatter, body). Frontmatter is None if not a note.

    notefmt.split_frontmatter returns the raw block, because the skills are
    stdlib-only and have no YAML parser. The worker does, so it parses.
    """
    front, body = notefmt.split_frontmatter(text)
    if not front:
        return None, text
    try:
        data = yaml.safe_load(front.strip().strip("-"))
    except yaml.YAMLError:
        return None, text
    if not isinstance(data, dict):
        return None, text
    return data, body.lstrip("\n")


def parse_summary(block: str) -> tuple[str | None, str]:
    """Return (style, summary text), tolerating blocks with no style stamp.

    notefmt.parse_block gives up without the stamp because a skill needs to
    know whether to regenerate. The index would rather carry unstamped prose
    than nothing, so it falls back to stripping the markers.
    """
    if not block:
        return None, ""
    style, text = notefmt.parse_block(block)
    if text:
        return style, text
    for marker in (notefmt.SUM_BEGIN, notefmt.SUM_END, "## Summary"):
        block = block.replace(marker, "")
    return None, block.strip()


def _text(value) -> str | None:
    """YAML gives dates back as date objects and numbers as ints."""
    return None if value is None else str(value)


def _iso_date(value) -> str:
    """The YYYY-MM-DD part of a YAML date, datetime or string; "" if none."""
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():          # 20261002
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text[:10]


def load_note(path: Path):
    """Read one vault note: (relative path, raw bytes, text, frontmatter, body).

    None if it is not an indexable note. The path is resolved and confined to
    the vault BEFORE anything is read: a symlink in the vault pointing at
    ~/.ssh would otherwise be read here, and since the full text now goes to
    the cloud, read means published.
    """
    try:
        resolved = path.resolve()
        rel = str(resolved.relative_to(VAULT.resolve()))
    except (ValueError, OSError):
        return None
    try:
        raw = resolved.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        log(f"notes: skipping {path.name}: {exc}")
        return None
    front, body = split_front(text)
    if front is None:
        return None
    return rel, raw, text, front, body


def note_row(path: Path) -> dict | None:
    """Extracted facts about a note (title, summary, style, ...), or None.

    This is what --check prints and what the summary tests pin. It is not what
    is pushed; that is note_push_row.
    """
    loaded = load_note(path)
    if loaded is None:
        return None
    rel, raw, _, front, body = loaded

    transcript, block = notefmt.split_summary(body)
    style, summary = parse_summary(block)
    duration = front.get("duration_s")
    # Articles carry site/url_id/published where videos carry
    # channel/video_id/upload_date. One shape for either.
    return {
        "path": rel,
        "title": str(front.get("title") or path.stem),
        "channel": _text(front.get("channel") or front.get("site")),
        "video_id": _text(front.get("video_id") or front.get("url_id")),
        "url": _text(front.get("url")),
        "upload_date": _text(front.get("upload_date") or front.get("published")),
        "duration_s": duration if isinstance(duration, int) else None,
        "transcript_method": _text(
            front.get("transcript_method") or front.get("fetch_method")
        ),
        "chars": len(transcript),
        "summary": summary,
        "summary_style": style,
        "has_summary": bool(summary),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "modified_at": datetime.fromtimestamp(
            path.resolve().stat().st_mtime, timezone.utc
        ).isoformat(timespec="seconds"),
    }


def note_push_row(path: Path) -> dict | None:
    """The seven fields fi-hub's POST /notes takes, or None if not a note.

    `kind` comes from the RAW frontmatter keys. note_row folds url_id into
    video_id and fetch_method into transcript_method so that one shape serves
    both kinds of note; deriving kind from that folded row would call every
    article a video.

    `date` is the upload date for videos and the fetch date for articles, not
    the article's own publication date. The notes already in the index were
    pushed that way, so a change here would re-date them.
    """
    loaded = load_note(path)
    if loaded is None:
        return None
    rel, raw, text, front, _ = loaded
    is_video = bool(front.get("video_id") or front.get("transcript_method"))
    return {
        "path": rel,
        "markdown": text,
        "title": str(front.get("title") or path.stem),
        "url": _text(front.get("url")) or "",
        "kind": "video" if is_video else "article",
        "date": _iso_date(front.get("upload_date" if is_video else "fetched_at")),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def scan_vault(row_fn=note_row) -> list[dict]:
    """Every indexable note under VAULT, in a stable order, one row per file."""
    if not VAULT.is_dir():
        return []
    rows: dict[str, dict] = {}
    skipped = 0
    for path in sorted(VAULT.rglob("*.md")):
        row = row_fn(path)
        if row is None:
            skipped += 1
            continue
        rows[row["path"]] = row         # two links to one file are one note
    if skipped:
        log(f"notes: {skipped} file(s) had no usable frontmatter, not indexed")
    return list(rows.values())


def _body_size(rows: list[dict]) -> int:
    """Exact bytes of the request body for these rows, as call() will encode it."""
    return len(json.dumps({"worker": WORKER_NAME, "notes": rows}).encode())


def take_batch(rows: list[dict]) -> list[dict]:
    """Remove and return the next batch from the front of `rows`.

    Packs in order until the next row would pass the byte or note limit. A row
    over the byte limit on its own goes alone: the limit is a recommendation,
    and the hard cap is higher. Read the limits at call time, so what a
    response says applies to the very next batch.
    """
    cur: list[dict] = []
    while rows:
        if cur and (len(cur) >= LIMITS["notes"]
                    or _body_size(cur + [rows[0]]) > LIMITS["bytes"]):
            break
        cur.append(rows.pop(0))
    return cur


def batch_notes(rows: list[dict]) -> list[list[dict]]:
    """All the batches `rows` would be sent in, at the current limits."""
    rest, batches = list(rows), []
    while rest:
        batches.append(take_batch(rest))
    return batches


def apply_limits(resp) -> None:
    """Adopt the limits the server reports, never above its own hard caps."""
    lim = resp.get("limits") if isinstance(resp, dict) else None
    if not isinstance(lim, dict):
        return

    def num(key):
        v = lim.get(key)
        return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None

    if (v := num("max_request_bytes")):
        LIMITS["max_bytes"] = v
    if (v := num("max_batch_notes")):
        LIMITS["max_notes"] = v
    if (v := num("recommended_batch_bytes")):
        LIMITS["bytes"] = min(v, LIMITS["max_bytes"])
    if (v := num("recommended_batch_notes")):
        LIMITS["notes"] = min(v, LIMITS["max_notes"])


class PushOutcome:
    """What a full push did, so the caller knows whether reconcile is safe."""

    def __init__(self):
        self.ok = True                  # every batch landed (a 413 skip is not a failure)
        self.skipped: set[str] = set()  # paths fi-hub refused as too large
        self.sent = 0


def _push_batch(batch: list[dict], out: PushOutcome) -> bool:
    """POST one batch. False means stop: the queue itself is not answering."""
    if len(batch) == 1 and _body_size(batch) > LIMITS["max_bytes"]:
        _skip_too_large(batch[0], out, "over the request limit before sending")
        return True
    try:
        _, resp = call("POST", "/notes",
                       body={"worker": WORKER_NAME, "notes": batch},
                       timeout=NOTES_TIMEOUT)
    except AuthError:
        raise
    except QueueError as exc:
        if exc.status == 413:
            if len(batch) > 1:          # a limit we did not know about: go smaller
                LIMITS["notes"] = max(1, len(batch) // 2)
                LIMITS["bytes"] = max(1, _body_size(batch) // 2)
                return all(_push_batch(b, out) for b in batch_notes(batch))
            _skip_too_large(batch[0], out, str(exc))
            return True
        out.ok = False
        log(f"notes: push of {len(batch)} note(s) failed: {exc}")
        return exc.status is not None   # the queue answered: later batches may work
    apply_limits(resp)
    out.sent += len(batch)
    if isinstance(resp, dict):
        log(f"notes: pushed {len(batch)} (inserted {resp.get('inserted', '?')}, "
            f"updated {resp.get('updated', '?')}, unchanged {resp.get('unchanged', '?')})")
        if resp.get("index_failed"):
            # Stored, but the index upload failed. Pushing the same note again
            # retries it, so the next full push does; nothing to do here.
            log(f"notes: {resp['index_failed']} stored but not indexed yet "
                f"(retried on the next push): {resp.get('index_errors')}")
    return True


def _skip_too_large(row: dict, out: PushOutcome, why: str) -> None:
    out.skipped.add(row["path"])
    log(f"notes: SKIPPING {row['path']} ({len(row['markdown'])} chars): too large "
        f"for fi-hub, not retrying: {why}")


def push_notes(rows: list[dict]) -> PushOutcome:
    out = PushOutcome()
    rest = list(rows)
    while rest:
        batch = take_batch(rest)
        if stop.is_set():
            out.ok = False
            break
        try:
            if not _push_batch(batch, out):
                out.ok = False
                break
        except QueueError as exc:       # auth, or any other failure that ends the push
            out.ok = False
            log(f"notes: push stopped: {exc}")
            break
    return out


def reconcile_notes(rows: list[dict], out: PushOutcome) -> set[str]:
    """Tell fi-hub what the vault holds so it can drop notes deleted here.

    Only ever called after a push in which every batch landed, so a crash or a
    dropped connection mid-push deletes nothing. Sends paths and hashes only,
    repeating until pending_deletes is 0 (the hub deletes 20 a call). Returns
    the paths the hub says it lacks or holds a different version of.
    """
    keep = [{"path": r["path"], "sha256": r["sha256"]} for r in rows]
    if not keep:                        # fi-hub refuses it, and it means "vault gone"
        return set()
    want: set[str] = set()
    last_pending = None
    for _ in range(RECONCILE_MAX_CALLS):
        try:
            _, resp = call("POST", "/notes/reconcile",
                           body={"worker": WORKER_NAME, "keep": keep},
                           timeout=NOTES_TIMEOUT)
        except QueueError as exc:
            log(f"notes: reconcile failed, will retry next cycle: {exc}")
            out.ok = False
            break
        if not isinstance(resp, dict):
            break
        want.update(p for p in (resp.get("missing") or []) + (resp.get("stale") or [])
                    if isinstance(p, str))
        pending = resp.get("pending_deletes") or 0
        log(f"notes: reconciled (deleted {resp.get('deleted', 0)}, "
            f"pending {pending}, failed {resp.get('delete_failed', 0)})")
        if resp.get("delete_failed"):
            log(f"notes: delete errors: {resp.get('delete_errors')}")
        if not pending:
            break
        if pending == last_pending and not resp.get("deleted"):
            log("notes: reconcile is making no progress, giving up until next cycle")
            break
        last_pending = pending
    return want - out.skipped


def publish_notes(rows: list[dict], *, full: bool) -> bool:
    """Push notes to fi-hub. True when nothing is left owing.

    full=True means "this is the whole vault": after every batch lands, send
    /notes/reconcile, and push once more whatever it reports missing or stale.
    One pass only; a second reconcile is the next cycle's job.
    """
    if PUBLISH_CATALOG in OFF:
        return True
    if not rows:
        if full:
            log("notes: no notes in the vault, nothing to push or reconcile")
        return True
    out = push_notes(rows)
    if full and out.ok:
        owed = reconcile_notes(rows, out)
        if owed:
            by_path = {r["path"]: r for r in rows}
            again = [by_path[p] for p in sorted(owed) if p in by_path]
            if again:
                log(f"notes: pushing {len(again)} note(s) fi-hub reported "
                    f"missing or stale")
                follow = push_notes(again)
                out.ok = out.ok and follow.ok
    return out.ok


def notes_after(result) -> None:
    """Push the single note a finished job touched.

    Called after the job has already been reported, and swallows everything:
    a notes push must never turn a job that succeeded into one that failed.
    """
    if PUBLISH_CATALOG in OFF or not isinstance(result, dict):
        return
    rel = (result.get("transcript_path") or result.get("summary_path")
           or result.get("source_path") or result.get("note_path"))
    if not isinstance(rel, str) or not rel:
        return
    try:
        path = Path(rel)
        path = (path if path.is_absolute() else VAULT / path).resolve()
        if not path.is_relative_to(VAULT.resolve()):
            return
        row = note_push_row(path)
        if row is not None:
            publish_notes([row], full=False)
    except Exception as exc:
        log(f"notes: post-job push skipped: {exc!r}")


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------

class Heartbeater:
    """Keeps the lease alive while a job runs, so a slow job is not requeued."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self.done.wait(HEARTBEAT_INTERVAL):
            try:
                heartbeat(self.job_id)
            except LeaseLost as exc:
                # Nothing a further heartbeat can fix: the queue reissued it.
                log(f"job {self.job_id}: lease lost, no more heartbeats "
                    f"(job keeps running): {exc}")
                return
            except QueueError as exc:
                log(f"job {self.job_id}: heartbeat failed (job keeps running): {exc}")

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.done.set()


def run_skill(skill: dict, params: dict) -> tuple[int, str, str]:
    """Run one skill. Returns (exit code, stdout, stderr).

    caffeinate wraps the job rather than the worker: this is a laptop, and
    keeping it awake for the whole uptime of the worker would be wrong. Keeping
    it awake exactly while a transcription runs is what we actually want.
    """
    cmd = resolve_run(skill["run"])
    if Path(CAFFEINATE).exists():
        cmd = [CAFFEINATE, "-dims"] + cmd

    proc = subprocess.Popen(
        cmd,
        cwd=skill["dir"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "JOB_TIMEOUT": str(JOB_TIMEOUT)},
    )
    try:
        out, err = proc.communicate(json.dumps(params), timeout=JOB_TIMEOUT)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        return 1, out, (err or "") + f"\nworker: killed after JOB_TIMEOUT={JOB_TIMEOUT}s"


def parse_result(stdout: str):
    """The contract is a JSON object on the last line of stdout."""
    for line in reversed(stdout.strip().splitlines()):
        if line.strip():
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                return None
    return None


def tail(text: str, lines: int = 12, limit: int = 1500) -> str:
    return "\n".join((text or "").strip().splitlines()[-lines:])[-limit:]


def handle(job: dict, skills: dict[str, dict]) -> None:
    job_id = job["id"]
    job_type = job.get("type") or ""
    params = job.get("params") or {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except json.JSONDecodeError:
            complete(job_id, error=f"params is not valid JSON: {params[:200]}", permanent=True)
            return

    skill = skills.get(job_type)
    if skill is None:
        log(f"job {job_id}: unknown type {job_type!r} — rejecting")
        complete(job_id, error=f"no skill declares type {job_type!r}", permanent=True)
        return

    log(f"job {job_id}: {job_type} {json.dumps(params)[:200]}")
    started = time.time()
    with Heartbeater(job_id):
        code, out, err = run_skill(skill, params)
    took = time.time() - started

    if code == 0:
        result = parse_result(out)
        if result is None:
            log(f"job {job_id}: exit 0 but no JSON on the last stdout line — failing transient")
            complete(job_id, error=f"skill produced no JSON result. stderr:\n{tail(err)}")
            return
        log(f"job {job_id}: done in {took:.0f}s")
        complete(job_id, result=result)
        notes_after(result)
        return

    permanent = code == 2
    log(f"job {job_id}: exit {code} after {took:.0f}s ({'permanent' if permanent else 'transient'})")
    complete(job_id, error=tail(err) or f"exit {code} with no stderr", permanent=permanent)


# ---------------------------------------------------------------------------

def check(skills: dict[str, dict]) -> int:
    problems = []   # block startup
    notes = []      # worth saying, but fine
    print(f"queue      {QUEUE_URL or '(unset)'}")
    print(f"token      {'set' if QUEUE_TOKEN else '(unset)'} via {AUTH_HEADER}: {AUTH_SCHEME} ...")
    print(f"worker     {WORKER_NAME}")
    print(f"skills dir {SKILLS_DIR}")
    print(f"vault      {VAULT}")
    print(f"python     {sys.executable}")
    print(f"timeouts   poll every {POLL_INTERVAL:g}s, lease {LEASE_SECONDS}s, "
          f"job {JOB_TIMEOUT}s, heartbeat {HEARTBEAT_INTERVAL}s")
    print(f"notes      {'off' if PUBLISH_CATALOG in OFF else f'on, full push every {CATALOG_RECONCILE_S}s'}")
    print()

    if not QUEUE_URL:
        problems.append("QUEUE_URL is unset")
    if not QUEUE_TOKEN:
        problems.append("QUEUE_TOKEN is unset")
    if not skills:
        problems.append(f"no skills found under {SKILLS_DIR}")

    # Not a problem: run_skill only wraps in caffeinate when it exists, and a
    # machine without it (any non-macOS host, CI) runs jobs perfectly well —
    # it just won't hold off sleep. Worth saying, not worth refusing to start.
    if not Path(CAFFEINATE).exists():
        notes.append(f"{CAFFEINATE} not found — jobs will not hold off sleep")

    for job_type, skill in sorted(skills.items()):
        cmd = resolve_run(skill["run"])
        script = skill["dir"] / cmd[-1]
        ok = script.is_file()
        print(f"  {job_type:<22} {'ok ' if ok else 'MISSING'} {script}")
        if not ok:
            problems.append(f"{job_type}: {script} does not exist")

    if PUBLISH_CATALOG not in OFF:
        print()
        if not VAULT.is_dir():
            notes.append(f"{VAULT} does not exist — there is nothing to push")
        rows = scan_vault()
        print(f"  notes: {len(rows)} note(s)")
        for r in rows:
            state = f"summary:{r['summary_style'] or 'unstamped'}" if r["has_summary"] else "no summary"
            print(f"    {r['path']:<50} {r['chars']:>7} chars  {state}")

    print()
    for n in notes:
        print(f"NOTE: {n}")
    for p in problems:
        print(f"PROBLEM: {p}")
    print("check passed" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


PARAM_TYPES = {"string", "integer", "number", "boolean", "array", "object"}


def validate_manifest(manifest: dict) -> list[str]:
    """Problems that would make fi-hub refuse or mis-validate this skill.

    fi-hub's enqueue_job accepts only parameters a skill declares, with the
    declared type and enum, so an incomplete manifest makes a working skill
    unusable from Claude. Offline: nothing here touches the network.
    """
    name = manifest.get("type") or "(no type)"
    out: list[str] = []
    if not isinstance(manifest.get("summary"), str) or not manifest["summary"].strip():
        out.append(f"{name}: no summary")
    params = manifest.get("params")
    if not isinstance(params, dict) or params.get("type") != "object":
        out.append(f"{name}: params must be an object schema (type: object)")
        params = {}
    props = params.get("properties") or {}
    if not isinstance(props, dict):
        out.append(f"{name}: params.properties must be a mapping")
        props = {}
    for pname, spec in props.items():
        ptype = spec.get("type") if isinstance(spec, dict) else None
        if ptype not in PARAM_TYPES:
            out.append(f"{name}: param {pname!r} has type {ptype!r}, "
                       f"allowed: {', '.join(sorted(PARAM_TYPES))}")
        enum = spec.get("enum") if isinstance(spec, dict) else None
        if enum is not None and (not isinstance(enum, list) or not enum):
            out.append(f"{name}: param {pname!r} has an empty or non-list enum")
    required = params.get("required") or []
    if not isinstance(required, list):
        out.append(f"{name}: params.required must be a list")
        required = []
    for r in required:
        if r not in props:
            out.append(f"{name}: required param {r!r} is not declared in properties")
    dur = manifest.get("typical_duration_s")
    if not isinstance(dur, (int, float)) or isinstance(dur, bool) or dur <= 0:
        out.append(f"{name}: typical_duration_s missing (Fi quotes it against a job's age)")
    if not isinstance(manifest.get("produces"), list):
        out.append(f"{name}: produces must be a list")
    if not isinstance(manifest.get("idempotent"), bool):
        out.append(f"{name}: idempotent must be true or false")
    return out


def probe(skills: dict[str, dict]) -> int:
    """Prove the queue is reachable over HTTPS and accepts our token.

    Calls only GET /healthz, which the queue does not count as "the Mac was
    seen". Polling, a heartbeat, a completion and a manifest publish all do,
    and a probe that made one would make a dead worker look alive. The token is
    never printed.
    """
    print(f"queue      {QUEUE_URL or '(unset)'}")
    print(f"token      {'set' if QUEUE_TOKEN else '(unset)'}")
    cert = os.environ.get("SSL_CERT_FILE")
    print(f"tls        SSL_CERT_FILE={cert or '(unset)'}"
          + (f" ({'exists' if Path(cert).is_file() else 'MISSING'})" if cert else ""))
    print()
    failed = False

    def fail(msg: str) -> None:
        nonlocal failed
        failed = True
        print(f"FAIL  {msg}")

    if not QUEUE_URL:
        fail("QUEUE_URL is unset")
        return 1
    if not QUEUE_URL.startswith("https://"):
        print("NOTE  QUEUE_URL is not https")

    try:
        status, body = call("GET", "/healthz", timeout=15)
        ok = isinstance(body, dict) and body.get("ok") is True
        print(f"ok    reachable, TLS ok, token accepted: GET /healthz -> {status}"
              + ("" if ok else " (but body.ok is not true)"))
        failed |= not ok
    except TLSError as exc:
        fail(f"TLS: {exc}")
    except EdgeBlocked as exc:
        print("ok    TLS ok")
        fail(f"blocked at the Cloudflare edge (User-Agent?): {exc}")
    except AuthError:
        print("ok    reachable, TLS ok")
        fail("auth: the queue answered 401 to our token (wrong or rotated QUEUE_TOKEN)")
    except QueueError as exc:
        fail(f"reachability: {exc}")

    try:
        call("GET", "/healthz", timeout=15, auth=False)
        print("NOTE  /healthz answered 200 without a token (expected 401)")
    except AuthError:
        print("ok    the queue refuses a request with no token (401)")
    except EdgeBlocked as exc:
        fail(f"blocked at the Cloudflare edge (User-Agent?) (no-token request): {exc}")
    except TLSError as exc:
        fail(f"TLS (no-token request): {exc}")
    except QueueError as exc:
        fail(f"reachability (no-token request): {exc}")

    print()
    problems = [p for s in skills.values() for p in validate_manifest(s["manifest"])]
    if not skills:
        problems.append(f"no skills found under {SKILLS_DIR}")
    for p in problems:
        fail(f"manifest: {p}")
    if not problems:
        print(f"ok    {len(skills)} local manifest(s) valid: {', '.join(sorted(skills))}")

    print()
    print("probe failed" if failed else "probe passed")
    return 1 if failed else 0


def schedule_full(ok: bool) -> float:
    """When the next full push is due: the hourly timer, or sooner after a failure.

    A launch with no network fails its first push; waiting an hour would leave
    fi-hub without the vault that long.
    """
    if not ok:
        return time.time() + NOTES_RETRY_S
    if CATALOG_RECONCILE_S > 0:
        return time.time() + CATALOG_RECONCILE_S
    return float("inf")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="validate config, touch no network")
    ap.add_argument("--probe", action="store_true",
                    help="GET /healthz over HTTPS (with and without the token) and "
                         "validate local manifests; never counts as 'Mac seen'")
    ap.add_argument("--once", action="store_true", help="handle one job, then exit")
    args = ap.parse_args()

    skills = load_skills(SKILLS_DIR)

    if args.check:
        return check(skills)
    if args.probe:
        return probe(skills)

    if not QUEUE_URL:
        log("ERROR: QUEUE_URL is unset")
        return 2
    if not skills:
        log(f"ERROR: no skills found under {SKILLS_DIR}")
        return 2

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    log(f"worker {WORKER_NAME} starting — queue {QUEUE_URL}, "
        f"skills: {', '.join(sorted(skills))}")
    manifest_ok = publish_manifest(skills)
    manifest_retry_at = time.time() + MANIFEST_RETRY_S
    next_full = schedule_full(publish_notes(scan_vault(note_push_row), full=True))

    backoff = 1
    while not stop.is_set():
        # A note edited directly in Obsidian is reported by no job, so the
        # vault is re-sent whole on a timer, then reconciled. The manifest goes
        # with it, so a hub that lost it (or a first publish that failed) heals.
        if time.time() >= next_full:
            manifest_ok = publish_manifest(skills)
            manifest_retry_at = time.time() + MANIFEST_RETRY_S
            next_full = schedule_full(publish_notes(scan_vault(note_push_row), full=True))

        try:
            job = lease_next()
            backoff = 1
        except QueueError as exc:
            # The laptop is off the network often. That is normal, not an
            # error. But a refused token and a failed handshake are not
            # outages, and calling them one hides the real cause.
            if isinstance(exc, EdgeBlocked):
                what = "blocked at the Cloudflare edge (User-Agent?), not an outage"
            elif isinstance(exc, AuthError):
                what = "queue refused the token (check QUEUE_TOKEN)"
            elif isinstance(exc, TLSError):
                what = "TLS failure (not an outage)"
            else:
                what = "queue unavailable"
            log(f"{what}, retrying in {backoff}s: {exc}")
            stop.wait(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
            continue

        # The queue just answered, so this is the moment a manifest that failed
        # to publish (launched with no network) can go through. Throttled: a
        # hub that is up but rejecting it must not be asked every poll.
        if not manifest_ok and time.time() >= manifest_retry_at:
            manifest_ok = publish_manifest(skills)
            manifest_retry_at = time.time() + MANIFEST_RETRY_S

        if job is None:
            # 204: the queue answered at once. Sleep, or this loop spins.
            stop.wait(POLL_INTERVAL)
            continue

        try:
            handle(job, skills)
        except QueueError as exc:
            # Reporting failed, not the job. Let the lease lapse and requeue.
            log(f"could not report job {job.get('id')}: {exc}")
        except Exception as exc:
            log(f"worker error on job {job.get('id')}: {exc!r}")
            try:
                complete(job["id"], error=f"worker error: {exc!r}")
            except QueueError:
                pass

        if args.once:
            break

    log("worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
