"""worker.py

Guarded behaviours:

  1. Only declared job types run                   (the allowlist IS the boundary)
  2. exit 2 -> permanent, exit 1 -> transient      (a poisoned job must not cycle)
  3. A long job heartbeats                         (or the lease lapses under it)
  4. An unreachable queue backs off, never crashes (this laptop is often offline)
  5. The notes push never writes a note and never fails a job

Every test runs against an in-process stub queue, and VAULT_DIR always points
into tmp_path. Nothing reaches the VPS or the real vault.
"""

import hashlib
import socket
import ssl
import sys
import threading
import time
import urllib.error
from pathlib import Path

import pytest

from conftest import (
    FRONTMATTER,
    SKILL_NO_JSON,
    SKILL_OK,
    SKILL_PERMANENT,
    SKILL_SLOW,
    SKILL_TRANSIENT,
    make_skill,
    note_legacy,
    note_with_summary,
)

WORKER = "worker.py"


@pytest.fixture
def skills(tmp_path):
    """A skills directory with one well-behaved skill."""
    root = tmp_path / "skills"
    make_skill(root, "good_skill", SKILL_OK)
    return root


def load_worker(load, skills_root, queue_url="http://127.0.0.1:1", **env):
    # VAULT_DIR points at a directory that does not exist unless a test opts
    # in, and the notes push is off by default: no test may read the real vault.
    return load(WORKER, {
        "SKILLS_DIR": str(skills_root),
        "VAULT_DIR": str(skills_root.parent / "no-vault"),
        "QUEUE_URL": queue_url,
        "QUEUE_TOKEN": "test-token",
        "POLL_WAIT": "1",
        "POLL_INTERVAL": "1",
        "JOB_TIMEOUT": "30",
        "HEARTBEAT_INTERVAL": "30",
        "PUBLISH_MANIFEST": "0",
        "PUBLISH_CATALOG": "0",
        **env,
    })


# ---------------------------------------------------------------------------
# manifests are the allowlist
# ---------------------------------------------------------------------------

def test_load_skills_reads_manifests(load, skills):
    w = load_worker(load, skills)
    found = w.load_skills(skills)
    assert set(found) == {"good_skill"}
    assert found["good_skill"]["dir"] == skills / "good_skill"


def test_load_skills_ignores_manifest_without_type(load, tmp_path):
    root = tmp_path / "skills"
    (root / "nameless").mkdir(parents=True)
    (root / "nameless" / "run.py").write_text("", encoding="utf-8")
    (root / "nameless" / "skill.yaml").write_text("summary: no type here\n", encoding="utf-8")
    w = load_worker(load, root)
    assert w.load_skills(root) == {}


def test_load_skills_survives_broken_yaml(load, tmp_path):
    """One bad manifest must not take the whole worker down."""
    root = tmp_path / "skills"
    make_skill(root, "fine", SKILL_OK)
    (root / "broken").mkdir(parents=True)
    (root / "broken" / "run.py").write_text("", encoding="utf-8")
    (root / "broken" / "skill.yaml").write_text("type: [unclosed\n", encoding="utf-8")
    w = load_worker(load, root)
    assert set(w.load_skills(root)) == {"fine"}


def test_load_skills_registers_by_declared_type_not_dirname(load, tmp_path):
    root = tmp_path / "skills"
    d = make_skill(root, "dirname", SKILL_OK)
    (d / "skill.yaml").write_text("type: declared_type\n", encoding="utf-8")
    w = load_worker(load, root)
    assert set(w.load_skills(root)) == {"declared_type"}


# ---------------------------------------------------------------------------
# resolve_run — never depend on what PATH resolves to
# ---------------------------------------------------------------------------

def test_resolve_run_pins_env_python_to_our_interpreter(load, skills):
    w = load_worker(load, skills)
    assert w.resolve_run(["/usr/bin/env", "python3", "run.py"]) == [sys.executable, "run.py"]


def test_resolve_run_pins_bare_python(load, skills):
    w = load_worker(load, skills)
    assert w.resolve_run(["python3", "run.py"]) == [sys.executable, "run.py"]


def test_resolve_run_leaves_explicit_commands_alone(load, skills):
    w = load_worker(load, skills)
    assert w.resolve_run(["/opt/bin/node", "x.js"]) == ["/opt/bin/node", "x.js"]


# ---------------------------------------------------------------------------
# parse_result — the stdout contract
# ---------------------------------------------------------------------------

def test_parse_result_reads_the_last_line(load, skills):
    w = load_worker(load, skills)
    assert w.parse_result('noise\n{"a": 1}\n') == {"a": 1}


def test_parse_result_tolerates_trailing_blank_lines(load, skills):
    w = load_worker(load, skills)
    assert w.parse_result('{"a": 1}\n\n\n') == {"a": 1}


def test_parse_result_rejects_non_json_last_line(load, skills):
    """Trailing prose means the skill broke its contract — don't guess."""
    w = load_worker(load, skills)
    assert w.parse_result('{"a": 1}\noops trailing text') is None


def test_parse_result_empty_stdout(load, skills):
    w = load_worker(load, skills)
    assert w.parse_result("") is None


def test_tail_bounds_the_error_text(load, skills):
    w = load_worker(load, skills)
    out = w.tail("\n".join(str(i) for i in range(500)), lines=3)
    assert out.splitlines() == ["497", "498", "499"]


# ---------------------------------------------------------------------------
# run_skill — real subprocesses, real exit codes
# ---------------------------------------------------------------------------

def test_run_skill_passes_params_on_stdin(load, skills):
    w = load_worker(load, skills)
    code, out, err = w.run_skill(w.load_skills(skills)["good_skill"], {"hello": "world"})
    assert code == 0
    assert w.parse_result(out)["echoed"] == {"hello": "world"}


def test_run_skill_captures_stderr_separately(load, skills):
    w = load_worker(load, skills)
    code, out, err = w.run_skill(w.load_skills(skills)["good_skill"], {})
    assert "progress on stderr" in err
    assert "progress on stderr" not in out, "logs must not pollute the result line"


def test_run_skill_reports_exit_codes(load, tmp_path):
    root = tmp_path / "skills"
    make_skill(root, "permanent", SKILL_PERMANENT)
    make_skill(root, "transient", SKILL_TRANSIENT)
    w = load_worker(load, root)
    skills = w.load_skills(root)
    assert w.run_skill(skills["permanent"], {})[0] == 2
    assert w.run_skill(skills["transient"], {})[0] == 1


def test_run_skill_kills_an_overrunning_job(load, tmp_path):
    root = tmp_path / "skills"
    make_skill(root, "slow", SKILL_SLOW)
    w = load_worker(load, root, JOB_TIMEOUT="1")
    started = time.time()
    code, out, err = w.run_skill(w.load_skills(root)["slow"], {})
    assert code == 1, "a timeout is transient — the job may succeed on a retry"
    assert "JOB_TIMEOUT" in err
    assert time.time() - started < 15, "must not wait for the full sleep"


# ---------------------------------------------------------------------------
# handle — job outcome classification
# ---------------------------------------------------------------------------

def test_handle_unknown_type_is_permanent(load, skills, stub_queue):
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url)
        w.handle({"id": "j1", "type": "not_a_skill", "params": {}}, w.load_skills(skills))
        body = q.completions()[0]
    assert body["status"] == "failed"
    assert body["permanent"] is True
    assert "not_a_skill" in body["error"]


def test_handle_unknown_type_never_executes_anything(load, skills, stub_queue, monkeypatch):
    """The allowlist is the security boundary — nothing may run."""
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url)

        def boom(*a, **k):
            raise AssertionError("an undeclared type must not reach run_skill")

        monkeypatch.setattr(w, "run_skill", boom)
        w.handle({"id": "j1", "type": "../../etc/passwd", "params": {}}, w.load_skills(skills))
        assert q.completions()[0]["permanent"] is True


def test_handle_success_reports_the_result(load, skills, stub_queue):
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url)
        w.handle({"id": "j1", "type": "good_skill", "params": {"x": 1}}, w.load_skills(skills))
        body = q.completions()[0]
    assert body["status"] == "done"
    assert body["result"]["echoed"] == {"x": 1}
    assert body["error"] is None


def test_handle_exit_2_is_permanent(load, tmp_path, stub_queue):
    root = tmp_path / "skills"
    make_skill(root, "permanent", SKILL_PERMANENT)
    with stub_queue() as q:
        w = load_worker(load, root, queue_url=q.url)
        w.handle({"id": "j1", "type": "permanent", "params": {}}, w.load_skills(root))
        body = q.completions()[0]
    assert body["status"] == "failed" and body["permanent"] is True
    assert "nope" in body["error"]


def test_handle_exit_1_is_transient(load, tmp_path, stub_queue):
    root = tmp_path / "skills"
    make_skill(root, "transient", SKILL_TRANSIENT)
    with stub_queue() as q:
        w = load_worker(load, root, queue_url=q.url)
        w.handle({"id": "j1", "type": "transient", "params": {}}, w.load_skills(root))
        body = q.completions()[0]
    assert body["status"] == "failed" and body["permanent"] is False


def test_handle_exit_0_without_json_is_transient(load, tmp_path, stub_queue):
    """Success with no parseable result is a bug, not a completed job."""
    root = tmp_path / "skills"
    make_skill(root, "nojson", SKILL_NO_JSON)
    with stub_queue() as q:
        w = load_worker(load, root, queue_url=q.url)
        w.handle({"id": "j1", "type": "nojson", "params": {}}, w.load_skills(root))
        body = q.completions()[0]
    assert body["status"] == "failed" and body["permanent"] is False


def test_handle_accepts_params_as_a_json_string(load, skills, stub_queue):
    """Some queues store params serialized; both shapes must work."""
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url)
        w.handle({"id": "j1", "type": "good_skill", "params": '{"x": 2}'},
                 w.load_skills(skills))
        body = q.completions()[0]
    assert body["result"]["echoed"] == {"x": 2}


def test_handle_rejects_unparseable_params(load, skills, stub_queue):
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url)
        w.handle({"id": "j1", "type": "good_skill", "params": "{not json"},
                 w.load_skills(skills))
        body = q.completions()[0]
    assert body["permanent"] is True


def test_long_job_heartbeats_to_hold_the_lease(load, tmp_path, stub_queue):
    root = tmp_path / "skills"
    make_skill(root, "slow", "import time; time.sleep(2.5); print('{}')")
    with stub_queue() as q:
        w = load_worker(load, root, queue_url=q.url, HEARTBEAT_INTERVAL="1", JOB_TIMEOUT="30")
        w.handle({"id": "j1", "type": "slow", "params": {}}, w.load_skills(root))
        beats = [p for m, p, b in q.calls if p.endswith("/heartbeat")]
    assert len(beats) >= 1, "a slow job with no heartbeat gets requeued underneath it"


# ---------------------------------------------------------------------------
# queue transport
# ---------------------------------------------------------------------------

def test_auth_header_is_sent(load, skills, stub_queue):
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url)
        w.call("GET", "/jobs/next")
    assert q.token_seen == "Bearer test-token"


def test_auth_scheme_is_configurable(load, skills, stub_queue):
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        QUEUE_AUTH_HEADER="X-Token", QUEUE_AUTH_SCHEME="")
        w.call("GET", "/jobs/next")
    assert q.token_seen is None, "the token moved to X-Token"


def test_unreachable_queue_raises_queueerror(load, skills):
    w = load_worker(load, skills, queue_url="http://127.0.0.1:1")
    with pytest.raises(w.QueueError):
        w.call("GET", "/jobs/next")


def test_http_error_raises_queueerror(load, skills, stub_queue):
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url)
        with pytest.raises(w.QueueError) as exc:
            w.call("GET", "/nope")
    assert "404" in str(exc.value)


def test_lease_next_returns_none_on_204(load, skills, stub_queue):
    with stub_queue(jobs=[]) as q:
        w = load_worker(load, skills, queue_url=q.url)
        assert w.lease_next() is None


def test_lease_next_unwraps_a_job_envelope(load, skills, stub_queue):
    with stub_queue(jobs=[{"id": "j1", "type": "good_skill", "params": {}}]) as q:
        w = load_worker(load, skills, queue_url=q.url)
        job = w.lease_next()
    assert job["id"] == "j1"


def test_lease_next_sends_worker_name_and_wait(load, skills, stub_queue):
    with stub_queue(jobs=[]) as q:
        w = load_worker(load, skills, queue_url=q.url, WORKER_NAME="testmac")
        w.lease_next()
        _, _, query = q.calls[0]
    assert query["worker"] == ["testmac"]
    assert query["wait"] == ["1"]


def test_manifest_404_is_not_fatal(load, skills, stub_queue):
    """A queue with no /manifest endpoint must not stop the worker."""
    with stub_queue(manifest_status=404) as q:
        w = load_worker(load, skills, queue_url=q.url, PUBLISH_MANIFEST="auto")
        w.publish_manifest(w.load_skills(skills))  # no raise


def test_manifest_publish_sends_the_skill_manifests(load, skills, stub_queue):
    with stub_queue(manifest_status=200) as q:
        w = load_worker(load, skills, queue_url=q.url, PUBLISH_MANIFEST="auto")
        w.publish_manifest(w.load_skills(skills))
        body = [b for m, p, b in q.calls if p == "/manifest"][0]
    assert body["skills"][0]["type"] == "good_skill"


# ---------------------------------------------------------------------------
# --check and the main loop
# ---------------------------------------------------------------------------

def test_check_passes_with_valid_config(load, skills, capsys):
    w = load_worker(load, skills, queue_url="http://127.0.0.1:8080")
    assert w.check(w.load_skills(skills)) == 0
    assert "check passed" in capsys.readouterr().out


def test_check_passes_without_caffeinate(load, skills, capsys, monkeypatch):
    """Non-macOS hosts (and CI) have no caffeinate — that must not block startup."""
    w = load_worker(load, skills, queue_url="http://127.0.0.1:8080")
    monkeypatch.setattr(w, "CAFFEINATE", "/nonexistent/caffeinate")
    assert w.check(w.load_skills(skills)) == 0
    out = capsys.readouterr().out
    assert "NOTE:" in out and "hold off sleep" in out
    assert "PROBLEM:" not in out


def test_check_flags_missing_config(load, skills, capsys):
    w = load_worker(load, skills, QUEUE_URL="", QUEUE_TOKEN="")
    assert w.check(w.load_skills(skills)) == 1
    out = capsys.readouterr().out
    assert "QUEUE_URL is unset" in out and "QUEUE_TOKEN is unset" in out


def test_check_never_prints_the_token(load, skills, capsys):
    w = load_worker(load, skills, QUEUE_TOKEN="super-secret-value")
    w.check(w.load_skills(skills))
    assert "super-secret-value" not in capsys.readouterr().out


def test_check_flags_a_missing_run_script(load, tmp_path, capsys):
    root = tmp_path / "skills"
    d = make_skill(root, "ghost", SKILL_OK)
    (d / "run.py").unlink()
    w = load_worker(load, root)
    assert w.check(w.load_skills(root)) == 1
    assert "MISSING" in capsys.readouterr().out


def test_main_once_processes_exactly_one_job(load, skills, stub_queue):
    jobs = [{"id": "j1", "type": "good_skill", "params": {"n": 1}},
            {"id": "j2", "type": "good_skill", "params": {"n": 2}}]
    with stub_queue(jobs=jobs) as q:
        w = load_worker(load, skills, queue_url=q.url)
        sys.argv = ["worker.py", "--once"]
        assert w.main() == 0
        completions = q.completions()
    assert len(completions) == 1
    assert completions[0]["result"]["echoed"] == {"n": 1}


def test_main_refuses_to_start_without_a_queue_url(load, skills):
    w = load_worker(load, skills, QUEUE_URL="")
    sys.argv = ["worker.py"]
    assert w.main() == 2


def test_main_refuses_to_start_with_no_skills(load, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    w = load_worker(load, empty)
    sys.argv = ["worker.py"]
    assert w.main() == 2


# ---------------------------------------------------------------------------
# notes — extraction (note_row)
#
# This parser is the worker's own copy of the note format. It must agree with
# the skills' copies on where a summary starts and ends, or the index
# carries markup instead of prose.
# ---------------------------------------------------------------------------

NOTE = "abc123-test-video.md"


@pytest.fixture
def cat(load, skills, vault):
    """A worker pointed at a real (temporary) vault, pushing enabled."""
    return load_worker(load, skills, VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")


def test_note_row_reads_frontmatter(cat, note):
    row = cat.note_row(note)
    assert row["path"] == f"transcripts/{NOTE}"
    assert row["title"] == "Test Video"
    assert row["channel"] == "Chan"
    assert row["video_id"] == "abc123"
    assert row["duration_s"] == 862
    assert row["transcript_method"] == "captions:auto"


def test_note_row_extracts_a_sentinel_summary(cat, note):
    note_with_summary(note, "A careful summary of the thing.", "detailed")
    row = cat.note_row(note)
    assert row["summary"] == "A careful summary of the thing."
    assert row["summary_style"] == "detailed"
    assert row["has_summary"] is True


def test_note_row_extracts_a_legacy_summary(cat, note):
    note_legacy(note, "Written before the sentinels existed.")
    row = cat.note_row(note)
    assert row["summary"] == "Written before the sentinels existed."
    assert row["has_summary"] is True
    # No style stamp to read, and the extractor must not invent one.
    assert row["summary_style"] is None


def test_note_row_without_a_summary(cat, note):
    row = cat.note_row(note)
    assert row["summary"] == ""
    assert row["has_summary"] is False
    assert row["summary_style"] is None


def test_summary_text_carries_no_markup(cat, note):
    """A leaked sentinel or heading would show up inside search snippets."""
    note_with_summary(note, "Just the prose.", "brief")
    summary = cat.note_row(note)["summary"]
    for marker in ("<!--", "-->", "## Summary", "summary:begin", "summary:end"):
        assert marker not in summary


def test_chars_counts_the_transcript_not_the_summary(cat, note):
    plain = cat.note_row(note)["chars"]
    note_with_summary(note, "A summary that adds length to the file.", "detailed")
    assert cat.note_row(note)["chars"] == plain


def test_note_row_skips_a_file_without_frontmatter(cat, vault):
    stray = vault / "transcripts" / "notes.md"
    stray.write_text("just some thoughts, no frontmatter\n", encoding="utf-8")
    assert cat.note_row(stray) is None


def test_note_row_skips_broken_frontmatter(cat, vault):
    broken = vault / "transcripts" / "broken.md"
    broken.write_text("---\ntitle: \"unclosed\n  bad: [\n---\n\nbody\n", encoding="utf-8")
    assert cat.note_row(broken) is None


def test_note_row_skips_a_file_outside_the_vault(cat, tmp_path, note):
    outside = tmp_path / "elsewhere.md"
    outside.write_text(note.read_text(encoding="utf-8"), encoding="utf-8")
    assert cat.note_row(outside) is None


def test_note_row_skips_undecodable_bytes(cat, vault):
    binary = vault / "transcripts" / "binary.md"
    binary.write_bytes(b"---\ntitle: x\n---\n\n\xff\xfe not utf-8")
    assert cat.note_row(binary) is None


def test_scan_vault_lists_notes_and_skips_the_rest(cat, note, vault):
    (vault / "transcripts" / "stray.md").write_text("no frontmatter", encoding="utf-8")
    rows = cat.scan_vault()
    assert [r["path"] for r in rows] == [f"transcripts/{NOTE}"]


def test_scan_vault_is_stable_across_runs(cat, note):
    """The push is re-sent on a timer; an unchanged vault must produce an
    identical payload, or the hub sees changes that are not there."""
    assert cat.scan_vault() == cat.scan_vault()


def test_scan_vault_on_a_missing_directory(load, skills, tmp_path):
    w = load_worker(load, skills, VAULT_DIR=str(tmp_path / "gone"),
                    PUBLISH_CATALOG="auto")
    assert w.scan_vault() == []


# ---------------------------------------------------------------------------
# fi-hub transport (milestone 1)
#
# The queue is now an HTTPS short-poll Cloudflare Worker. What these protect:
#   - a TLS failure is not "queue unreachable", and a 401 is not an outage
#   - an empty poll sleeps, or the loop spins and exhausts the Free plan
#   - --probe proves the link without ever counting as "the Mac was seen"
#   - a lost lease (409) is logged, stops heartbeating, and never crashes
#   - the Mac sends no pushes
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent


class NotTLS:
    """A TCP server that answers any connection with plain HTTP text.

    Pointed at by an https:// URL, the client's handshake fails with a real
    ssl.SSLError ("wrong version number"). Hermetic: no certificates needed.
    """

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.url = f"https://127.0.0.1:{self.sock.getsockname()[1]}"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            except OSError:
                pass
            conn.close()

    def close(self):
        self.sock.close()


@pytest.fixture
def not_tls():
    server = NotTLS()
    yield server
    server.close()


def run_main(w, monkeypatch, seconds, args=("worker.py",)):
    """Run main() for a bounded time, then ask it to stop."""
    monkeypatch.setattr(w.signal, "signal", lambda *a: None)
    monkeypatch.setattr(sys, "argv", list(args))
    timer = threading.Timer(seconds, w.stop.set)
    timer.start()
    try:
        return w.main()
    finally:
        timer.cancel()


def test_tls_failure_is_reported_as_tls_not_unreachable(load, skills, not_tls):
    w = load_worker(load, skills, queue_url=not_tls.url)
    with pytest.raises(w.TLSError) as exc:
        w.call("GET", "/jobs/next", timeout=5)
    assert "TLS failure" in str(exc.value)
    assert "unreachable" not in str(exc.value)


def test_certificate_verify_failure_is_a_tls_error(load, skills, monkeypatch):
    """What a missing SSL_CERT_FILE looks like: urlopen wraps it in URLError."""
    w = load_worker(load, skills, queue_url="https://fi-hub.invalid/worker")
    cause = ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED")

    def refuse(*a, **k):
        raise urllib.error.URLError(cause)

    monkeypatch.setattr(w.urllib.request, "urlopen", refuse)
    with pytest.raises(w.TLSError) as exc:
        w.call("GET", "/healthz")
    assert "CERTIFICATE_VERIFY_FAILED" in str(exc.value)
    assert "SSL_CERT_FILE" in str(exc.value), "the message must point at the fix"


def test_a_bare_ssl_error_is_a_tls_error(load, skills, monkeypatch):
    w = load_worker(load, skills, queue_url="https://fi-hub.invalid/worker")

    def refuse(*a, **k):
        raise ssl.SSLError("handshake")

    monkeypatch.setattr(w.urllib.request, "urlopen", refuse)
    with pytest.raises(w.TLSError):
        w.call("GET", "/healthz")


def test_an_unreachable_queue_is_not_a_tls_error(load, skills):
    w = load_worker(load, skills, queue_url="http://127.0.0.1:1")
    with pytest.raises(w.QueueError) as exc:
        w.call("GET", "/healthz")
    assert not isinstance(exc.value, w.TLSError)
    assert "unreachable" in str(exc.value)


def test_a_401_is_an_auth_error_and_other_http_errors_are_not(load, skills, stub_queue):
    with stub_queue(token="right") as q:
        w = load_worker(load, skills, queue_url=q.url, QUEUE_TOKEN="wrong")
        with pytest.raises(w.AuthError):
            w.call("GET", "/healthz")
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url)
        with pytest.raises(w.QueueError) as exc:
            w.call("GET", "/nope")
    assert not isinstance(exc.value, (w.AuthError, w.TLSError))


def test_main_loop_logs_tls_failure_as_such(load, skills, not_tls, monkeypatch, capsys):
    w = load_worker(load, skills, queue_url=not_tls.url)
    run_main(w, monkeypatch, 1.5)
    out = capsys.readouterr().out
    assert "TLS failure" in out
    assert "queue unavailable" not in out


def test_main_loop_logs_a_refused_token_as_such(
    load, skills, stub_queue, monkeypatch, capsys
):
    with stub_queue(token="right") as q:
        w = load_worker(load, skills, queue_url=q.url, QUEUE_TOKEN="wrong")
        run_main(w, monkeypatch, 1.5)
    out = capsys.readouterr().out
    assert "refused the token" in out
    assert "queue unavailable" not in out
    assert "wrong" not in out, "the token must never reach the log"


def test_main_loop_still_calls_an_outage_an_outage(load, skills, monkeypatch, capsys):
    w = load_worker(load, skills, queue_url="http://127.0.0.1:1")
    run_main(w, monkeypatch, 1.5)
    assert "queue unavailable" in capsys.readouterr().out


def test_an_empty_poll_sleeps_instead_of_spinning(load, skills, stub_queue, monkeypatch):
    """fi-hub answers 204 at once. Without the sleep this is hundreds of calls a
    second, and the Free plan allows 100,000 requests a day."""
    with stub_queue(jobs=[]) as q:
        w = load_worker(load, skills, queue_url=q.url, POLL_INTERVAL="1")
        run_main(w, monkeypatch, 2.5)
        polls = len(q.polls())
    assert 2 <= polls <= 4, f"expected about one poll a second, saw {polls}"


def test_the_poll_sleep_is_interruptible(load, skills, stub_queue, monkeypatch):
    with stub_queue(jobs=[]) as q:
        w = load_worker(load, skills, queue_url=q.url, POLL_INTERVAL="30")
        started = time.time()
        run_main(w, monkeypatch, 0.5)
    assert time.time() - started < 5, "SIGTERM must not wait out the poll interval"


def test_poll_interval_has_a_floor(load, skills):
    """0 would be the spin again."""
    w = load_worker(load, skills, POLL_INTERVAL="0")
    assert w.POLL_INTERVAL >= 1


def test_poll_sends_worker_wait_and_explicit_lease(load, skills, stub_queue):
    with stub_queue(jobs=[]) as q:
        w = load_worker(load, skills, queue_url=q.url, WORKER_NAME="mac")
        w.lease_next()
        query = q.polls()[0]
    assert query["worker"] == ["mac"]
    assert query["wait"] == ["1"]
    assert query["lease"] == ["120"]


def test_heartbeat_names_the_worker_in_the_body_and_asks_for_the_lease(
    load, skills, stub_queue
):
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url, WORKER_NAME="mac")
        w.heartbeat("j1")
        beat = [b for m, p, b in q.calls if p == "/jobs/j1/heartbeat"][0]
        assert q.last_query == {"lease": ["120"]}
    assert beat == {"worker": "mac"}


def test_a_lost_lease_stops_the_heartbeats_and_names_the_job(
    load, tmp_path, stub_queue, capsys
):
    root = tmp_path / "skills"
    make_skill(root, "slow", "import time; time.sleep(3.5); print('{}')")
    with stub_queue(heartbeat_status=409) as q:
        w = load_worker(load, root, queue_url=q.url, HEARTBEAT_INTERVAL="1")
        w.handle({"id": "job-xyz", "type": "slow", "params": {}}, w.load_skills(root))
        beats = [p for m, p, b in q.calls if p.endswith("/heartbeat")]
    out = capsys.readouterr().out
    assert len(beats) == 1, "keep heartbeating a job the queue already reissued?"
    assert "job-xyz" in out and "lease lost" in out


def test_a_409_on_complete_is_logged_and_not_fatal(load, skills, stub_queue, capsys):
    with stub_queue(complete_status=409) as q:
        w = load_worker(load, skills, queue_url=q.url)
        assert w.complete("job-abc", result={"ok": True}) is False
        w.handle({"id": "job-abc", "type": "good_skill", "params": {}},
                 w.load_skills(skills))      # must not raise
    out = capsys.readouterr().out
    assert "job-abc" in out and "lease lost" in out


def test_the_note_is_still_pushed_when_the_lease_was_lost(
    load, skills, stub_queue, monkeypatch
):
    """The skill wrote the note whether or not the queue recorded the result."""
    pushed = []
    with stub_queue(complete_status=409) as q:
        w = load_worker(load, skills, queue_url=q.url)
        monkeypatch.setattr(w, "notes_after", lambda result: pushed.append(result))
        w.handle({"id": "j1", "type": "good_skill", "params": {}}, w.load_skills(skills))
    assert len(pushed) == 1


def test_complete_returns_true_when_recorded(load, skills, stub_queue):
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url)
        assert w.complete("j1", result={}) is True


# --- --probe ---------------------------------------------------------------

def run_probe(w, skills_root, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["worker.py", "--probe"])
    return w.main()


def test_probe_passes_against_a_healthy_queue(load, skills, stub_queue, monkeypatch, capsys):
    skill = skills / "good_skill" / "skill.yaml"
    skill.write_text(
        "type: good_skill\nsummary: ok\nparams:\n  type: object\n  required: [x]\n"
        "  properties:\n    x: {type: string}\nproduces: [a]\n"
        "typical_duration_s: 5\nidempotent: true\n", encoding="utf-8")
    with stub_queue(token="test-token") as q:
        w = load_worker(load, skills, queue_url=q.url)
        assert run_probe(w, skills, monkeypatch) == 0
        paths = [(m, p) for m, p, _ in q.calls]
    out = capsys.readouterr().out
    assert "probe passed" in out
    assert "test-token" not in out
    assert "token      set" in out
    assert "refuses a request with no token" in out
    assert paths == [("GET", "/healthz"), ("GET", "/healthz")]


def test_probe_never_makes_a_call_the_queue_counts_as_the_mac_being_seen(
    load, skills, stub_queue, monkeypatch
):
    """A poll, heartbeat, completion or manifest publish flips queue_status to
    'awake'. A probe that did any of them would hide a dead worker."""
    with stub_queue(token="test-token", jobs=[{"id": "j", "type": "good_skill"}]) as q:
        w = load_worker(load, skills, queue_url=q.url, PUBLISH_MANIFEST="auto")
        run_probe(w, skills, monkeypatch)
        paths = {p for _, p, _ in q.calls}
        assert len(q.jobs) == 1, "the probe must not lease a job"
    assert paths == {"/healthz"}


def test_probe_sends_the_second_request_without_the_token(
    load, skills, stub_queue, monkeypatch
):
    with stub_queue(token="test-token") as q:
        w = load_worker(load, skills, queue_url=q.url)
        run_probe(w, skills, monkeypatch)
        assert q.token_seen is None


def test_probe_reports_a_refused_token_and_does_not_print_it(
    load, skills, stub_queue, monkeypatch, capsys
):
    with stub_queue(token="right") as q:
        w = load_worker(load, skills, queue_url=q.url, QUEUE_TOKEN="sekrit-wrong")
        assert run_probe(w, skills, monkeypatch) == 1
    out = capsys.readouterr().out
    assert "auth" in out and "401" in out
    assert "sekrit-wrong" not in out


def test_probe_reports_tls_separately(load, skills, not_tls, monkeypatch, capsys):
    w = load_worker(load, skills, queue_url=not_tls.url, QUEUE_TOKEN="sekrit")
    assert run_probe(w, skills, monkeypatch) == 1
    out = capsys.readouterr().out
    assert "TLS" in out and "reachability" not in out
    assert "sekrit" not in out


def test_probe_reports_reachability_separately(load, skills, monkeypatch, capsys):
    w = load_worker(load, skills, queue_url="http://127.0.0.1:1", QUEUE_TOKEN="sekrit")
    assert run_probe(w, skills, monkeypatch) == 1
    out = capsys.readouterr().out
    assert "reachability" in out and "TLS:" not in out
    assert "sekrit" not in out


def test_probe_fails_on_a_manifest_fi_hub_would_mis_validate(
    load, skills, stub_queue, monkeypatch, capsys
):
    (skills / "good_skill" / "skill.yaml").write_text(
        "type: good_skill\nsummary: ok\nparams:\n  type: object\n"
        "  required: [missing]\n  properties:\n    x: {type: 'null'}\n"
        "idempotent: true\nproduces: [a]\n", encoding="utf-8")
    with stub_queue(token="test-token") as q:
        w = load_worker(load, skills, queue_url=q.url)
        assert run_probe(w, skills, monkeypatch) == 1
    out = capsys.readouterr().out
    assert "typical_duration_s" in out
    assert "not declared in properties" in out
    assert "allowed:" in out


def test_the_real_manifests_validate(load, skills):
    """The skills Fi will actually call must declare every param, with types."""
    w = load_worker(load, skills, SKILLS_DIR=str(ROOT))
    real = w.load_skills(ROOT)
    assert {"transcribe_video", "summarize_transcript", "summarize_url"} <= set(real)
    for name, skill in real.items():
        assert w.validate_manifest(skill["manifest"]) == [], name


def test_check_stays_offline(load, skills, monkeypatch, capsys):
    """--check must not open a socket, whatever QUEUE_URL says."""
    w = load_worker(load, skills, queue_url="https://fi-hub.invalid/worker")

    def no_network(*a, **k):
        raise AssertionError("--check touched the network")

    monkeypatch.setattr(w.urllib.request, "urlopen", no_network)
    monkeypatch.setattr(sys, "argv", ["worker.py", "--check"])
    assert w.main() == 0


# --- ntfy leaves the Mac ---------------------------------------------------

def test_the_worker_no_longer_sends_pushes(load, tmp_path, stub_queue, monkeypatch):
    """Pushes are server-side now. Even with the old variable still set, a
    permanent failure must not reach ntfy."""
    root = tmp_path / "skills"
    make_skill(root, "permanent", SKILL_PERMANENT)
    with stub_queue() as q:
        w = load_worker(load, root, queue_url=q.url, NTFY_TOPIC="a-topic")
        seen = []
        real = w.urllib.request.urlopen

        def spy(req, *a, **k):
            seen.append(req.full_url)
            return real(req, *a, **k)

        monkeypatch.setattr(w.urllib.request, "urlopen", spy)
        w.handle({"id": "j1", "type": "permanent", "params": {}}, w.load_skills(root))
    assert not hasattr(w, "alert") and not hasattr(w, "NTFY_TOPIC")
    assert seen and all(u.startswith(q.url) for u in seen), seen


def test_ntfy_is_gone_from_the_worker_and_its_config():
    for rel in ("worker.py", "com.hermes.worker.plist", ".env.example"):
        assert "ntfy" not in (ROOT / rel).read_text(encoding="utf-8").lower(), rel


# ---------------------------------------------------------------------------
# notes — what is pushed
#
# fi-hub stores the FULL note and serves get_note and search_notes from it, so
# the body leaving this machine is the point. The old catalog's invariant, "the
# transcript body never leaves", is reversed on purpose; its test is rewritten
# below as the inverse. What still must hold: the path is confined to the vault
# before anything is read, kind follows the note's own keys, and what is sent
# is exactly the file.
# ---------------------------------------------------------------------------

ARTICLE_FRONT = """---
title: "An Article"
site: "example.com"
url: https://example.com/a
url_id: 0123456789a
published: 2025-03-04
source_type: article
fetch_method: curl+html
chars: 900
fetched_at: 2026-10-02T09:30:00+00:00
---
"""

PLAIN_FRONT = """---
title: "Neither"
url: https://example.com/n
---
"""


def write_note(vault, name, body="body text " * 50, front=FRONTMATTER):
    path = vault / "transcripts" / name
    path.write_text(front + "\n" + body, encoding="utf-8")
    return path


def test_push_row_has_exactly_the_seven_fields(cat, note):
    row = cat.note_push_row(note)
    assert set(row) == {"path", "markdown", "title", "url", "kind", "date", "sha256"}
    assert row["path"] == f"transcripts/{NOTE}"
    assert row["title"] == "Test Video"
    assert row["url"] == "http://example/watch?v=abc123"


def test_the_full_transcript_body_is_pushed(cat, note, stub_queue):
    """Inverse of the old invariant. Fi quotes from the body, so the body is sent,
    byte for byte, with its frontmatter and any summary block."""
    note_with_summary(note, "A careful summary.", "detailed")
    expected = note.read_text(encoding="utf-8")
    row = cat.note_push_row(note)
    assert row["markdown"] == expected
    assert "so the thing about this is" in row["markdown"]
    assert "A careful summary." in row["markdown"]
    assert row["sha256"] == hashlib.sha256(note.read_bytes()).hexdigest()


def test_the_body_reaches_the_queue_in_the_request(load, skills, vault, note, stub_queue):
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.publish_notes(w.scan_vault(w.note_push_row), full=True)
        sent = q.notes_pushes()[0]["notes"][0]
    assert sent["markdown"] == note.read_text(encoding="utf-8")
    assert "so the thing about this is" in sent["markdown"]


def test_sha256_is_of_the_file_bytes_not_the_decoded_text(cat, vault):
    # Non-ASCII and a stray CR: hashing re-encoded text would differ.
    p = vault / "transcripts" / "crlf.md"
    p.write_bytes(b'---\ntitle: "x"\nvideo_id: v\n---\n\ncaf\xc3\xa9 \xe2\x80\x94 \r\n')
    row = cat.note_push_row(p)
    assert row["sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()


def test_a_video_note_is_a_video_with_its_upload_date(cat, note):
    row = cat.note_push_row(note)
    assert row["kind"] == "video"
    assert row["date"] == "2026-01-01"


def test_an_article_is_an_article_dated_by_when_it_was_fetched(cat, vault):
    p = write_note(vault, "art.md", front=ARTICLE_FRONT)
    row = cat.note_push_row(p)
    assert row["kind"] == "article"
    assert row["date"] == "2026-10-02", "fetch date, not the 2025 publication date"


def test_kind_comes_from_the_raw_keys_not_the_folded_row(cat, vault):
    """note_row folds url_id into video_id and fetch_method into
    transcript_method so one shape serves both. Deriving kind from that would
    call every article a video."""
    p = write_note(vault, "art.md", front=ARTICLE_FRONT)
    folded = cat.note_row(p)
    assert folded["video_id"] and folded["transcript_method"]     # the trap
    assert cat.note_push_row(p)["kind"] == "article"


def test_a_note_with_neither_key_is_an_article_with_no_date(cat, vault):
    row = cat.note_push_row(write_note(vault, "n.md", front=PLAIN_FRONT))
    assert row["kind"] == "article"
    assert row["date"] == ""


def test_title_falls_back_to_the_file_name(cat, vault):
    p = write_note(vault, "untitled-thing.md", front="---\nvideo_id: v\n---\n")
    assert cat.note_push_row(p)["title"] == "untitled-thing"


def test_a_symlink_pointing_outside_the_vault_is_never_read(
    load, skills, vault, note, tmp_path, monkeypatch
):
    """Without the check, a link to ~/.ssh/anything.md is read and published."""
    secret = tmp_path / "outside" / "secret.md"
    secret.parent.mkdir()
    secret.write_text(FRONTMATTER + "\nTOP SECRET CONTENT", encoding="utf-8")
    (vault / "transcripts" / "evil.md").symlink_to(secret)

    w = load_worker(load, skills, VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
    opened = []
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes",
                        lambda self: (opened.append(self.resolve()), real(self))[1])

    pushed = w.scan_vault(w.note_push_row)
    rows = w.scan_vault()
    direct = w.note_push_row(vault / "transcripts" / "evil.md")

    assert secret.resolve() not in opened, "the outside file was opened"
    assert direct is None
    assert [r["path"] for r in pushed] == [f"transcripts/{NOTE}"]
    assert [r["path"] for r in rows] == [f"transcripts/{NOTE}"]
    assert "TOP SECRET" not in repr(pushed)


def test_a_symlink_into_the_vault_is_one_note_not_two(cat, vault, note):
    (vault / "transcripts" / "alias.md").symlink_to(note)
    assert [r["path"] for r in cat.scan_vault(cat.note_push_row)] == [f"transcripts/{NOTE}"]


def test_note_row_also_checks_the_path_before_reading(
    load, skills, vault, tmp_path, monkeypatch
):
    outside = tmp_path / "outside.md"
    outside.write_text(FRONTMATTER, encoding="utf-8")
    link = vault / "transcripts" / "link.md"
    link.symlink_to(outside)
    w = load_worker(load, skills, VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
    opened = []
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes",
                        lambda self: (opened.append(self.resolve()), real(self))[1])
    assert w.note_row(link) is None
    assert outside.resolve() not in opened


# ---------------------------------------------------------------------------
# notes — batching
# ---------------------------------------------------------------------------

def many_notes(vault, count, size=200):
    for i in range(count):
        write_note(vault, f"n{i:03d}.md", body="x" * size,
                   front=FRONTMATTER.replace("abc123", f"id{i:03d}"))


def test_at_most_ten_notes_per_request(load, skills, vault, stub_queue):
    many_notes(vault, 25)
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        assert w.publish_notes(w.scan_vault(w.note_push_row), full=False)
        sizes = [len(b["notes"]) for b in q.notes_pushes()]
    assert sizes == [10, 10, 5]


def test_batches_keep_the_vault_order(load, skills, vault, stub_queue):
    many_notes(vault, 12)
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.publish_notes(w.scan_vault(w.note_push_row), full=False)
        paths = [n["path"] for b in q.notes_pushes() for n in b["notes"]]
    assert paths == sorted(paths) and len(paths) == 12


def test_a_request_never_passes_200000_bytes(load, skills, vault, stub_queue):
    many_notes(vault, 5, size=90_000)
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.publish_notes(w.scan_vault(w.note_push_row), full=False)
        sizes = [len(b["notes"]) for b in q.notes_pushes()]
        raw = list(q.raw_sizes)
    assert sizes == [2, 2, 1]
    assert max(raw) <= 200_000


def test_the_size_estimate_is_the_real_request_size(load, skills, vault, stub_queue):
    many_notes(vault, 3, size=1000)
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        rows = w.scan_vault(w.note_push_row)
        w.publish_notes(rows, full=False)
        assert q.raw_sizes == [w._body_size(rows)]


def test_a_note_over_the_limit_goes_alone_and_is_accepted(
    load, skills, vault, stub_queue
):
    write_note(vault, "a-small.md", body="x" * 100,
               front=FRONTMATTER.replace("abc123", "small1"))
    write_note(vault, "b-huge.md", body="y" * 250_000,
               front=FRONTMATTER.replace("abc123", "huge1"))
    write_note(vault, "c-small.md", body="z" * 100,
               front=FRONTMATTER.replace("abc123", "small2"))
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        assert w.publish_notes(w.scan_vault(w.note_push_row), full=False)
        shape = [[n["path"].rsplit("/", 1)[1] for n in b["notes"]] for b in q.notes_pushes()]
    assert shape == [["a-small.md"], ["b-huge.md"], ["c-small.md"]]


def test_the_servers_limits_are_adopted(load, skills, vault, stub_queue):
    """limits in a response is the server telling us what to aim for."""
    many_notes(vault, 25)
    lim = {"recommended_batch_bytes": 200000, "recommended_batch_notes": 2,
           "max_request_bytes": 1000000, "max_batch_notes": 40}

    def responder(body):
        return 200, {"ok": True, "inserted": len(body["notes"]), "limits": lim}

    with stub_queue(notes_responder=responder) as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.publish_notes(w.scan_vault(w.note_push_row), full=False)
        sizes = [len(b["notes"]) for b in q.notes_pushes()]
    assert sizes == [10] + [2] * 7 + [1], "the first batch is sized before any response"


def test_recommended_limits_never_exceed_the_servers_hard_caps(load, skills):
    w = load_worker(load, skills)
    w.apply_limits({"limits": {"recommended_batch_bytes": 5_000_000,
                               "recommended_batch_notes": 500,
                               "max_request_bytes": 1_000_000, "max_batch_notes": 40}})
    assert w.LIMITS["bytes"] == 1_000_000 and w.LIMITS["notes"] == 40


def test_garbage_limits_are_ignored(load, skills):
    w = load_worker(load, skills)
    before = dict(w.LIMITS)
    w.apply_limits({"limits": {"recommended_batch_bytes": "lots",
                               "recommended_batch_notes": -3, "max_batch_notes": True}})
    w.apply_limits({"limits": "nope"})
    w.apply_limits(None)
    assert w.LIMITS == before


def test_the_notes_request_timeout_is_at_least_60_seconds(
    load, skills, vault, note, stub_queue
):
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        seen = {}
        real = w.call

        def spy(method, path, *a, **k):
            seen[path] = k.get("timeout")
            return real(method, path, *a, **k)

        w.call = spy
        w.publish_notes(w.scan_vault(w.note_push_row), full=True)
    assert seen["/notes"] >= 60 and seen["/notes/reconcile"] >= 60


def test_a_note_that_gets_413_alone_is_logged_skipped_and_not_retried(
    load, skills, vault, stub_queue, capsys
):
    many_notes(vault, 3)

    def responder(body):
        if any(n["path"].endswith("n001.md") for n in body["notes"]):
            return 413, {"error": "body over 1000000 bytes"}
        return 200, {"ok": True, "inserted": len(body["notes"])}

    with stub_queue(notes_responder=responder) as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        rows = w.scan_vault(w.note_push_row)
        assert w.publish_notes(rows, full=True), "a skip is not a failed push"
        n001 = [1 for b in q.notes_pushes() for n in b["notes"] if n["path"].endswith("n001.md")]
        kept = [k["path"] for k in q.reconciles()[0]["keep"]]
    out = capsys.readouterr().out
    assert "SKIPPING transcripts/n001.md" in out
    assert len(n001) >= 1 and len(n001) <= 2, "tried alone at most once more, never looped"
    assert len(kept) == 3, "still in the vault, so still kept"


def test_a_413_on_a_batch_splits_it_down_to_the_one_note(
    load, skills, vault, stub_queue
):
    many_notes(vault, 4)

    def responder(body):
        if len(body["notes"]) > 1:
            return 413, {"error": "at most 1 notes per request"}
        return 200, {"ok": True, "inserted": 1}

    with stub_queue(notes_responder=responder) as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        assert w.publish_notes(w.scan_vault(w.note_push_row), full=False)
        landed = {n["path"] for b in q.notes_pushes() if len(b["notes"]) == 1
                  for n in b["notes"]}
    assert len(landed) == 4


def test_a_note_too_big_for_any_request_is_skipped_without_being_sent(
    load, skills, vault, stub_queue
):
    write_note(vault, "enormous.md", body="q" * 1_100_000)
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        assert w.publish_notes(w.scan_vault(w.note_push_row), full=False)
        assert q.notes_pushes() == []


def test_index_failures_in_the_response_are_logged(
    load, skills, vault, note, stub_queue, capsys
):
    def responder(body):
        return 200, {"ok": True, "inserted": 1, "index_failed": 1,
                     "index_errors": [{"path": "transcripts/x.md", "error": "overloaded"}]}

    with stub_queue(notes_responder=responder) as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        assert w.publish_notes(w.scan_vault(w.note_push_row), full=False)
    assert "not indexed yet" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# notes — reconcile
# ---------------------------------------------------------------------------

def test_a_full_push_is_followed_by_a_reconcile_of_paths_and_hashes_only(
    load, skills, vault, note, stub_queue
):
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.publish_notes(w.scan_vault(w.note_push_row), full=True)
        rec = q.reconciles()
    assert len(rec) == 1
    assert rec[0]["worker"] == "mac"
    assert rec[0]["keep"] == [{"path": f"transcripts/{NOTE}",
                               "sha256": hashlib.sha256(note.read_bytes()).hexdigest()}]
    assert "markdown" not in repr(rec[0])


def test_a_single_note_push_never_reconciles(load, skills, vault, note, stub_queue):
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.publish_notes(w.scan_vault(w.note_push_row), full=False)
        assert q.reconciles() == []


def test_no_reconcile_after_a_batch_failed(load, skills, vault, stub_queue):
    """A crash or error mid-push must delete nothing."""
    many_notes(vault, 25)
    calls = []

    def responder(body):
        calls.append(len(body["notes"]))
        return (500, {"error": "boom"}) if len(calls) == 2 else (200, {"ok": True})

    with stub_queue(notes_responder=responder) as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        assert w.publish_notes(w.scan_vault(w.note_push_row), full=True) is False
        assert q.reconciles() == []
    assert len(calls) == 3, "an HTTP error on one batch does not stop the others"


def test_an_unreachable_queue_stops_the_push_and_reconciles_nothing(
    load, skills, vault
):
    many_notes(vault, 25)
    w = load_worker(load, skills, VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
    attempts = []
    real = w.call

    def spy(method, path, *a, **k):
        attempts.append(path)
        return real(method, path, *a, **k)

    w.call = spy
    assert w.publish_notes(w.scan_vault(w.note_push_row), full=True) is False
    assert attempts == ["/notes"], "no point trying 2 more batches of a dead queue"


def test_a_refused_token_stops_the_push(load, skills, vault, note, stub_queue):
    with stub_queue(token="right") as q:
        w = load_worker(load, skills, queue_url=q.url, QUEUE_TOKEN="wrong",
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        assert w.publish_notes(w.scan_vault(w.note_push_row), full=True) is False
        assert [p for _, p, _ in q.calls] == ["/notes"]


def test_a_stop_request_mid_push_reconciles_nothing(load, skills, vault, stub_queue):
    many_notes(vault, 25)
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        real = w.call

        def stopping(method, path, *a, **k):
            out = real(method, path, *a, **k)
            w.stop.set()
            return out

        w.call = stopping
        assert w.publish_notes(w.scan_vault(w.note_push_row), full=True) is False
        assert q.reconciles() == []


def test_reconcile_repeats_until_nothing_is_pending(load, skills, vault, note, stub_queue):
    with stub_queue(reconcile_pending=[40, 20, 0]) as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.publish_notes(w.scan_vault(w.note_push_row), full=True)
        assert len(q.reconciles()) == 3


def test_reconcile_gives_up_when_it_makes_no_progress(
    load, skills, vault, note, stub_queue, capsys
):
    with stub_queue(reconcile_pending=[7] * 50) as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.publish_notes(w.scan_vault(w.note_push_row), full=True)
        assert len(q.reconciles()) == 2
    assert "no progress" in capsys.readouterr().out


def test_an_empty_vault_is_never_reconciled(load, skills, vault, stub_queue):
    """fi-hub refuses an empty keep: it looks like a vault that failed to mount."""
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        assert w.publish_notes(w.scan_vault(w.note_push_row), full=True) is True
        assert q.calls == []


def test_missing_and_stale_notes_are_pushed_once_in_the_same_cycle(
    load, skills, vault, stub_queue
):
    many_notes(vault, 3)
    with stub_queue(reconcile_extra={"missing": ["transcripts/n001.md"],
                                     "stale": ["transcripts/n002.md",
                                               "transcripts/not-in-the-vault.md"]}) as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.publish_notes(w.scan_vault(w.note_push_row), full=True)
        pushes = [[n["path"] for n in b["notes"]] for b in q.notes_pushes()]
        assert len(q.reconciles()) == 1, "one pass: no second reconcile"
    assert pushes[0] == ["transcripts/n000.md", "transcripts/n001.md", "transcripts/n002.md"]
    assert pushes[1:] == [["transcripts/n001.md", "transcripts/n002.md"]]


def test_a_skipped_note_is_not_pushed_again_after_reconcile(
    load, skills, vault, stub_queue
):
    many_notes(vault, 2)

    def responder(body):
        if any(n["path"].endswith("n001.md") for n in body["notes"]):
            return 413, {"error": "too big"}
        return 200, {"ok": True}

    with stub_queue(notes_responder=responder,
                    reconcile_extra={"missing": ["transcripts/n001.md"]}) as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.publish_notes(w.scan_vault(w.note_push_row), full=True)
        tries = [1 for b in q.notes_pushes() for n in b["notes"] if n["path"].endswith("n001.md")]
    assert len(tries) <= 2


# ---------------------------------------------------------------------------
# notes — publishing, ported from the catalog tests
# ---------------------------------------------------------------------------

def test_publish_notes_sends_the_whole_vault(load, skills, vault, note, stub_queue):
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        assert w.publish_notes(w.scan_vault(w.note_push_row), full=True) is True
        sent = q.notes_pushes()
    assert len(sent) == 1
    assert sent[0]["worker"] == "mac"
    assert [n["path"] for n in sent[0]["notes"]] == [f"transcripts/{NOTE}"]


def test_publish_notes_404_is_not_fatal_and_stays_visible(
    load, skills, vault, note, stub_queue, capsys
):
    """A 404 here is a QUEUE_URL missing its /worker, which must keep showing in
    the log. The old catalog went quiet after the first 404; the new endpoint
    is not optional."""
    with stub_queue(notes_responder=lambda b: (404, {"error": "not found"})) as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        assert w.publish_notes(w.scan_vault(w.note_push_row), full=True) is False
        assert w.publish_notes(w.scan_vault(w.note_push_row), full=True) is False
        assert len(q.notes_pushes()) == 2 and q.reconciles() == []
    assert capsys.readouterr().out.count("push of 1 note(s) failed") == 2


def test_publish_notes_disabled_sends_nothing(load, skills, vault, note, stub_queue):
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="0")
        assert w.publish_notes(w.scan_vault(w.note_push_row), full=True) is True
        assert q.calls == []


def test_publish_notes_survives_an_unreachable_queue(load, skills, vault, note):
    w = load_worker(load, skills, VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
    assert w.publish_notes(w.scan_vault(w.note_push_row), full=True) is False


# ---------------------------------------------------------------------------
# notes — the per-job push
# ---------------------------------------------------------------------------

def test_notes_after_pushes_only_the_touched_note(load, skills, vault, note, stub_queue):
    write_note(vault, "other.md")
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.notes_after({"transcript_path": f"transcripts/{NOTE}"})
        sent = q.notes_pushes()
        assert q.reconciles() == [], "one note is not the whole vault"
    assert len(sent) == 1
    assert [n["path"] for n in sent[0]["notes"]] == [f"transcripts/{NOTE}"]


def test_notes_after_accepts_summary_path(load, skills, vault, note, stub_queue):
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.notes_after({"summary_path": str(note), "regenerated": True})
        assert len(q.notes_pushes()) == 1


def test_notes_after_accepts_the_note_path_summarize_url_returns(
    load, skills, vault, stub_queue
):
    """The old catalog_after never read note_path, so articles were only ever
    indexed by the next full push."""
    art = write_note(vault, "art.md", front=ARTICLE_FRONT)
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.notes_after({"note_path": str(art), "url": "https://example.com/a"})
        pushed = q.notes_pushes()[0]["notes"][0]
    assert pushed["kind"] == "article" and pushed["path"] == "transcripts/art.md"


def test_notes_after_refuses_a_path_outside_the_vault(
    load, skills, vault, note, stub_queue, tmp_path
):
    outside = tmp_path / "secret.md"
    outside.write_text(note.read_text(encoding="utf-8"), encoding="utf-8")
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.notes_after({"transcript_path": "../secret.md"})
        w.notes_after({"transcript_path": str(outside)})
        assert q.notes_pushes() == []


def test_notes_after_ignores_a_result_with_no_path(load, skills, vault, stub_queue):
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.notes_after({"ok": True})
        w.notes_after("not a dict")
        w.notes_after(None)
        assert q.notes_pushes() == []


def test_notes_after_swallows_an_unexpected_error(load, skills, vault, note, stub_queue):
    """The job is already reported done by the time this runs."""
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.note_push_row = lambda _: (_ for _ in ()).throw(RuntimeError("boom"))
        w.notes_after({"transcript_path": f"transcripts/{NOTE}"})
        assert q.notes_pushes() == []


SKILL_WRITES_NOTE = """
import json, sys
p = json.load(sys.stdin)
print(json.dumps({"transcript_path": p["path"], "ok": True}))
"""


def test_a_finished_job_pushes_its_note(load, tmp_path, vault, note, stub_queue):
    root = tmp_path / "skills"
    make_skill(root, "writes_note", SKILL_WRITES_NOTE)
    job = {"id": "j1", "type": "writes_note",
           "params": {"path": f"transcripts/{NOTE}"}}
    with stub_queue() as q:
        w = load_worker(load, root, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.handle(job, w.load_skills(root))
        completions, sent = q.completions(), q.notes_pushes()
    assert completions[0]["status"] == "done"
    assert [n["path"] for n in sent[0]["notes"]] == [f"transcripts/{NOTE}"]


def test_a_broken_notes_push_still_leaves_the_job_done(
    load, tmp_path, vault, note, stub_queue
):
    root = tmp_path / "skills"
    make_skill(root, "writes_note", SKILL_WRITES_NOTE)
    job = {"id": "j1", "type": "writes_note",
           "params": {"path": f"transcripts/{NOTE}"}}
    with stub_queue(notes_responder=lambda b: (500, {"error": "boom"})) as q:
        w = load_worker(load, root, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.handle(job, w.load_skills(root))
        completions = q.completions()
    assert len(completions) == 1
    assert completions[0]["status"] == "done"
    assert completions[0]["error"] is None


def test_the_note_is_pushed_even_when_the_lease_was_lost(
    load, tmp_path, vault, note, stub_queue
):
    """The skill wrote the note whether or not the queue recorded the result."""
    root = tmp_path / "skills"
    make_skill(root, "writes_note", SKILL_WRITES_NOTE)
    job = {"id": "j1", "type": "writes_note",
           "params": {"path": f"transcripts/{NOTE}"}}
    with stub_queue(complete_status=409) as q:
        w = load_worker(load, root, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.handle(job, w.load_skills(root))
        sent = q.notes_pushes()
    assert [n["path"] for n in sent[0]["notes"]] == [f"transcripts/{NOTE}"]


def test_check_lists_the_notes(load, skills, vault, note, capsys):
    w = load_worker(load, skills, VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
    assert w.check(w.load_skills(skills)) == 0
    out = capsys.readouterr().out
    assert "notes: 1 note(s)" in out
    assert f"transcripts/{NOTE}" in out


# ---------------------------------------------------------------------------
# notes and manifest — the wiring in main()
# ---------------------------------------------------------------------------

def test_startup_pushes_the_vault_and_reconciles(load, skills, vault, note, stub_queue):
    """Covers main(), not just publish_notes itself."""
    job = {"id": "j1", "type": "good_skill", "params": {}}
    with stub_queue(jobs=[job]) as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        sys.argv = ["worker.py", "--once"]
        assert w.main() == 0
        sent, rec = q.notes_pushes(), q.reconciles()
    assert [n["path"] for n in sent[0]["notes"]] == [f"transcripts/{NOTE}"]
    assert len(rec) == 1


def test_the_hourly_timer_republishes_the_manifest_and_the_vault(
    load, skills, vault, note, stub_queue, monkeypatch
):
    with stub_queue(manifest_status=200) as q:
        w = load_worker(load, skills, queue_url=q.url, VAULT_DIR=str(vault),
                        PUBLISH_CATALOG="auto", PUBLISH_MANIFEST="auto",
                        CATALOG_RECONCILE_S="1")
        run_main(w, monkeypatch, 3.5)
        manifests = [1 for m, p, _ in q.calls if p == "/manifest"]
        fulls = len(q.reconciles())
    assert len(manifests) >= 3 and fulls >= 3


def test_a_manifest_that_failed_at_launch_is_retried_until_it_lands(
    load, skills, vault, stub_queue, monkeypatch, capsys
):
    """Launched with no network: the first publish fails, and enqueue_job refuses
    until a manifest lands. Waiting for the next restart would be days."""
    with stub_queue(manifest_status=503) as q:
        w = load_worker(load, skills, queue_url=q.url, PUBLISH_CATALOG="0",
                        PUBLISH_MANIFEST="auto", CATALOG_RECONCILE_S="0")
        w.MANIFEST_RETRY_S = 1
        threading.Timer(1.6, lambda: setattr(q, "manifest_status", 200)).start()
        run_main(w, monkeypatch, 5.0)
        posts = [1 for m, p, _ in q.calls if p == "/manifest"]
    out = capsys.readouterr().out
    assert "will retry" in out
    assert "published manifest" in out
    after = out.split("published manifest")[1:]
    assert len(posts) >= 3
    assert len(after) == 1, "once it landed, no more retries"


def test_a_failed_startup_push_is_retried_without_waiting_an_hour(
    load, skills, vault, note, stub_queue, monkeypatch
):
    attempts = []

    def responder(body):
        attempts.append(1)
        return (503, {"error": "starting"}) if len(attempts) == 1 else (200, {"ok": True})

    with stub_queue(notes_responder=responder) as q:
        w = load_worker(load, skills, queue_url=q.url, VAULT_DIR=str(vault),
                        PUBLISH_CATALOG="auto", CATALOG_RECONCILE_S="3600")
        w.NOTES_RETRY_S = 1
        run_main(w, monkeypatch, 3.5)
        assert len(q.reconciles()) == 1, "the retry landed and reconciled"
    assert len(attempts) == 2


# ---------------------------------------------------------------------------
# Cloudflare's edge (found live at the first reload)
#
# Python's default User-Agent, `Python-urllib/3.x`, gets "403 error code: 1010"
# from Cloudflare before fi-hub is reached, so the token is never even looked
# at. Nothing in the stub-only suite could see it, hence a stub that behaves
# like the edge. Both the fix and the way it is reported are pinned.
# ---------------------------------------------------------------------------

def test_every_request_names_itself_not_python_urllib(load, skills, stub_queue):
    with stub_queue(manifest_status=200) as q:
        w = load_worker(load, skills, queue_url=q.url)
        w.call("GET", "/healthz")
        w.call("GET", "/jobs/next")
        w.call("POST", "/manifest", body={"worker": "mac", "skills": []})
        w.call("GET", "/healthz", auth=False)
        seen = list(q.user_agents)
    assert len(seen) == 4
    assert all(ua == "fi-skills-worker/1.0" for ua in seen), seen
    assert not any(ua.startswith("Python-urllib") for ua in seen)


def test_the_edge_that_blocks_the_default_agent_lets_the_worker_through(
    load, skills, stub_queue
):
    """Fails if the User-Agent is ever dropped: the stub then behaves as the
    live edge did and every call raises EdgeBlocked."""
    for style in ("json", "text"):
        with stub_queue(edge_block=style) as q:
            w = load_worker(load, skills, queue_url=q.url)
            status, body = w.call("GET", "/healthz")
        assert status == 200 and body["ok"] is True


def test_an_edge_403_is_its_own_error_whatever_the_body_style(load, skills, stub_queue):
    for style in ("json", "text"):
        with stub_queue(edge_block=style, edge_block_all=True) as q:
            w = load_worker(load, skills, queue_url=q.url)
            with pytest.raises(w.EdgeBlocked) as exc:
                w.call("GET", "/jobs/next")
        assert exc.value.status == 403
        assert "Cloudflare edge" in str(exc.value)
        assert not isinstance(exc.value, w.AuthError)


def test_a_403_that_is_not_cloudflare_stays_a_plain_queue_error(
    load, skills, stub_queue
):
    with stub_queue(notes_responder=lambda b: (403, {"error": "forbidden"})) as q:
        w = load_worker(load, skills, queue_url=q.url)
        with pytest.raises(w.QueueError) as exc:
            w.call("POST", "/notes", body={"notes": []})
    assert not isinstance(exc.value, w.EdgeBlocked)
    assert exc.value.status == 403


def test_main_loop_reports_an_edge_block_as_one(
    load, skills, stub_queue, monkeypatch, capsys
):
    with stub_queue(edge_block="json", edge_block_all=True) as q:
        w = load_worker(load, skills, queue_url=q.url)
        run_main(w, monkeypatch, 1.5)
    out = capsys.readouterr().out
    assert "blocked at the Cloudflare edge (User-Agent?)" in out
    assert "queue unavailable" not in out
    assert "refused the token" not in out


def test_probe_reports_an_edge_block_not_reachability(
    load, skills, stub_queue, monkeypatch, capsys
):
    with stub_queue(edge_block="json", edge_block_all=True) as q:
        w = load_worker(load, skills, queue_url=q.url, QUEUE_TOKEN="sekrit")
        assert run_probe(w, skills, monkeypatch) == 1
    out = capsys.readouterr().out
    assert out.count("blocked at the Cloudflare edge (User-Agent?)") == 2   # both calls
    assert "reachability" not in out and "auth:" not in out
    assert "sekrit" not in out


def test_the_failed_pushes_name_the_edge_too(load, skills, vault, note, stub_queue, capsys):
    with stub_queue(edge_block="text", edge_block_all=True) as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        assert w.publish_notes(w.scan_vault(w.note_push_row), full=True) is False
        assert q.reconciles() == []
    assert "Cloudflare edge" in capsys.readouterr().out
