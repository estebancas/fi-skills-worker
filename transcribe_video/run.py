#!/usr/bin/env python3
"""
~/skills-worker/transcribe_video/run.py

Params (JSON on stdin):
    source        YouTube URL (or any yt-dlp-supported URL)
    language      ISO-639-1 code or "auto"          (default: auto)
    force_whisper skip existing captions            (default: false)
    allow_long    bypass the duration guard         (default: false)

Result (JSON on last stdout line):
    {"transcript_path": ..., "method": "captions"|"whisper", "title": ...,
     "duration_s": ..., "video_id": ...}

Exit codes:
    0  success
    1  transient failure   (worker will retry)
    2  permanent failure   (worker will not retry)
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

VAULT = Path(os.environ.get("VAULT_DIR", Path.home() / "vault")).expanduser()
TRANSCRIPTS = VAULT / "transcripts"
MODEL = os.environ.get(
    "WHISPER_MODEL", str(Path.home() / "models" / "ggml-base.en.bin")
)
COOKIES_FROM_BROWSER = os.environ.get("COOKIES_FROM_BROWSER", "chrome")
COOKIES_FILE = os.environ.get("COOKIES_FILE", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT", "1800"))
# Processing seconds per second of audio, measured on this machine. Apple
# Silicon + base.en sits near 0.15-0.25; bump it if your estimates undershoot.
WHISPER_COST = float(os.environ.get("WHISPER_COST", "0.30"))
# Fraction of JOB_TIMEOUT we're willing to spend before refusing outright.
SAFETY = 0.8

STARTED = time.time()


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def die(msg: str, permanent: bool = False) -> NoReturn:
    """Always exits. NoReturn tells type checkers the caller stops here."""
    log(f"ERROR: {msg}")
    sys.exit(2 if permanent else 1)


def alert(title: str, body: str) -> None:
    """Best-effort push notification. Never fails the job."""
    if not NTFY_TOPIC:
        return
    try:
        import urllib.request

        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode(),
            headers={"Title": title, "Priority": "default"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        log(f"(ntfy failed: {exc})")


def ytdlp_base() -> list[str]:
    cmd = ["yt-dlp", "--no-playlist", "--no-progress"]
    if COOKIES_FILE:
        cmd += ["--cookies", COOKIES_FILE]
    elif COOKIES_FROM_BROWSER:
        cmd += ["--cookies-from-browser", COOKIES_FROM_BROWSER]
    return cmd


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# ---------------------------------------------------------------------------
# metadata + bot detection
# ---------------------------------------------------------------------------

BOT_MARKERS = (
    "confirm you're not a bot",
    "sign in to confirm",
    "not a bot",
    "cookies are no longer valid",
    "please sign in",
)


def probe(url: str) -> dict:
    """One metadata call. Tells us duration AND what captions exist."""
    proc = run(ytdlp_base() + ["--dump-single-json", "--skip-download", url])
    if proc.returncode != 0:
        err = proc.stderr.strip()
        low = err.lower()
        if any(m in low for m in BOT_MARKERS):
            alert(
                "Transcript blocked",
                "YouTube is asking for bot confirmation. Cookies may be stale — "
                "open Chrome, visit youtube.com while signed in, then requeue.",
            )
            die(f"bot check triggered; cookies stale or missing. {err[-400:]}")
        if "video unavailable" in low or "private video" in low:
            die(f"video unavailable: {err[-300:]}", permanent=True)
        die(f"metadata fetch failed: {err[-400:]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        die("could not parse yt-dlp metadata")


def pick_caption_track(meta: dict, want: str) -> tuple[str, str] | None:
    """Return (lang_code, kind) for the best available track, or None.

    Manual captions beat auto-generated ones: they're human-written and
    correctly punctuated, so we never fall back to whisper when they exist.
    """
    manual = meta.get("subtitles") or {}
    auto = meta.get("automatic_captions") or {}

    def match(tracks: dict) -> str | None:
        if not tracks:
            return None
        if want and want != "auto":
            for code in tracks:
                if code.split("-")[0].lower() == want.lower():
                    return code
            return None
        for code in tracks:
            if code.split("-")[0].lower() == "en":
                return code
        # No English — fall back to whatever the uploader's language is.
        native = meta.get("language")
        if native:
            for code in tracks:
                if code.split("-")[0].lower() == native.split("-")[0].lower():
                    return code
        return next(iter(tracks), None)

    code = match(manual)
    if code:
        return code, "manual"
    code = match(auto)
    if code:
        return code, "auto"
    return None


# ---------------------------------------------------------------------------
# duration guard
# ---------------------------------------------------------------------------

def guard_duration(meta: dict, allow_long: bool) -> None:
    """Refuse up front rather than dying at the timeout after 30 wasted minutes."""
    duration = meta.get("duration") or 0
    if not duration:
        log("WARNING: unknown duration, skipping guard")
        return

    estimate = duration * WHISPER_COST + 60  # +60s for download/convert
    budget = JOB_TIMEOUT * SAFETY
    hh = f"{duration // 3600}h{(duration % 3600) // 60:02d}m"

    log(f"duration {hh}, estimated whisper time {estimate / 60:.1f}min, "
        f"budget {budget / 60:.1f}min")

    if estimate <= budget:
        return

    msg = (
        f"{meta.get('title', 'video')} runs {hh}. Estimated transcription "
        f"{estimate / 60:.0f}min exceeds the {JOB_TIMEOUT // 60}min job timeout."
    )
    if allow_long:
        log(f"WARNING: {msg} — proceeding because allow_long=true")
        alert("Long transcription started", msg + " Running anyway.")
        return

    alert("Transcription refused", msg + " Requeue with allow_long=true, or raise JOB_TIMEOUT.")
    die(msg + " Requeue with allow_long=true to override.", permanent=True)


# ---------------------------------------------------------------------------
# caption path
# ---------------------------------------------------------------------------

TIMESTAMP = re.compile(r"^\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->")
TAGS = re.compile(r"<[^>]+>")


def srt_to_text(path: Path) -> str:
    """Strip cues and collapse the rolling duplicates auto-captions produce."""
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = TAGS.sub("", raw).strip()
        if not line or line.isdigit() or TIMESTAMP.match(line):
            continue
        if out and line == out[-1]:
            continue
        # Auto-captions scroll: each cue repeats the previous cue's tail.
        if out and out[-1].endswith(line):
            continue
        if out and line.startswith(out[-1]):
            out[-1] = line
            continue
        out.append(line)
    return "\n".join(out)


def fetch_captions(url: str, lang: str, kind: str, tmp: Path) -> str | None:
    flag = "--write-auto-subs" if kind == "auto" else "--write-subs"
    proc = run(
        ytdlp_base()
        + [
            "--skip-download", flag,
            "--sub-langs", lang,
            "--convert-subs", "srt",
            "-o", str(tmp / "%(id)s"),
            url,
        ]
    )
    files = sorted(tmp.glob("*.srt"))
    if proc.returncode != 0 or not files:
        log(f"caption download failed, falling back to whisper: {proc.stderr[-300:]}")
        return None
    text = srt_to_text(files[0])
    return text if len(text) > 40 else None


# ---------------------------------------------------------------------------
# whisper path
# ---------------------------------------------------------------------------

def whisper_transcribe(url: str, language: str, tmp: Path) -> str:
    if not Path(MODEL).is_file():
        die(f"whisper model not found at {MODEL}", permanent=True)

    log("no usable captions — downloading audio")
    proc = run(
        ytdlp_base()
        + [
            "-x", "--audio-format", "wav",
            "--postprocessor-args", "-ar 16000 -ac 1",
            "-o", str(tmp / "audio.%(ext)s"),
            url,
        ]
    )
    wav = tmp / "audio.wav"
    if proc.returncode != 0 or not wav.is_file():
        die(f"audio download failed: {proc.stderr[-400:]}")

    remaining = JOB_TIMEOUT - (time.time() - STARTED) - 30
    if remaining < 30:
        die("no time budget left for whisper", permanent=True)

    log(f"transcribing ({remaining / 60:.0f}min budget)")
    cmd = ["whisper-cli", "-m", MODEL, "-f", str(wav), "-otxt", "-of", str(tmp / "out")]
    if language and language != "auto":
        cmd += ["-l", language]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=remaining)
    except subprocess.TimeoutExpired:
        alert("Transcription timed out", f"whisper exceeded its budget on {url}")
        die("whisper exceeded the remaining time budget", permanent=True)

    txt = tmp / "out.txt"
    if proc.returncode != 0 or not txt.is_file():
        die(f"whisper failed: {(proc.stderr or proc.stdout)[-400:]}")
    return txt.read_text(encoding="utf-8", errors="replace").strip()


# ---------------------------------------------------------------------------

SUM_BEGIN = "<!-- summary:begin -->"
SUM_END = "<!-- summary:end -->"
LEGACY_MARK = "\n## Summary\n"


def existing_summary(dest: Path) -> str:
    """Pull the summary block out of a note we are about to overwrite.

    summarize_transcript writes into this same file. At-least-once delivery
    (an expired lease requeues the job) and re-asking Hermes for a video that
    already finished both land us here again — without this the summary is
    silently lost. Sentinels are the current format; the heading is the
    fallback for notes written before them.
    """
    if not dest.is_file():
        return ""
    prior = dest.read_text(encoding="utf-8", errors="replace")
    i = prior.find(SUM_BEGIN)
    if i != -1:
        j = prior.find(SUM_END, i)
        if j != -1:
            return prior[i:j + len(SUM_END)]
    i = prior.find(LEGACY_MARK)
    if i != -1:
        return f"{SUM_BEGIN}\n{prior[i:].strip()}\n{SUM_END}"
    return ""


def write_note(meta: dict, text: str, method: str, lang: str | None) -> Path:
    TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
    vid = meta.get("id", "unknown")
    slug = re.sub(r"[^\w\s-]", "", meta.get("title", "untitled"))[:80].strip()
    slug = re.sub(r"[\s_]+", "-", slug).lower() or "untitled"
    dest = TRANSCRIPTS / f"{vid}-{slug}.md"

    upload = meta.get("upload_date") or ""
    if len(upload) == 8:
        upload = f"{upload[:4]}-{upload[4:6]}-{upload[6:]}"

    front = [
        "---",
        f'title: "{meta.get("title", "").replace(chr(34), chr(39))}"',
        f'channel: "{meta.get("uploader", "")}"',
        f"video_id: {vid}",
        f"url: {meta.get('webpage_url', '')}",
        f"duration_s: {meta.get('duration', 0)}",
        f"upload_date: {upload}",
        f"transcript_method: {method}",
        f"transcript_language: {lang or 'unknown'}",
        f"transcribed_at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "---",
        "",
    ]

    keep = existing_summary(dest)
    if keep:
        log("preserving the existing summary block")
        keep = "\n" + keep + "\n\n"

    # Atomic: at-least-once delivery means this can run twice.
    with tempfile.NamedTemporaryFile(
        "w", dir=TRANSCRIPTS, delete=False, encoding="utf-8", suffix=".tmp"
    ) as tmp:
        tmp.write("\n".join(front) + keep + text + "\n")
        staged = tmp.name
    os.replace(staged, dest)
    return dest


def main(tmpdir: str) -> None:
    for tool in ("yt-dlp", "ffmpeg"):
        if not shutil.which(tool):
            die(f"{tool} not on PATH (launchd PATH may be missing Homebrew)", permanent=True)

    p = json.load(sys.stdin)
    url = (p.get("source") or "").strip()
    if not url:
        die("no source given", permanent=True)
    language = p.get("language", "auto")

    meta = probe(url)
    log(f"{meta.get('title', '?')} — {meta.get('uploader', '?')}")

    # (transcript, method, language) always travel together — binding them as one
    # value keeps "we have text but no method" unrepresentable.
    got: tuple[str, str, str | None] | None = None

    if not p.get("force_whisper"):
        track = pick_caption_track(meta, language)
        if track:
            code, kind = track
            log(f"found {kind} captions [{code}]")
            captions = fetch_captions(url, code, kind, Path(tmpdir))
            if captions:
                got = (captions, f"captions:{kind}", code)
        else:
            log("no caption tracks published for this video")

    if got is None:
        guard_duration(meta, bool(p.get("allow_long")))
        got = (whisper_transcribe(url, language, Path(tmpdir)), "whisper", language)

    text, method, used_lang = got
    if len(text) < 20:
        die("transcript came back empty")

    dest = write_note(meta, text, method, used_lang)
    log(f"wrote {dest} ({len(text)} chars via {method})")

    print(json.dumps({
        "transcript_path": str(dest),
        "method": method,
        "title": meta.get("title"),
        "channel": meta.get("uploader"),
        "duration_s": meta.get("duration"),
        "video_id": meta.get("id"),
        "chars": len(text),
    }))


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="ytx-") as workdir:
        main(workdir)
