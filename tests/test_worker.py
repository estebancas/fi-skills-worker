"""worker.py

Guarded behaviours:

  1. Only declared job types run                   (the allowlist IS the boundary)
  2. exit 2 -> permanent, exit 1 -> transient      (a poisoned job must not cycle)
  3. A long job heartbeats                         (or the lease lapses under it)
  4. An unreachable queue backs off, never crashes (this laptop is often offline)

Every test runs against an in-process stub queue. Nothing reaches the VPS.
"""

import sys
import time

import pytest

from conftest import (
    SKILL_NO_JSON,
    SKILL_OK,
    SKILL_PERMANENT,
    SKILL_SLOW,
    SKILL_TRANSIENT,
    make_skill,
)

WORKER = "worker.py"


@pytest.fixture
def skills(tmp_path):
    """A skills directory with one well-behaved skill."""
    root = tmp_path / "skills"
    make_skill(root, "good_skill", SKILL_OK)
    return root


def load_worker(load, skills_root, queue_url="http://127.0.0.1:1", **env):
    return load(WORKER, {
        "SKILLS_DIR": str(skills_root),
        "QUEUE_URL": queue_url,
        "QUEUE_TOKEN": "test-token",
        "POLL_WAIT": "1",
        "JOB_TIMEOUT": "30",
        "HEARTBEAT_INTERVAL": "30",
        "PUBLISH_MANIFEST": "0",
        "NTFY_TOPIC": "",
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
