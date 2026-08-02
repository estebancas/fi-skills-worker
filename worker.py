#!/usr/bin/env python3
"""
~/skills-worker/worker.py

Long-polls the Hermes job queue on the VPC, runs the matching skill locally,
and reports the result back. Runs under launchd; see com.hermes.worker.plist.

The skill manifests in this directory are the allowlist: a job whose type has
no skill.yaml is rejected outright, so Hermes can never name a script that was
not declared.

Config (environment):
    QUEUE_URL            base URL of the queue, e.g. http://<tailnet-ip>:8080
    QUEUE_TOKEN          bearer token
    QUEUE_AUTH_HEADER    header carrying it            (default: Authorization)
    QUEUE_AUTH_SCHEME    prefix for the value          (default: Bearer)
    WORKER_NAME          identifies this worker        (default: mac)
    SKILLS_DIR           where the skills live         (default: this directory)
    POLL_WAIT            long-poll seconds             (default: 30)
    JOB_TIMEOUT          per-job ceiling, seconds      (default: 1800)
    HEARTBEAT_INTERVAL   seconds between heartbeats    (default: 30)
    PUBLISH_MANIFEST     push skills to the queue      (default: auto)
    VAULT_DIR            notes to catalogue            (default: ~/vault)
    PUBLISH_CATALOG      push the note catalog         (default: auto)
    CATALOG_RECONCILE_S  seconds between full pushes   (default: 3600)
    NTFY_TOPIC           optional push notifications

Usage:
    worker.py            run the loop
    worker.py --check    validate config and skills, touch no network
    worker.py --once     handle a single job, then exit
"""

import argparse
import hashlib
import json
import os
import re
import signal
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

HERE = Path(__file__).resolve().parent

QUEUE_URL = os.environ.get("QUEUE_URL", "").rstrip("/")
QUEUE_TOKEN = os.environ.get("QUEUE_TOKEN", "")
AUTH_HEADER = os.environ.get("QUEUE_AUTH_HEADER", "Authorization")
AUTH_SCHEME = os.environ.get("QUEUE_AUTH_SCHEME", "Bearer")
WORKER_NAME = os.environ.get("WORKER_NAME", "mac")
SKILLS_DIR = Path(os.environ.get("SKILLS_DIR", HERE)).expanduser()
POLL_WAIT = int(os.environ.get("POLL_WAIT", "30"))
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT", "1800"))
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "30"))
PUBLISH_MANIFEST = os.environ.get("PUBLISH_MANIFEST", "auto").lower()
VAULT = Path(os.environ.get("VAULT_DIR", Path.home() / "vault")).expanduser()
PUBLISH_CATALOG = os.environ.get("PUBLISH_CATALOG", "auto").lower()
CATALOG_RECONCILE_S = int(os.environ.get("CATALOG_RECONCILE_S", "3600"))
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

OFF = ("0", "no", "false", "off")
CAFFEINATE = "/usr/bin/caffeinate"
BACKOFF_MAX = 60

stop = threading.Event()


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}", flush=True)


def alert(title: str, body: str) -> None:
    """Best-effort push. A failed notification must never fail a job."""
    if not NTFY_TOPIC:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode(),
            headers={"Title": title, "Priority": "default"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        log(f"(ntfy failed: {exc})")


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
    """Queue unreachable or misbehaving. Always transient — back off and retry."""


def call(method: str, path: str, body=None, query=None, timeout=30):
    url = QUEUE_URL + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    if QUEUE_TOKEN:
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
        detail = exc.read().decode(errors="replace")[:300]
        raise QueueError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise QueueError(f"{method} {path} unreachable: {exc}") from exc


def lease_next():
    """Long-poll for a job. Returns the job dict, or None if none was offered."""
    _, body = call(
        "GET", "/jobs/next",
        query={"worker": WORKER_NAME, "wait": POLL_WAIT},
        timeout=POLL_WAIT + 15,
    )
    if not body:
        return None
    job = body.get("job", body) if isinstance(body, dict) else None
    if not job or not job.get("id"):
        return None
    return job


def heartbeat(job_id: str) -> None:
    call("POST", f"/jobs/{job_id}/heartbeat", body={"worker": WORKER_NAME}, timeout=15)


def complete(job_id: str, *, result=None, error=None, permanent=False) -> None:
    call("POST", f"/jobs/{job_id}/complete", body={
        "worker": WORKER_NAME,
        "status": "done" if error is None else "failed",
        "result": result,
        "error": error,
        "permanent": permanent,
    }, timeout=30)


def publish_manifest(skills: dict[str, dict]) -> None:
    """Tell the queue what this worker can do. Optional — skipped if unsupported."""
    if PUBLISH_MANIFEST in ("0", "no", "false", "off"):
        return
    payload = {"worker": WORKER_NAME, "skills": [s["manifest"] for s in skills.values()]}
    try:
        call("POST", "/manifest", body=payload, timeout=15)
        log(f"published manifest ({len(skills)} skills)")
    except QueueError as exc:
        msg = str(exc)
        if "HTTP 404" in msg or "HTTP 405" in msg:
            log("queue has no /manifest endpoint — skipping (set PUBLISH_MANIFEST=0 to silence)")
        else:
            log(f"manifest publish failed, continuing: {msg}")


# ---------------------------------------------------------------------------
# catalog
#
# The vault lives here; Hermes lives on the VPS and cannot read it. So the
# worker replicates an index of it — frontmatter plus the LLM summary, never
# the transcript body — and the queue makes that searchable. Discovery
# questions ("do I have anything about X?") are then answerable while this
# machine is asleep, which is the one thing the pull design handles badly.
#
# The note format is parsed here a third time (both skills also parse it).
# The duplication is deliberate and bounded: this copy only ever *reads*, so
# its worst failure is a missing catalog row, corrected by the next reconcile.
# The skills' copies guard against clobbering a summary, which is not
# recoverable — do not merge the two without moving all three to a shared
# module.
# ---------------------------------------------------------------------------

SUM_BEGIN = "<!-- summary:begin -->"
SUM_END = "<!-- summary:end -->"
LEGACY_MARK = "\n## Summary\n"
BLOCK_META = re.compile(
    r"<!--\s*generated\s+(\S+)\s+by\s+(\S+)(?:\s+style=(\w+))?\s*-->"
)

_catalog_unsupported = False


def split_front(text: str) -> tuple[dict | None, str]:
    """Return (frontmatter, body). Frontmatter is None if this isn't a note."""
    if not text.startswith("---"):
        return None, text
    end = text.find("\n---", 3)
    if end == -1:
        return None, text
    try:
        front = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return None, text
    if not isinstance(front, dict):
        return None, text
    return front, text[end + 4:].lstrip("\n")


def strip_summary(body: str) -> tuple[str, str]:
    """Return (transcript, summary block). Mirrors split_summary in the skills."""
    i = body.find(SUM_BEGIN)
    if i != -1:
        j = body.find(SUM_END, i)
        if j != -1:
            j += len(SUM_END)
            return (body[:i] + body[j:]).strip(), body[i:j]
    i = body.find(LEGACY_MARK)
    if i != -1:
        return body[:i].strip(), body[i:]
    return body.strip(), ""


def parse_summary(block: str) -> tuple[str | None, str]:
    """Return (style, summary text). Style is None on blocks written before
    it was stamped, and on the pre-sentinel layout."""
    if not block:
        return None, ""
    meta = BLOCK_META.search(block)
    text = block[meta.end():] if meta else block
    for marker in (SUM_BEGIN, SUM_END, "## Summary"):
        text = text.replace(marker, "")
    return (meta.group(3) if meta else None), text.strip()


def _text(value) -> str | None:
    """YAML gives dates back as date objects and numbers as ints."""
    return None if value is None else str(value)


def note_row(path: Path) -> dict | None:
    """One catalog row, or None if this file is not a note we can index."""
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        log(f"catalog: skipping {path.name}: {exc}")
        return None

    front, body = split_front(text)
    if front is None:
        return None

    try:
        rel = str(path.resolve().relative_to(VAULT.resolve()))
    except (ValueError, OSError):
        return None

    transcript, block = strip_summary(body)
    style, summary = parse_summary(block)
    duration = front.get("duration_s")
    return {
        "path": rel,
        "title": str(front.get("title") or path.stem),
        "channel": _text(front.get("channel")),
        "video_id": _text(front.get("video_id")),
        "url": _text(front.get("url")),
        "upload_date": _text(front.get("upload_date")),
        "duration_s": duration if isinstance(duration, int) else None,
        "transcript_method": _text(front.get("transcript_method")),
        "chars": len(transcript),
        "summary": summary,
        "summary_style": style,
        "has_summary": bool(summary),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "modified_at": datetime.fromtimestamp(
            path.stat().st_mtime, timezone.utc
        ).isoformat(timespec="seconds"),
    }


def scan_vault() -> list[dict]:
    """Every indexable note under VAULT, in a stable order."""
    if not VAULT.is_dir():
        return []
    rows = []
    skipped = 0
    for path in sorted(VAULT.rglob("*.md")):
        row = note_row(path)
        if row is None:
            skipped += 1
            continue
        rows.append(row)
    if skipped:
        log(f"catalog: {skipped} file(s) had no usable frontmatter, not indexed")
    return rows


def publish_catalog(notes: list[dict], *, full: bool) -> None:
    """Push notes to the queue. Optional — skipped if the endpoint is absent.

    full=True means "this is the complete set": the queue drops rows it does
    not see, which is how a note deleted in Obsidian leaves the index.
    """
    global _catalog_unsupported
    if PUBLISH_CATALOG in OFF or _catalog_unsupported:
        return
    if not notes and not full:
        return
    try:
        call("POST", "/catalog",
             body={"worker": WORKER_NAME, "full": full, "notes": notes},
             timeout=60)
        log(f"published catalog ({len(notes)} note(s){', full' if full else ''})")
    except QueueError as exc:
        msg = str(exc)
        if "HTTP 404" in msg or "HTTP 405" in msg:
            _catalog_unsupported = True
            log("queue has no /catalog endpoint — skipping "
                "(set PUBLISH_CATALOG=0 to silence)")
        else:
            log(f"catalog publish failed, continuing: {msg}")


def catalog_after(result) -> None:
    """Push the single note a finished job touched.

    Called after the job has already been reported, and swallows everything:
    a catalog push must never turn a job that succeeded into one that failed.
    """
    if PUBLISH_CATALOG in OFF or not isinstance(result, dict):
        return
    rel = (result.get("transcript_path") or result.get("summary_path")
           or result.get("source_path"))
    if not isinstance(rel, str) or not rel:
        return
    try:
        path = Path(rel)
        path = (path if path.is_absolute() else VAULT / path).resolve()
        if not path.is_relative_to(VAULT.resolve()):
            return
        row = note_row(path)
        if row is not None:
            publish_catalog([row], full=False)
    except Exception as exc:
        log(f"catalog: post-job push skipped: {exc!r}")


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
            except QueueError as exc:
                log(f"  heartbeat failed (job keeps running): {exc}")

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
        catalog_after(result)
        return

    permanent = code == 2
    log(f"job {job_id}: exit {code} after {took:.0f}s ({'permanent' if permanent else 'transient'})")
    complete(job_id, error=tail(err) or f"exit {code} with no stderr", permanent=permanent)
    if permanent:
        alert(f"Job failed: {job_type}", tail(err, lines=4, limit=300))


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
    print(f"timeouts   poll {POLL_WAIT}s, job {JOB_TIMEOUT}s, heartbeat {HEARTBEAT_INTERVAL}s")
    print(f"catalog    {'off' if PUBLISH_CATALOG in OFF else f'on, full push every {CATALOG_RECONCILE_S}s'}")
    print(f"ntfy       {NTFY_TOPIC or '(unset)'}")
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
            notes.append(f"{VAULT} does not exist — the catalog will be empty")
        rows = scan_vault()
        print(f"  catalog: {len(rows)} note(s)")
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="validate config, touch no network")
    ap.add_argument("--once", action="store_true", help="handle one job, then exit")
    args = ap.parse_args()

    skills = load_skills(SKILLS_DIR)

    if args.check:
        return check(skills)

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
    publish_manifest(skills)
    publish_catalog(scan_vault(), full=True)
    reconciled = time.time()

    backoff = 1
    while not stop.is_set():
        # A note edited directly in Obsidian is reported by no job, so the
        # catalog is re-sent whole on a timer. This bounds how stale it gets.
        if CATALOG_RECONCILE_S > 0 and time.time() - reconciled >= CATALOG_RECONCILE_S:
            publish_catalog(scan_vault(), full=True)
            reconciled = time.time()

        try:
            job = lease_next()
            backoff = 1
        except QueueError as exc:
            # The laptop is off the network often. That is normal, not an error.
            log(f"queue unavailable, retrying in {backoff}s: {exc}")
            stop.wait(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
            continue

        if job is None:
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
