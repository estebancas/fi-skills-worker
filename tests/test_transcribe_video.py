"""transcribe_video/run.py

Guarded behaviours:

  1. Re-transcribing must not destroy a summary another skill wrote (data loss)
  2. Captions must win over whisper when they exist            (20s vs 30min)
  3. Manual captions must win over auto-generated              (quality)
  4. A video too long for the timeout must fail in seconds     (wasted work)

yt-dlp and whisper are never invoked: every subprocess goes through `run()`,
which the tests replace.
"""

import json
import subprocess
from pathlib import Path

import pytest

SKILL = "transcribe_video/run.py"

META = {
    "id": "abc123",
    "title": "Test Video: A Study!",
    "uploader": "Chan",
    "duration": 600,
    "upload_date": "20260101",
    "webpage_url": "http://example/watch?v=abc123",
    "language": "en",
}


@pytest.fixture
def tv(load, vault, tmp_path):
    return load(SKILL, {
        "VAULT_DIR": str(vault),
        "JOB_TIMEOUT": "1800",
        "WHISPER_MODEL": str(tmp_path / "model.bin"),
        "NTFY_TOPIC": "",
    })


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


# ---------------------------------------------------------------------------
# srt_to_text — auto-caption cleanup
# ---------------------------------------------------------------------------

SRT = """1
00:00:00,000 --> 00:00:02,000
hello there

2
00:00:02,000 --> 00:00:04,000
hello there
this is a test

3
00:00:04,000 --> 00:00:06,000
<c.colorE5E5E5>this is a test</c>
and here is more
"""


def test_srt_to_text_strips_cues_and_tags(tv, tmp_path):
    f = tmp_path / "a.srt"
    f.write_text(SRT, encoding="utf-8")
    out = tv.srt_to_text(f)
    assert "-->" not in out and "<c." not in out
    assert not any(line.strip().isdigit() for line in out.splitlines())


def test_srt_to_text_collapses_rolling_duplicates(tv, tmp_path):
    """Auto-captions repeat the previous cue's tail; the note must not."""
    f = tmp_path / "a.srt"
    f.write_text(SRT, encoding="utf-8")
    out = tv.srt_to_text(f)
    assert out.count("hello there") == 1
    assert out.count("this is a test") == 1
    assert "and here is more" in out


def test_srt_to_text_handles_empty_file(tv, tmp_path):
    f = tmp_path / "empty.srt"
    f.write_text("", encoding="utf-8")
    assert tv.srt_to_text(f) == ""


# ---------------------------------------------------------------------------
# pick_caption_track
# ---------------------------------------------------------------------------

def test_manual_captions_beat_auto(tv):
    meta = {"subtitles": {"en": []}, "automatic_captions": {"en": []}}
    assert tv.pick_caption_track(meta, "auto") == ("en", "manual")


def test_falls_back_to_auto_when_no_manual(tv):
    meta = {"subtitles": {}, "automatic_captions": {"en-orig": []}}
    assert tv.pick_caption_track(meta, "auto") == ("en-orig", "auto")


def test_requested_language_is_honoured(tv):
    meta = {"subtitles": {"es": [], "en": []}, "automatic_captions": {}}
    assert tv.pick_caption_track(meta, "es") == ("es", "manual")


def test_requested_language_missing_falls_through_to_auto(tv):
    meta = {"subtitles": {"en": []}, "automatic_captions": {"es": []}}
    assert tv.pick_caption_track(meta, "es") == ("es", "auto")


def test_auto_prefers_english_then_uploader_language(tv):
    meta = {"subtitles": {"de": [], "en": []}, "automatic_captions": {}, "language": "de"}
    assert tv.pick_caption_track(meta, "auto") == ("en", "manual")
    meta = {"subtitles": {"de": [], "fr": []}, "automatic_captions": {}, "language": "de"}
    assert tv.pick_caption_track(meta, "auto") == ("de", "manual")


def test_no_tracks_returns_none(tv):
    assert tv.pick_caption_track({"subtitles": {}, "automatic_captions": {}}, "auto") is None


# ---------------------------------------------------------------------------
# guard_duration — refuse before wasting 30 minutes
# ---------------------------------------------------------------------------

def test_short_video_passes_the_guard(tv):
    tv.guard_duration({**META, "duration": 600}, allow_long=False)  # no raise


def test_long_video_is_refused_permanently(load, vault, tmp_path):
    tv = load(SKILL, {"VAULT_DIR": str(vault), "JOB_TIMEOUT": "600", "WHISPER_COST": "0.3"})
    with pytest.raises(SystemExit) as exc:
        tv.guard_duration({**META, "duration": 4 * 3600}, allow_long=False)
    assert exc.value.code == 2, "retrying a too-long video would waste the same 30 minutes"


def test_allow_long_overrides_the_guard(load, vault):
    tv = load(SKILL, {"VAULT_DIR": str(vault), "JOB_TIMEOUT": "600", "WHISPER_COST": "0.3"})
    tv.guard_duration({**META, "duration": 4 * 3600}, allow_long=True)  # no raise


def test_unknown_duration_skips_the_guard(tv):
    tv.guard_duration({**META, "duration": 0}, allow_long=False)  # no raise


# ---------------------------------------------------------------------------
# existing_summary — the anti-clobber check
# ---------------------------------------------------------------------------

def test_existing_summary_absent_file(tv, tmp_path):
    assert tv.existing_summary(tmp_path / "nope.md") == ""


def test_existing_summary_reads_sentinel_block(tv, tmp_path):
    p = tmp_path / "n.md"
    p.write_text(f"---\na: b\n---\n\n{tv.SUM_BEGIN}\nKEEP ME\n{tv.SUM_END}\n\nbody", encoding="utf-8")
    block = tv.existing_summary(p)
    assert "KEEP ME" in block
    assert block.startswith(tv.SUM_BEGIN) and block.endswith(tv.SUM_END)


def test_existing_summary_wraps_legacy_heading(tv, tmp_path):
    p = tmp_path / "n.md"
    p.write_text("---\na: b\n---\n\nbody\n\n## Summary\n\nOLD\n", encoding="utf-8")
    block = tv.existing_summary(p)
    assert "OLD" in block
    assert block.startswith(tv.SUM_BEGIN)


def test_existing_summary_none_when_note_has_no_summary(tv, tmp_path):
    p = tmp_path / "n.md"
    p.write_text("---\na: b\n---\n\njust a transcript", encoding="utf-8")
    assert tv.existing_summary(p) == ""


# ---------------------------------------------------------------------------
# write_note
# ---------------------------------------------------------------------------

def test_write_note_creates_frontmatter_and_body(tv):
    dest = tv.write_note(META, "the transcript", "captions:auto", "en")
    out = dest.read_text(encoding="utf-8")
    assert out.startswith("---\n")
    assert "video_id: abc123" in out
    assert "transcript_method: captions:auto" in out
    assert "upload_date: 2026-01-01" in out, "yyyymmdd must be normalised"
    assert out.rstrip().endswith("the transcript")


def test_write_note_slug_is_filesystem_safe(tv):
    dest = tv.write_note(META, "t", "captions:auto", "en")
    assert dest.name == "abc123-test-video-a-study.md"


def test_write_note_preserves_an_existing_summary(tv):
    """The bug this guards: os.replace silently dropping the summary."""
    dest = tv.write_note(META, "first transcript", "captions:auto", "en")
    dest.write_text(
        dest.read_text(encoding="utf-8").replace(
            "first transcript",
            f"{tv.SUM_BEGIN}\nMY SUMMARY\n{tv.SUM_END}\n\nfirst transcript",
        ),
        encoding="utf-8",
    )
    tv.write_note(META, "second transcript", "whisper", "en")
    out = dest.read_text(encoding="utf-8")
    assert "MY SUMMARY" in out, "re-transcribing must not clobber the summary"
    assert "second transcript" in out and "first transcript" not in out
    assert out.index("MY SUMMARY") < out.index("second transcript")


def test_write_note_is_idempotent(tv):
    a = tv.write_note(META, "t", "captions:auto", "en")
    b = tv.write_note(META, "t", "captions:auto", "en")
    assert a == b
    assert len(list(a.parent.glob("*.md"))) == 1


def test_write_note_leaves_no_temp_files(tv):
    dest = tv.write_note(META, "t", "captions:auto", "en")
    assert list(dest.parent.glob("*.tmp")) == []


def test_write_note_escapes_quotes_in_title(tv):
    dest = tv.write_note({**META, "title": 'He said "hi"'}, "t", "captions:auto", "en")
    line = [l for l in dest.read_text(encoding="utf-8").splitlines() if l.startswith("title:")][0]
    assert line.count('"') == 2, "an unescaped quote would break the YAML frontmatter"


# ---------------------------------------------------------------------------
# probe — yt-dlp failure classification
# ---------------------------------------------------------------------------

def test_probe_parses_metadata(tv, monkeypatch):
    monkeypatch.setattr(tv, "run", lambda *a, **k: completed(0, json.dumps(META)))
    assert tv.probe("http://x")["id"] == "abc123"


def test_probe_bot_check_is_transient(tv, monkeypatch):
    """Stale cookies are fixable; the job should come back, not dead-letter."""
    monkeypatch.setattr(
        tv, "run",
        lambda *a, **k: completed(1, "", "ERROR: Sign in to confirm you're not a bot"),
    )
    with pytest.raises(SystemExit) as exc:
        tv.probe("http://x")
    assert exc.value.code == 1


def test_probe_unavailable_video_is_permanent(tv, monkeypatch):
    monkeypatch.setattr(
        tv, "run", lambda *a, **k: completed(1, "", "ERROR: Video unavailable")
    )
    with pytest.raises(SystemExit) as exc:
        tv.probe("http://x")
    assert exc.value.code == 2, "a deleted video will never succeed"


def test_probe_unparseable_json_fails(tv, monkeypatch):
    monkeypatch.setattr(tv, "run", lambda *a, **k: completed(0, "not json"))
    with pytest.raises(SystemExit):
        tv.probe("http://x")


# ---------------------------------------------------------------------------
# cookies wiring — the launchd Keychain trap
# ---------------------------------------------------------------------------

def test_cookies_file_takes_precedence_over_browser(load, vault, tmp_path):
    tv = load(SKILL, {
        "VAULT_DIR": str(vault),
        "COOKIES_FILE": str(tmp_path / "cookies.txt"),
        "COOKIES_FROM_BROWSER": "chrome",
    })
    cmd = tv.ytdlp_base()
    assert "--cookies" in cmd
    assert "--cookies-from-browser" not in cmd, (
        "under launchd the browser path needs a Keychain unlock it cannot get"
    )


def test_cookies_from_browser_used_when_no_file(load, vault):
    tv = load(SKILL, {"VAULT_DIR": str(vault), "COOKIES_FILE": "",
                      "COOKIES_FROM_BROWSER": "chrome"})
    assert "--cookies-from-browser" in tv.ytdlp_base()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def test_main_requires_a_source(tv, monkeypatch, tmp_path):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    monkeypatch.setattr(tv.shutil, "which", lambda t: "/usr/bin/" + t)
    with pytest.raises(SystemExit) as exc:
        tv.main(str(tmp_path))
    assert exc.value.code == 2


def test_main_missing_tooling_is_permanent(tv, monkeypatch, tmp_path):
    """launchd PATH not including Homebrew is a config error, not a blip."""
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"source": "http://x"})))
    monkeypatch.setattr(tv.shutil, "which", lambda t: None)
    with pytest.raises(SystemExit) as exc:
        tv.main(str(tmp_path))
    assert exc.value.code == 2


def test_main_captions_path_end_to_end(tv, monkeypatch, tmp_path, capsys):
    """Captions available -> note written, whisper never touched."""
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"source": "http://x"})))
    monkeypatch.setattr(tv.shutil, "which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(tv, "probe", lambda url: {**META, "subtitles": {"en": []},
                                                  "automatic_captions": {}})
    monkeypatch.setattr(tv, "fetch_captions", lambda *a: "the caption text is long enough")

    def no_whisper(*a, **k):
        raise AssertionError("whisper must not run when captions exist")

    monkeypatch.setattr(tv, "whisper_transcribe", no_whisper)
    tv.main(str(tmp_path))
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["method"] == "captions:manual"
    assert Path(out["transcript_path"]).is_file()
    assert out["chars"] == len("the caption text is long enough")


def test_main_falls_back_to_whisper_without_captions(tv, monkeypatch, tmp_path, capsys):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"source": "http://x"})))
    monkeypatch.setattr(tv.shutil, "which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(tv, "probe", lambda url: {**META, "subtitles": {}, "automatic_captions": {}})
    monkeypatch.setattr(tv, "whisper_transcribe", lambda *a: "whisper produced this text")
    tv.main(str(tmp_path))
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["method"] == "whisper"


def test_main_force_whisper_skips_captions(tv, monkeypatch, tmp_path, capsys):
    import io
    monkeypatch.setattr("sys.stdin",
                        io.StringIO(json.dumps({"source": "http://x", "force_whisper": True})))
    monkeypatch.setattr(tv.shutil, "which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(tv, "probe", lambda url: {**META, "subtitles": {"en": []},
                                                  "automatic_captions": {}})

    def no_captions(*a, **k):
        raise AssertionError("force_whisper must skip the caption path")

    monkeypatch.setattr(tv, "fetch_captions", no_captions)
    monkeypatch.setattr(tv, "whisper_transcribe", lambda *a: "whisper produced this text")
    tv.main(str(tmp_path))
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["method"] == "whisper"


def test_main_rejects_an_empty_transcript(tv, monkeypatch, tmp_path):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"source": "http://x"})))
    monkeypatch.setattr(tv.shutil, "which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(tv, "probe", lambda url: {**META, "subtitles": {}, "automatic_captions": {}})
    monkeypatch.setattr(tv, "whisper_transcribe", lambda *a: "  ")
    with pytest.raises(SystemExit) as exc:
        tv.main(str(tmp_path))
    assert exc.value.code == 1


def test_main_takes_its_workdir_as_a_parameter(tv):
    """Regression: tmpdir used to be an implicit module global."""
    import inspect
    assert list(inspect.signature(tv.main).parameters) == ["tmpdir"]
