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

SKILL = "summarize_transcript/run.py"


@pytest.fixture
def sm(load, vault):
    return load(SKILL, {"VAULT_DIR": str(vault), "JOB_TIMEOUT": "1800"})


# ---------------------------------------------------------------------------
# split_summary / parse_block — the sentinel protocol
# ---------------------------------------------------------------------------

def test_split_summary_finds_sentinel_block(sm):
    body = "before\n<!-- summary:begin -->\nSUM\n<!-- summary:end -->\nafter"
    source, block = sm.split_summary(body)
    assert "SUM" not in source
    assert source.split() == ["before", "after"]
    assert "SUM" in block


def test_split_summary_returns_empty_when_absent(sm):
    source, block = sm.split_summary("just a transcript")
    assert source == "just a transcript"
    assert block == ""


def test_split_summary_migrates_legacy_heading(sm):
    """Notes written before sentinels get wrapped, not dropped."""
    source, block = sm.split_summary("transcript\n\n## Summary\n\nOLD SUMMARY\n")
    assert source == "transcript"
    assert "OLD SUMMARY" in block
    assert block.startswith(sm.SUM_BEGIN) and block.endswith(sm.SUM_END)


def test_split_summary_ignores_unterminated_sentinel(sm):
    """A begin with no end is corrupt; treat the whole thing as source."""
    body = "transcript\n<!-- summary:begin -->\ntruncated"
    source, block = sm.split_summary(body)
    assert block == ""
    assert "truncated" in source


def test_parse_block_reads_style_and_text(sm):
    block = sm.make_block("THE TEXT", "gpt-oss:latest", "bullets")
    style, text = sm.parse_block(block)
    assert style == "bullets"
    assert text == "THE TEXT"


def test_parse_block_returns_none_style_for_unstamped_block(sm):
    """Blocks written before style= was stamped regenerate once."""
    block = f"{sm.SUM_BEGIN}\n## Summary\n\n<!-- generated 2026-01-01 by m -->\n\nX\n{sm.SUM_END}"
    style, text = sm.parse_block(block)
    assert style is None
    assert text == "X"


def test_parse_block_survives_missing_metadata(sm):
    style, text = sm.parse_block("no metadata here")
    assert (style, text) == (None, "")


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
# chunk
# ---------------------------------------------------------------------------

def test_chunk_short_text_is_single_pass(sm):
    assert len(sm.chunk("x" * 100)) == 1


def test_chunk_splits_long_text_with_overlap(load, vault):
    sm = load(SKILL, {"VAULT_DIR": str(vault), "SUMMARY_CHUNK_CHARS": "500"})
    text = ". ".join(f"sentence number {i}" for i in range(400))
    chunks = sm.chunk(text)
    assert len(chunks) > 1
    assert all(c.strip() for c in chunks)
    # every chunk must fit the window it was sized for
    assert all(len(c) <= 500 + sm.OVERLAP for c in chunks)


def test_chunk_covers_the_whole_input(load, vault):
    sm = load(SKILL, {"VAULT_DIR": str(vault), "SUMMARY_CHUNK_CHARS": "400"})
    text = ". ".join(f"unique{i}" for i in range(300))
    joined = " ".join(sm.chunk(text))
    assert "unique0" in joined and "unique299" in joined, "no content dropped"


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
    assert out.count(sm.SUM_BEGIN) == 1
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
    assert sm.SUM_BEGIN in out


# ---------------------------------------------------------------------------
# ollama transport
# ---------------------------------------------------------------------------

def _http_error(code):
    return urllib.error.HTTPError("u", code, "msg", Message(), io.BytesIO(b"detail"))


def test_ollama_sends_num_ctx_and_keep_alive(sm, monkeypatch):
    """num_ctx unset is the top cause of silently truncated summaries."""
    sent = {}

    def fake_urlopen(req, timeout=None):
        sent["body"] = json.loads(req.data)

        class R:
            status = 200

            def read(self):
                return json.dumps({"response": "  text  "}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return R()

    monkeypatch.setattr(sm.urllib.request, "urlopen", fake_urlopen)
    assert sm.ollama("p", "s", 10) == "text"
    assert sent["body"]["options"]["num_ctx"] == sm.NUM_CTX
    assert sent["body"]["keep_alive"] == sm.KEEP_ALIVE
    assert sent["body"]["stream"] is False


def test_ollama_missing_model_is_permanent(sm, monkeypatch):
    monkeypatch.setattr(
        sm.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(_http_error(404)),
    )
    with pytest.raises(SystemExit) as exc:
        sm.ollama("p", "s", 10)
    assert exc.value.code == 2, "a model that isn't pulled will never succeed"


def test_ollama_unreachable_is_transient(sm, monkeypatch):
    monkeypatch.setattr(
        sm.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("refused")),
    )
    with pytest.raises(SystemExit) as exc:
        sm.ollama("p", "s", 10)
    assert exc.value.code == 1, "Ollama being down should retry, not dead-letter"


def test_ollama_server_error_is_transient(sm, monkeypatch):
    monkeypatch.setattr(
        sm.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(_http_error(500)),
    )
    with pytest.raises(SystemExit) as exc:
        sm.ollama("p", "s", 10)
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# summarise — single pass vs map-reduce
# ---------------------------------------------------------------------------

def test_summarise_single_chunk_makes_one_call(sm, monkeypatch):
    calls = []
    monkeypatch.setattr(sm, "ollama", lambda p, s, t: calls.append(p) or "SUMMARY")
    text, n = sm.summarise("short transcript", "detailed", "Title")
    assert (text, n) == ("SUMMARY", 1)
    assert len(calls) == 1
    assert "short transcript" in calls[0]


def test_summarise_map_reduces_long_input(load, vault, monkeypatch):
    sm = load(SKILL, {"VAULT_DIR": str(vault), "SUMMARY_CHUNK_CHARS": "300"})
    calls = []
    monkeypatch.setattr(sm, "ollama", lambda p, s, t: calls.append(p) or "PARTIAL")
    text, n = sm.summarise(". ".join(f"sent{i}" for i in range(300)), "brief", "T")
    assert n > 1
    assert len(calls) == n + 1, "one call per chunk, plus the merge"
    assert "Section notes" in calls[-1], "final call merges the partials"


def test_summarise_system_prompt_mentions_asr_noise(sm):
    """The prompt is what makes unpunctuated captions summarise well."""
    assert "caption" in sm.SYSTEM.lower()


# ---------------------------------------------------------------------------
# main — the skip guard and param validation
# ---------------------------------------------------------------------------

# run.py rejects a generation under 40 chars as a failed one, so fakes must clear it.
GENERATED = "GENERATED SUMMARY TEXT that is comfortably over the minimum length."


def run_main(sm, monkeypatch, capsys, params, summary=GENERATED):
    monkeypatch.setattr(sm, "check_model", lambda: None)
    monkeypatch.setattr(sm, "ollama", lambda p, s, t: summary)
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

    monkeypatch.setattr(sm, "check_model", boom)
    monkeypatch.setattr(sm, "ollama", boom)
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
    block = f"{sm.SUM_BEGIN}\n## Summary\n\n<!-- generated 2026-01-01 by m -->\n\nOLD\n{sm.SUM_END}"
    note.write_text(FRONTMATTER + "\n" + block + "\n\n" + TRANSCRIPT, encoding="utf-8")
    out = run_main(sm, monkeypatch, capsys, {"path": str(note)})
    assert out["regenerated"] is True
    assert "style=detailed" in note.read_text(encoding="utf-8")


def test_main_never_summarises_its_own_summary(sm, note, monkeypatch, capsys):
    """The prompt must carry the transcript, not the previous summary."""
    note_with_summary(note, "PREVIOUS SUMMARY TEXT", "detailed")
    seen = {}

    def capture(prompt, system, timeout):
        seen["prompt"] = prompt
        return GENERATED

    monkeypatch.setattr(sm, "check_model", lambda: None)
    monkeypatch.setattr(sm, "ollama", capture)
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
    monkeypatch.setattr(sm, "check_model", lambda: None)
    monkeypatch.setattr(sm, "ollama", lambda p, s, t: "")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"path": str(note)})))
    with pytest.raises(SystemExit) as exc:
        sm.main()
    assert exc.value.code == 1, "a bad generation should be retried"


def test_main_model_param_overrides_default(sm, note, monkeypatch, capsys):
    out = run_main(sm, monkeypatch, capsys, {"path": str(note), "model": "other:7b"})
    assert out["model"] == "other:7b"


def test_result_json_is_the_last_stdout_line(sm, note, monkeypatch, capsys):
    """The worker parses the last stdout line — nothing may follow it."""
    monkeypatch.setattr(sm, "check_model", lambda: None)
    monkeypatch.setattr(sm, "ollama", lambda p, s, t: GENERATED)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"path": str(note)})))
    sm.main()
    lines = [l for l in capsys.readouterr().out.strip().splitlines() if l.strip()]
    assert json.loads(lines[-1])["summary_path"]


def test_config_defaults_match_the_documented_sizing(load, vault):
    sm = load(SKILL, {"VAULT_DIR": str(vault)})
    assert sm.NUM_CTX == 32768, "raising this changes the Metal budget on a 24GB Mac"
    assert sm.CHUNK_CHARS == sm.NUM_CTX * 4 // 2, "chunk size must derive from num_ctx"
    assert sm.KEEP_ALIVE == "30m"
