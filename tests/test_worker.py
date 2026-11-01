"""worker.py

Guarded behaviours:

  1. Only declared job types run                   (the allowlist IS the boundary)
  2. exit 2 -> permanent, exit 1 -> transient      (a poisoned job must not cycle)
  3. A long job heartbeats                         (or the lease lapses under it)
  4. An unreachable queue backs off, never crashes (this laptop is often offline)
  5. The catalog never writes and never fails a job (it is a read-only replica)

Every test runs against an in-process stub queue, and VAULT_DIR always points
into tmp_path. Nothing reaches the VPS or the real vault.
"""

import socket
import ssl
import sys
import threading
import time
import urllib.error
from pathlib import Path

import pytest

from conftest import (
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
    # in, and the catalog is off by default: no test may read the real vault.
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
# catalog — extraction
#
# This parser is the worker's own copy of the note format. It must agree with
# the skills' copies on where a summary starts and ends, or the catalog
# indexes markup instead of prose.
# ---------------------------------------------------------------------------

NOTE = "abc123-test-video.md"


@pytest.fixture
def cat(load, skills, vault):
    """A worker pointed at a real (temporary) vault, catalog enabled."""
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
    # No style stamp to read, and the catalog must not invent one.
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


def test_scan_vault_indexes_notes_and_skips_the_rest(cat, note, vault):
    (vault / "transcripts" / "stray.md").write_text("no frontmatter", encoding="utf-8")
    rows = cat.scan_vault()
    assert [r["path"] for r in rows] == [f"transcripts/{NOTE}"]


def test_scan_vault_is_stable_across_runs(cat, note):
    """The push is re-sent on a timer; an unchanged vault must produce an
    identical payload, or the queue churns rows that did not change."""
    assert cat.scan_vault() == cat.scan_vault()


def test_scan_vault_on_a_missing_directory(load, skills, tmp_path):
    w = load_worker(load, skills, VAULT_DIR=str(tmp_path / "gone"),
                    PUBLISH_CATALOG="auto")
    assert w.scan_vault() == []


# ---------------------------------------------------------------------------
# catalog — publishing
# ---------------------------------------------------------------------------

def test_publish_catalog_sends_a_full_snapshot(load, skills, vault, note, stub_queue):
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.publish_catalog(w.scan_vault(), full=True)
        sent = q.catalogs()
    assert len(sent) == 1
    assert sent[0]["full"] is True
    assert sent[0]["worker"] == "mac"
    assert [n["path"] for n in sent[0]["notes"]] == [f"transcripts/{NOTE}"]


def test_publish_catalog_never_sends_the_transcript_body(
    load, skills, vault, note, stub_queue
):
    """The body stays on this machine. Only metadata and the summary leave."""
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.publish_catalog(w.scan_vault(), full=True)
        row = q.catalogs()[0]["notes"][0]
    assert "so the thing about this is" not in repr(row)


def test_publish_catalog_404_is_not_fatal_and_stops_retrying(
    load, skills, vault, note, stub_queue
):
    with stub_queue(catalog_status=404) as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.publish_catalog(w.scan_vault(), full=True)
        w.publish_catalog(w.scan_vault(), full=True)
        assert len(q.catalogs()) == 1


def test_publish_catalog_disabled_sends_nothing(load, skills, vault, note, stub_queue):
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="0")
        w.publish_catalog(w.scan_vault(), full=True)
        assert q.catalogs() == []


def test_publish_catalog_survives_an_unreachable_queue(load, skills, vault, note):
    w = load_worker(load, skills, VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
    w.publish_catalog(w.scan_vault(), full=True)   # must not raise


# ---------------------------------------------------------------------------
# catalog — the per-job push
# ---------------------------------------------------------------------------

def test_catalog_after_pushes_only_the_touched_note(
    load, skills, vault, note, stub_queue
):
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.catalog_after({"transcript_path": f"transcripts/{NOTE}"})
        sent = q.catalogs()
    assert len(sent) == 1
    assert sent[0]["full"] is False
    assert [n["path"] for n in sent[0]["notes"]] == [f"transcripts/{NOTE}"]


def test_catalog_after_accepts_summary_path(load, skills, vault, note, stub_queue):
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.catalog_after({"summary_path": str(note), "regenerated": True})
        assert len(q.catalogs()) == 1


def test_catalog_after_refuses_a_path_outside_the_vault(
    load, skills, vault, note, stub_queue, tmp_path
):
    outside = tmp_path / "secret.md"
    outside.write_text(note.read_text(encoding="utf-8"), encoding="utf-8")
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.catalog_after({"transcript_path": "../secret.md"})
        w.catalog_after({"transcript_path": str(outside)})
        assert q.catalogs() == []


def test_catalog_after_ignores_a_result_with_no_path(
    load, skills, vault, stub_queue
):
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.catalog_after({"ok": True})
        w.catalog_after("not a dict")
        w.catalog_after(None)
        assert q.catalogs() == []


def test_catalog_after_swallows_an_unexpected_error(
    load, skills, vault, note, stub_queue
):
    """The job is already reported done by the time this runs."""
    with stub_queue() as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.note_row = lambda _: (_ for _ in ()).throw(RuntimeError("boom"))
        w.catalog_after({"transcript_path": f"transcripts/{NOTE}"})
        assert q.catalogs() == []


SKILL_WRITES_NOTE = """
import json, sys
p = json.load(sys.stdin)
print(json.dumps({"transcript_path": p["path"], "ok": True}))
"""


def test_a_finished_job_updates_the_catalog(load, tmp_path, vault, note, stub_queue):
    root = tmp_path / "skills"
    make_skill(root, "writes_note", SKILL_WRITES_NOTE)
    job = {"id": "j1", "type": "writes_note",
           "params": {"path": f"transcripts/{NOTE}"}}
    with stub_queue() as q:
        w = load_worker(load, root, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.handle(job, w.load_skills(root))
        completions, sent = q.completions(), q.catalogs()
    assert completions[0]["status"] == "done"
    assert [n["path"] for n in sent[0]["notes"]] == [f"transcripts/{NOTE}"]


def test_a_broken_catalog_push_still_leaves_the_job_done(
    load, tmp_path, vault, note, stub_queue
):
    root = tmp_path / "skills"
    make_skill(root, "writes_note", SKILL_WRITES_NOTE)
    job = {"id": "j1", "type": "writes_note",
           "params": {"path": f"transcripts/{NOTE}"}}
    with stub_queue(catalog_status=500) as q:
        w = load_worker(load, root, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        w.handle(job, w.load_skills(root))
        completions = q.completions()
    assert len(completions) == 1
    assert completions[0]["status"] == "done"
    assert completions[0]["error"] is None


def test_check_lists_the_catalog(load, skills, vault, note, capsys):
    w = load_worker(load, skills, VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
    assert w.check(w.load_skills(skills)) == 0
    out = capsys.readouterr().out
    assert "catalog: 1 note(s)" in out
    assert f"transcripts/{NOTE}" in out


def test_startup_publishes_a_full_snapshot(load, skills, vault, note, stub_queue):
    """Covers the wiring in main(), not just publish_catalog itself."""
    job = {"id": "j1", "type": "good_skill", "params": {}}
    with stub_queue(jobs=[job]) as q:
        w = load_worker(load, skills, queue_url=q.url,
                        VAULT_DIR=str(vault), PUBLISH_CATALOG="auto")
        sys.argv = ["worker.py", "--once"]
        assert w.main() == 0
        sent = q.catalogs()
    assert sent[0]["full"] is True
    assert [n["path"] for n in sent[0]["notes"]] == [f"transcripts/{NOTE}"]


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
        monkeypatch.setattr(w, "catalog_after", lambda result: pushed.append(result))
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
