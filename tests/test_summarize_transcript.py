"""summarize_transcript/run.py

The guarded behaviours, in rough order of how much a regression would hurt:

  1. A re-run must not clobber or stack a summary  (silent data loss)
  2. A path outside the vault must be refused      (arbitrary file read)
  3. An existing same-style summary must be reused (cost: ~30s of GPU per job)
  4. The model must see the transcript, not a previous summary
"""

import io
import json
import urllib.error
from email.message import Message

import pytest

from conftest import FRONTMATTER, TRANSCRIPT, note_legacy, note_with_summary
from skilllib import notefmt

SKILL = "summarize_transcript/run.py"


@pytest.fixture
def sm(load, vault):
    return load(SKILL, {"VAULT_DIR": str(vault), "JOB_TIMEOUT": "1800"})


# ---------------------------------------------------------------------------
# read_note — untrusted path handling
# ---------------------------------------------------------------------------

def test_read_note_accepts_vault_relative_path(sm, note):
    path, front, body = sm.read_note("transcripts/abc123-test-video.md")
    assert path == note.resolve()
    assert front.startswith("---")
    assert "so the thing" in body


def test_read_note_rejects_traversal(sm):
    """../../.ssh/id_ed25519 must not be readable. This is the security test."""
    with pytest.raises(SystemExit) as exc:
        sm.read_note("../../.ssh/id_ed25519")
    assert exc.value.code == 2, "vault escape must be permanent, not retried"


def test_read_note_rejects_absolute_path_outside_vault(sm, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        sm.read_note(str(outside))
    assert exc.value.code == 2


def test_read_note_missing_file_is_permanent(sm):
    with pytest.raises(SystemExit) as exc:
        sm.read_note("transcripts/nope.md")
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# write_back — layout and non-destructiveness
# ---------------------------------------------------------------------------

def test_write_back_puts_summary_above_transcript(sm, note):
    path, front, body = sm.read_note(str(note))
    sm.write_back(path, front, body, "NEW SUMMARY", "m", "detailed")
    out = note.read_text(encoding="utf-8")
    assert out.index("NEW SUMMARY") < out.index("so the thing")
    assert out.startswith("---"), "frontmatter must stay first"


def test_write_back_replaces_rather_than_stacks(sm, note):
    note_with_summary(note, "FIRST", "detailed")
    path, front, body = sm.read_note(str(note))
    sm.write_back(path, front, body, "SECOND", "m", "detailed")
    out = note.read_text(encoding="utf-8")
    assert out.count(notefmt.SUM_BEGIN) == 1
    assert "FIRST" not in out and "SECOND" in out


def test_write_back_preserves_the_transcript(sm, note):
    note_with_summary(note, "S", "detailed")
    path, front, body = sm.read_note(str(note))
    sm.write_back(path, front, body, "S2", "m", "detailed")
    assert "so the thing" in note.read_text(encoding="utf-8")


def test_write_back_leaves_no_temp_files(sm, note):
    path, front, body = sm.read_note(str(note))
    sm.write_back(path, front, body, "S", "m", "detailed")
    assert list(note.parent.glob("*.tmp")) == [], "atomic write must clean up"


def test_write_back_migrates_a_legacy_note(sm, note):
    note_legacy(note, "LEGACY SUMMARY")
    path, front, body = sm.read_note(str(note))
    sm.write_back(path, front, body, "LEGACY SUMMARY", "m", "detailed")
    out = note.read_text(encoding="utf-8")
    assert out.count("LEGACY SUMMARY") == 1
    assert out.index("LEGACY SUMMARY") < out.index("so the thing")
    assert notefmt.SUM_BEGIN in out


def _http_error(code):
    return urllib.error.HTTPError("u", code, "msg", Message(), io.BytesIO(b"detail"))


# ---------------------------------------------------------------------------
# main — the skip guard and param validation
# ---------------------------------------------------------------------------

# run.py rejects a generation under 40 chars as a failed one, so fakes must clear it.
GENERATED = "GENERATED SUMMARY TEXT that is comfortably over the minimum length."


def run_main(sm, monkeypatch, capsys, params, summary=GENERATED):
    monkeypatch.setattr(sm.llm, "check_model", lambda c: None)
    monkeypatch.setattr(sm.llm, "generate", lambda c, p, s, t: summary)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(params)))
    sm.main()
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_main_generates_when_no_summary_exists(sm, note, monkeypatch, capsys):
    out = run_main(sm, monkeypatch, capsys, {"path": str(note)})
    assert out["regenerated"] is True
    assert out["summary"] == GENERATED
    assert GENERATED in note.read_text(encoding="utf-8")


def test_main_skips_when_same_style_already_present(sm, note, monkeypatch, capsys):
    note_with_summary(note, "EXISTING", "detailed")
    out = run_main(sm, monkeypatch, capsys, {"path": str(note)})
    assert out["regenerated"] is False
    assert out["summary"] == "EXISTING"
    assert out["chunks"] == 0


def test_main_skip_never_calls_ollama(sm, note, monkeypatch, capsys):
    """The skip must be free — no generate call, and no /api/tags probe."""
    note_with_summary(note, "EXISTING", "detailed")

    def boom(*a, **k):
        raise AssertionError("Ollama must not be contacted on the skip path")

    monkeypatch.setattr(sm.llm, "check_model", boom)
    monkeypatch.setattr(sm.llm, "generate", boom)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"path": str(note)})))
    sm.main()
    assert json.loads(capsys.readouterr().out.strip())["regenerated"] is False


def test_main_regenerates_on_style_change(sm, note, monkeypatch, capsys):
    note_with_summary(note, "EXISTING", "detailed")
    out = run_main(sm, monkeypatch, capsys, {"path": str(note), "style": "bullets"})
    assert out["regenerated"] is True
    assert out["style"] == "bullets"


def test_main_regenerates_on_force(sm, note, monkeypatch, capsys):
    note_with_summary(note, "EXISTING", "detailed")
    out = run_main(sm, monkeypatch, capsys, {"path": str(note), "force": True})
    assert out["regenerated"] is True
    assert out["summary"] == GENERATED


def test_main_regenerates_when_style_stamp_missing(sm, note, monkeypatch, capsys):
    """Pre-stamp notes upgrade themselves on the next run."""
    block = f"{notefmt.SUM_BEGIN}\n## Summary\n\n<!-- generated 2026-01-01 by m -->\n\nOLD\n{notefmt.SUM_END}"
    note.write_text(FRONTMATTER + "\n" + block + "\n\n" + TRANSCRIPT, encoding="utf-8")
    out = run_main(sm, monkeypatch, capsys, {"path": str(note)})
    assert out["regenerated"] is True
    assert "style=detailed" in note.read_text(encoding="utf-8")


def test_main_never_summarises_its_own_summary(sm, note, monkeypatch, capsys):
    """The prompt must carry the transcript, not the previous summary."""
    note_with_summary(note, "PREVIOUS SUMMARY TEXT", "detailed")
    seen = {}

    def capture(cfg, prompt, system, timeout):
        seen["prompt"] = prompt
        return GENERATED

    monkeypatch.setattr(sm.llm, "check_model", lambda c: None)
    monkeypatch.setattr(sm.llm, "generate", capture)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"path": str(note), "force": True})))
    sm.main()
    assert "PREVIOUS SUMMARY TEXT" not in seen["prompt"]
    assert "so the thing" in seen["prompt"]


def test_main_write_back_false_leaves_note_alone(sm, note, monkeypatch, capsys):
    before = note.read_text(encoding="utf-8")
    out = run_main(sm, monkeypatch, capsys, {"path": str(note), "write_back": False})
    assert out["summary"] == GENERATED
    assert note.read_text(encoding="utf-8") == before


def test_main_rejects_unknown_style(sm, note, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"path": str(note), "style": "haiku"})))
    with pytest.raises(SystemExit) as exc:
        sm.main()
    assert exc.value.code == 2


def test_main_rejects_missing_path(sm, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    with pytest.raises(SystemExit) as exc:
        sm.main()
    assert exc.value.code == 2


def test_main_rejects_too_short_note(sm, vault, monkeypatch):
    tiny = vault / "transcripts" / "tiny.md"
    tiny.write_text(FRONTMATTER + "\nhi", encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"path": str(tiny)})))
    with pytest.raises(SystemExit) as exc:
        sm.main()
    assert exc.value.code == 2


def test_main_empty_model_output_is_transient(sm, note, monkeypatch):
    monkeypatch.setattr(sm.llm, "check_model", lambda c: None)
    monkeypatch.setattr(sm.llm, "generate", lambda c, p, s, t: "")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"path": str(note)})))
    with pytest.raises(SystemExit) as exc:
        sm.main()
    assert exc.value.code == 1, "a bad generation should be retried"


def test_main_model_param_overrides_default(sm, note, monkeypatch, capsys):
    out = run_main(sm, monkeypatch, capsys, {"path": str(note), "model": "other:7b"})
    assert out["model"] == "other:7b"


def test_result_json_is_the_last_stdout_line(sm, note, monkeypatch, capsys):
    """The worker parses the last stdout line — nothing may follow it."""
    monkeypatch.setattr(sm.llm, "check_model", lambda c: None)
    monkeypatch.setattr(sm.llm, "generate", lambda c, p, s, t: GENERATED)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"path": str(note)})))
    sm.main()
    lines = [l for l in capsys.readouterr().out.strip().splitlines() if l.strip()]
    assert json.loads(lines[-1])["summary_path"]


