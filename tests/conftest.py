"""Shared fixtures.

The modules under test read their configuration from the environment **at import
time** (VAULT_DIR, OLLAMA_*, QUEUE_URL, ...). A test that wants different config
therefore needs a freshly imported module, not a cached one — hence `load`,
which builds a new module object per call instead of going through sys.modules.

Nothing here touches the network, Ollama, or the real vault.
"""

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def load(monkeypatch):
    """Import one of the scripts fresh, with `env` applied first.

    Returns the module object. Because each call re-executes the module body,
    module-level constants pick up the patched environment.
    """

    def _load(rel_path: str, env: dict | None = None):
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, str(value))
        path = ROOT / rel_path
        name = f"uut_{rel_path.replace('/', '_').replace('.', '_')}"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot build an import spec for {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    return _load


# ---------------------------------------------------------------------------
# vault
# ---------------------------------------------------------------------------

FRONTMATTER = """---
title: "Test Video"
channel: "Chan"
video_id: abc123
url: http://example/watch?v=abc123
duration_s: 862
upload_date: 2026-01-01
transcript_method: captions:auto
transcript_language: en
transcribed_at: 2026-01-01T00:00:00+00:00
---
"""

TRANSCRIPT = "so the thing about this is " * 40  # ~1000 chars, over the 200 floor


@pytest.fixture
def vault(tmp_path):
    """An empty vault directory with a transcripts/ subdir."""
    v = tmp_path / "vault"
    (v / "transcripts").mkdir(parents=True)
    return v


@pytest.fixture
def note(vault):
    """A transcript note with no summary yet. Returns its path."""
    p = vault / "transcripts" / "abc123-test-video.md"
    p.write_text(FRONTMATTER + "\n" + TRANSCRIPT, encoding="utf-8")
    return p


def note_with_summary(path: Path, summary: str, style: str, model="gpt-oss:latest"):
    """Write a note carrying a sentinel-delimited summary block."""
    block = (
        "<!-- summary:begin -->\n## Summary\n\n"
        f"<!-- generated 2026-01-01 by {model} style={style} -->\n\n"
        f"{summary}\n<!-- summary:end -->"
    )
    path.write_text(FRONTMATTER + "\n" + block + "\n\n" + TRANSCRIPT, encoding="utf-8")
    return path


def note_legacy(path: Path, summary: str):
    """Pre-sentinel layout: summary appended under a bare heading at the end."""
    path.write_text(
        FRONTMATTER + "\n" + TRANSCRIPT + f"\n\n## Summary\n\n{summary}\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# stub queue — stands in for the VPS service in worker tests
# ---------------------------------------------------------------------------

LIMITS = {"recommended_batch_bytes": 200000, "recommended_batch_notes": 10,
          "max_request_bytes": 1000000, "max_batch_notes": 40}


class StubQueue:
    """In-process stand-in for fi-hub's /worker API. Records every call.

    Speaks the shapes captured in docs-stage3/HANDOFF.md section 4:

        GET  /healthz                 200 {ok, time}
        GET  /jobs/next               200 {job} | 204
        POST /jobs/{id}/heartbeat     200 {ok, lease_until} | 409
        POST /jobs/{id}/complete      200 {job} | 409
        POST /manifest                200 {hash, skills}
        POST /notes                   200 {ok, received, ..., limits}
        POST /notes/reconcile         200 {ok, ..., pending_deletes}

    Pass `token` to make it enforce the bearer like the real one (401 with
    {"error": "bad or missing token"}); left None it accepts anything, which is
    what the older tests assume.
    """

    def __init__(self, jobs=None, manifest_status=404, token=None, heartbeat_status=200, complete_status=200,
                 notes_responder=None, reconcile_pending=None, reconcile_extra=None,
                 edge_block=None, edge_block_all=False):
        self.jobs = list(jobs or [])
        self.manifest_status = manifest_status
        self.token = token
        self.heartbeat_status = heartbeat_status
        self.complete_status = complete_status
        # notes_responder(body) -> (status, json); default is a clean 200.
        self.notes_responder = notes_responder
        # pending_deletes to report on successive /notes/reconcile calls.
        self.reconcile_pending = list(reconcile_pending or [])
        # extra keys merged into every reconcile response (missing, stale, ...).
        self.reconcile_extra = dict(reconcile_extra or {})
        # Simulates Cloudflare's edge, which answers 403 "error 1010" to the
        # default `Python-urllib/x.y` User-Agent before fi-hub sees the request.
        # edge_block is the body style, "json" (what urllib receives, captured
        # live) or "text" (what curl receives); edge_block_all refuses every
        # client, for tests of how the worker reports it.
        self.edge_block = edge_block
        self.edge_block_all = edge_block_all
        self.user_agents: list[str] = []
        self.calls = []                          # (method, path, body-or-query)
        self.token_seen: str | None = None       # last Authorization header seen
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, code, obj=None):
                body = json.dumps(obj).encode() if obj is not None else b""
                self.send_response(code)
                if code == 401:
                    self.send_header("www-authenticate", "Bearer")
                if body:
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _edge_blocked(self) -> bool:
                ua = self.headers.get("User-Agent", "")
                outer.user_agents.append(ua)
                if not outer.edge_block:
                    return False
                if not (outer.edge_block_all or ua.startswith("Python-urllib")):
                    return False
                if outer.edge_block == "text":
                    body = b"error code: 1010"
                    ctype = "text/plain"
                else:
                    body = json.dumps({
                        "type": "https://developers.cloudflare.com/support/troubleshooting/"
                                "http-status-codes/cloudflare-1xxx-errors/error-1010/",
                        "title": "Error 1010: Access denied", "status": 403,
                        "detail": "The site owner has blocked access based on your "
                                  "browser's signature."}).encode()
                    ctype = "application/problem+json"
                self.send_response(403)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return True

            def _authorised(self) -> bool:
                outer.token_seen = self.headers.get("Authorization")
                if outer.token is None:
                    return True
                if outer.token_seen == f"Bearer {outer.token}":
                    return True
                self._reply(401, {"error": "bad or missing token"})
                return False

            def do_GET(self):
                u = urlparse(self.path)
                outer.calls.append(("GET", u.path, parse_qs(u.query)))
                if self._edge_blocked() or not self._authorised():
                    return
                if u.path == "/healthz":
                    return self._reply(200, {"ok": True, "time": 1793551872})
                if u.path == "/jobs/next":
                    with outer._lock:
                        if outer.jobs:
                            return self._reply(200, {"job": outer.jobs.pop(0)})
                    return self._reply(204)
                self._reply(404, {"error": "not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(n) or b"{}"
                u = urlparse(self.path)
                path = u.path
                body = json.loads(raw)
                outer.calls.append(("POST", path, body))
                outer.last_query = parse_qs(u.query)
                outer.raw_sizes.append(len(raw))
                if self._edge_blocked() or not self._authorised():
                    return
                if path == "/manifest":
                    if outer.manifest_status != 200:
                        return self._reply(outer.manifest_status, {"ok": True})
                    return self._reply(200, {"hash": "sha256:stub",
                                             "skills": len(body.get("skills", []))})
                if path == "/notes":
                    if outer.notes_responder:
                        code, obj = outer.notes_responder(body)
                        return self._reply(code, obj)
                    got = len(body.get("notes", []))
                    return self._reply(200, {
                        "ok": True, "received": got, "inserted": got, "updated": 0,
                        "unchanged": 0, "ignored": 0, "indexed": got,
                        "index_pending": 0, "index_failed": 0, "index_errors": [],
                        "unknown_keys": [], "total": got, "limits": LIMITS})
                if path == "/notes/reconcile":
                    with outer._lock:
                        pending = outer.reconcile_pending.pop(0) if outer.reconcile_pending else 0
                    return self._reply(200, {
                        "ok": True, "kept": len(body.get("keep", [])), "deleted": 0,
                        "delete_failed": 0, "delete_errors": [],
                        "pending_deletes": pending, "missing": [], "missing_count": 0,
                        "stale": [], "stale_count": 0, **outer.reconcile_extra})
                if path.endswith("/heartbeat"):
                    if outer.heartbeat_status != 200:
                        return self._reply(outer.heartbeat_status, {
                            "error": "lease lost or job not leased by this worker"})
                    return self._reply(200, {"ok": True, "lease_until": 1793551992})
                if path.endswith("/complete"):
                    if outer.complete_status != 200:
                        return self._reply(outer.complete_status, {
                            "error": "job not in leased state"})
                    return self._reply(200, {"job": {"id": path.split("/")[2],
                                                      "status": "done", "attempts": 1}})
                self._reply(200, {"ok": True})

            def log_message(self, format, *args):
                """Silence the per-request logging to stderr."""

        self.last_query: dict = {}
        self.raw_sizes: list[int] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def completions(self):
        return [b for m, p, b in self.calls if m == "POST" and p.endswith("/complete")]

    def notes_pushes(self):
        return [b for m, p, b in self.calls if m == "POST" and p == "/notes"]

    def reconciles(self):
        return [b for m, p, b in self.calls if m == "POST" and p == "/notes/reconcile"]

    def polls(self):
        return [q for m, p, q in self.calls if m == "GET" and p == "/jobs/next"]


@pytest.fixture
def stub_queue():
    return StubQueue


# ---------------------------------------------------------------------------
# fake skills — real subprocesses, so the worker's exit-code handling is real
# ---------------------------------------------------------------------------

def make_skill(root: Path, job_type: str, body: str, run=None):
    """Create <root>/<job_type>/{run.py,skill.yaml} with `body` as run.py."""
    d = root / job_type
    d.mkdir(parents=True, exist_ok=True)
    (d / "run.py").write_text(body, encoding="utf-8")
    manifest = f"type: {job_type}\nsummary: test skill\n"
    if run is not None:
        manifest += f"run: {json.dumps(run)}\n"
    (d / "skill.yaml").write_text(manifest, encoding="utf-8")
    return d


SKILL_OK = """
import json, sys
params = json.load(sys.stdin)
print("progress on stderr", file=sys.stderr)
print(json.dumps({"echoed": params, "ok": True}))
"""

SKILL_PERMANENT = """
import sys
print("ERROR: nope", file=sys.stderr)
sys.exit(2)
"""

SKILL_TRANSIENT = """
import sys
print("ERROR: try later", file=sys.stderr)
sys.exit(1)
"""

SKILL_NO_JSON = """
print("I forgot to print JSON")
"""

SKILL_SLOW = """
import time
time.sleep(30)
"""


@pytest.fixture
def skills_dir(tmp_path):
    return tmp_path / "skills"


@pytest.fixture
def make_skill_fixture():
    return make_skill
