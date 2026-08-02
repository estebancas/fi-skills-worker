"""skilllib — the note format and the Ollama client.

This code was extracted after being written three times. What it protects:

  1. A summary block is found wherever it sits, in either layout — getting this
     wrong destroys a summary another skill wrote, silently.
  2. num_ctx and keep_alive reach Ollama on every call. num_ctx unset is the
     top cause of "it only summarised the beginning".
  3. A missing model is permanent; a down Ollama is transient. Confusing the
     two either dead-letters a recoverable job or cycles a doomed one forever.

Nothing here touches the network or a real vault.
"""

import io
import json
import urllib.error
from email.message import Message

import pytest

from skilllib import llm, notefmt

# ---------------------------------------------------------------------------
# notefmt — the sentinel protocol
# ---------------------------------------------------------------------------


def test_split_summary_finds_sentinel_block():
    body = "before\n<!-- summary:begin -->\nSUM\n<!-- summary:end -->\nafter"
    source, block = notefmt.split_summary(body)
    assert "SUM" not in source
    assert source.split() == ["before", "after"]
    assert "SUM" in block


def test_split_summary_returns_empty_when_absent():
    source, block = notefmt.split_summary("just a transcript")
    assert source == "just a transcript"
    assert block == ""


def test_split_summary_migrates_legacy_heading():
    """Notes written before sentinels get wrapped, not dropped."""
    source, block = notefmt.split_summary("transcript\n\n## Summary\n\nOLD SUMMARY\n")
    assert source == "transcript"
    assert "OLD SUMMARY" in block
    assert block.startswith(notefmt.SUM_BEGIN) and block.endswith(notefmt.SUM_END)


def test_split_summary_ignores_unterminated_sentinel():
    """A begin with no end is corrupt; treat the whole thing as source."""
    source, block = notefmt.split_summary(
        "transcript\n<!-- summary:begin -->\ntruncated"
    )
    assert block == ""
    assert "truncated" in source


def test_parse_block_reads_style_and_text():
    block = notefmt.make_block("THE TEXT", "gpt-oss:latest", "bullets")
    style, text = notefmt.parse_block(block)
    assert style == "bullets"
    assert text == "THE TEXT"


def test_parse_block_returns_none_style_for_unstamped_block():
    """Blocks written before style= was stamped regenerate once."""
    block = (f"{notefmt.SUM_BEGIN}\n## Summary\n\n"
             f"<!-- generated 2026-01-01 by m -->\n\nX\n{notefmt.SUM_END}")
    style, text = notefmt.parse_block(block)
    assert style is None
    assert text == "X"


def test_parse_block_survives_missing_metadata():
    assert notefmt.parse_block("no metadata here") == (None, "")


def test_split_frontmatter_separates_the_fenced_block():
    front, body = notefmt.split_frontmatter('---\ntitle: "x"\n---\nbody here\n')
    assert front.startswith("---") and 'title: "x"' in front
    assert body.strip() == "body here"


def test_split_frontmatter_without_frontmatter():
    front, body = notefmt.split_frontmatter("no frontmatter\n")
    assert front == ""
    assert body == "no frontmatter\n"


def test_existing_summary_absent_file(tmp_path):
    assert notefmt.existing_summary(tmp_path / "nope.md") == ""


def test_existing_summary_reads_sentinel_block(tmp_path):
    p = tmp_path / "n.md"
    p.write_text(
        f"text\n{notefmt.SUM_BEGIN}\nKEEP ME\n{notefmt.SUM_END}\n", encoding="utf-8"
    )
    assert "KEEP ME" in notefmt.existing_summary(p)


def test_existing_summary_wraps_legacy_heading(tmp_path):
    p = tmp_path / "n.md"
    p.write_text("text\n\n## Summary\n\nOLD\n", encoding="utf-8")
    block = notefmt.existing_summary(p)
    assert "OLD" in block
    assert block.startswith(notefmt.SUM_BEGIN)


def test_existing_summary_none_when_note_has_no_summary(tmp_path):
    p = tmp_path / "n.md"
    p.write_text("just a transcript\n", encoding="utf-8")
    assert notefmt.existing_summary(p) == ""


def test_slugify_strips_punctuation_and_lowercases():
    assert notefmt.slugify("Hello, World! Part 2") == "hello-world-part-2"


def test_slugify_falls_back_on_empty():
    assert notefmt.slugify("!!!") == "untitled"
    assert notefmt.slugify("") == "untitled"


def test_atomic_write_leaves_no_temp_files(tmp_path):
    p = tmp_path / "sub" / "n.md"
    notefmt.atomic_write(p, "content")
    assert p.read_text(encoding="utf-8") == "content"
    assert list(p.parent.glob("*.tmp")) == []


def test_compose_puts_summary_above_the_body():
    out = notefmt.compose("---\ntitle: x\n---", "BLOCK", "BODY")
    assert out.startswith("---")
    assert out.index("BLOCK") < out.index("BODY")


def test_compose_without_a_summary_block():
    out = notefmt.compose("---\ntitle: x\n---", "", "BODY")
    assert out.rstrip().endswith("BODY")


# ---------------------------------------------------------------------------
# llm — configuration
# ---------------------------------------------------------------------------

def cfg(**kw):
    base = dict(url="http://ollama.test", model="m:latest", num_ctx=32768,
                keep_alive="30m", chunk_chars=500)
    return llm.Config(**{**base, **kw})


def test_config_defaults_match_the_documented_sizing(monkeypatch):
    for key in ("OLLAMA_NUM_CTX", "OLLAMA_KEEP_ALIVE", "SUMMARY_CHUNK_CHARS",
                "OLLAMA_MODEL", "OLLAMA_URL"):
        monkeypatch.delenv(key, raising=False)
    c = llm.Config.from_env()
    assert c.num_ctx == 32768, "raising this changes the Metal budget on a 24GB Mac"
    assert c.chunk_chars == c.num_ctx * 4 // 2, "chunk size must derive from num_ctx"
    assert c.keep_alive == "30m"
    assert c.model == "gpt-oss:latest"


def test_config_model_override_wins(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "env:model")
    assert llm.Config.from_env("job:model").model == "job:model"
    assert llm.Config.from_env().model == "env:model"


# ---------------------------------------------------------------------------
# llm — transport
# ---------------------------------------------------------------------------

def _http_error(code):
    return urllib.error.HTTPError("u", code, "msg", Message(), io.BytesIO(b"detail"))


def test_generate_sends_num_ctx_and_keep_alive(monkeypatch):
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

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    c = cfg()
    assert llm.generate(c, "p", "s", 10) == "text"
    assert sent["body"]["options"]["num_ctx"] == c.num_ctx
    assert sent["body"]["keep_alive"] == c.keep_alive
    assert sent["body"]["stream"] is False
    assert sent["body"]["model"] == c.model


def test_generate_missing_model_is_permanent(monkeypatch):
    monkeypatch.setattr(
        llm.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(_http_error(404)),
    )
    with pytest.raises(llm.LLMError) as exc:
        llm.generate(cfg(), "p", "s", 10)
    assert exc.value.permanent is True, "a model that isn't pulled will never succeed"


def test_generate_unreachable_is_transient(monkeypatch):
    monkeypatch.setattr(
        llm.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("refused")),
    )
    with pytest.raises(llm.LLMError) as exc:
        llm.generate(cfg(), "p", "s", 10)
    assert exc.value.permanent is False, "Ollama down should retry, not dead-letter"


def test_generate_server_error_is_transient(monkeypatch):
    monkeypatch.setattr(
        llm.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(_http_error(500)),
    )
    with pytest.raises(llm.LLMError) as exc:
        llm.generate(cfg(), "p", "s", 10)
    assert exc.value.permanent is False


def test_check_model_rejects_a_model_that_is_not_installed(monkeypatch):
    payload = json.dumps({"models": [{"name": "other:latest"}]}).encode()

    class R:
        def read(self):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda *a, **k: R())
    with pytest.raises(llm.LLMError) as exc:
        llm.check_model(cfg())
    assert exc.value.permanent is True


# ---------------------------------------------------------------------------
# llm — chunking and map-reduce
# ---------------------------------------------------------------------------

def test_chunk_short_text_is_single_pass():
    assert len(llm.chunk("x" * 100, cfg())) == 1


def test_chunk_splits_long_text_with_overlap():
    c = cfg(chunk_chars=500)
    text = ". ".join(f"sentence number {i}" for i in range(400))
    chunks = llm.chunk(text, c)
    assert len(chunks) > 1
    assert all(x.strip() for x in chunks)
    # every chunk must fit the window it was sized for
    assert all(len(x) <= 500 + c.overlap for x in chunks)


def test_chunk_covers_the_whole_input():
    text = ". ".join(f"unique{i}" for i in range(300))
    joined = " ".join(llm.chunk(text, cfg(chunk_chars=400)))
    assert "unique0" in joined and "unique299" in joined, "no content dropped"


def budget():
    return 600.0


def test_summarise_single_chunk_makes_one_call(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "generate",
                        lambda c, p, s, t: calls.append(p) or "SUMMARY")
    text, n = llm.summarise(cfg(), "short transcript", "detailed", "Title", budget)
    assert (text, n) == ("SUMMARY", 1)
    assert len(calls) == 1
    assert "short transcript" in calls[0]


def test_summarise_map_reduces_long_input(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "generate",
                        lambda c, p, s, t: calls.append(p) or "PARTIAL")
    text, n = llm.summarise(
        cfg(chunk_chars=300), ". ".join(f"sent{i}" for i in range(300)),
        "brief", "T", budget,
    )
    assert n > 1
    assert len(calls) == n + 1, "one call per chunk, plus the merge"
    assert "Section notes" in calls[-1], "final call merges the partials"


def test_summarise_refuses_an_unknown_style():
    with pytest.raises(llm.LLMError) as exc:
        llm.summarise(cfg(), "text", "haiku", "T", budget)
    assert exc.value.permanent is True


def test_summarise_stops_when_the_budget_runs_out(monkeypatch):
    monkeypatch.setattr(llm, "generate", lambda c, p, s, t: "PARTIAL")
    with pytest.raises(llm.LLMError) as exc:
        llm.summarise(
            cfg(chunk_chars=300), ". ".join(f"sent{i}" for i in range(300)),
            "brief", "T", lambda: 10.0,
        )
    assert exc.value.permanent is False, "out of time should be retried"


def test_transcript_system_prompt_mentions_asr_noise():
    """The prompt is what makes unpunctuated captions summarise well."""
    assert "caption" in llm.SYSTEMS["transcript"].lower()


def test_article_system_prompt_warns_about_page_furniture():
    """Extracted HTML keeps nav and cookie banners the model must ignore."""
    assert "navigation" in llm.SYSTEMS["article"].lower()


def test_summarise_kind_selects_the_system_prompt(monkeypatch):
    seen = []
    monkeypatch.setattr(llm, "generate", lambda c, p, s, t: seen.append(s) or "OUT")
    llm.summarise(cfg(), "text", "brief", "T", budget, kind="article")
    assert seen[0] == llm.SYSTEMS["article"]
