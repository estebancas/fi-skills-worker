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

class StubQueue:
    """Minimal in-process queue. Records every call the worker makes."""

    def __init__(self, jobs=None, manifest_status=404, catalog_status=200):
        self.jobs = list(jobs or [])
        self.manifest_status = manifest_status
        self.catalog_status = catalog_status
        self.calls = []                          # (method, path, body)
        self.token_seen: str | None = None       # last Authorization header seen
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, code, obj=None):
                body = json.dumps(obj).encode() if obj is not None else b""
                self.send_response(code)
                if body:
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self):
                outer.token_seen = self.headers.get("Authorization")
                u = urlparse(self.path)
                outer.calls.append(("GET", u.path, parse_qs(u.query)))
                if u.path == "/jobs/next":
                    with outer._lock:
                        if outer.jobs:
                            return self._reply(200, {"job": outer.jobs.pop(0)})
                    return self._reply(204)
                self._reply(404)

            def do_POST(self):
                outer.token_seen = self.headers.get("Authorization")
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                path = urlparse(self.path).path
                outer.calls.append(("POST", path, body))
                if path == "/manifest":
                    return self._reply(outer.manifest_status, {"ok": True})
                if path == "/catalog":
                    return self._reply(outer.catalog_status, {"ok": True})
                self._reply(200, {"ok": True})

            def log_message(self, format, *args):
                """Silence the per-request logging to stderr."""

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

    def catalogs(self):
        return [b for m, p, b in self.calls if m == "POST" and p == "/catalog"]


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
