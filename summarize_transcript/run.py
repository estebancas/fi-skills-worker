#!/usr/bin/env python3
"""
~/skills-worker/summarize_transcript/run.py

Summarises a transcript note in the vault using a local Ollama model.
Long transcripts are chunked and map-reduced so they fit the context window.

Params (JSON on stdin):
    path        vault-relative or absolute path to the note   (required)
    style       "brief" | "detailed" | "bullets"              (default: detailed)
    model       override OLLAMA_MODEL                         (optional)
    write_back  insert the summary into the note itself       (default: true)
    force       redo a summary that already exists            (default: false)

Result:
    {"summary": ..., "summary_path": ..., "model": ..., "chunks": N,
     "source_path": ..., "source_chars": N, "regenerated": bool}

A note that already carries a summary in the requested style is left alone and
its existing text returned, so re-delivered and chained jobs are cheap.

Exit codes: 0 ok / 1 transient / 2 permanent
"""

import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import NoReturn

VAULT = Path(os.environ.get("VAULT_DIR", Path.home() / "vault")).expanduser()
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gpt-oss:latest")
# The model actually in use. main() overrides it from the job's `model` param;
# bound here so importing this module (a test, a linter) doesn't hit a NameError.
MODEL = OLLAMA_MODEL

# Ollama silently truncates to 2048/4096 tokens unless num_ctx is set. This is
# the single most common cause of "it only summarised the beginning".
# 32k fits a full-length podcast in one pass on a 24 GB Mac: gpt-oss-20b is
# 13.8 GB of weights and its KV cache is ~0.8 GB here, against a ~16 GB Metal
# budget. Lower this if you move to a smaller machine.
NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "32768"))
# How long ollama holds the model in VRAM after a request. 13 GB stays
# resident for this long — shorten it if the Mac is also doing other work.
KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")
# Chars per chunk. ~4 chars/token, and we leave room for prompt + output.
CHUNK_CHARS = int(os.environ.get("SUMMARY_CHUNK_CHARS", str(NUM_CTX * 4 // 2)))
OVERLAP = 400

JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT", "1800"))
STARTED = time.time()

STYLES = {
    "brief": "Write 3-5 sentences capturing only the central argument.",
    "detailed": (
        "Write 250-400 words. Open with a one-sentence thesis, then cover the "
        "main points in the order they were made. Preserve specific claims, "
        "numbers, names, and tool references verbatim where they appear."
    ),
    "bullets": (
        "Write a tight bulleted list of the key takeaways, one line each, "
        "maximum 12 bullets. Lead each bullet with its subject."
    ),
}


def log(m: str) -> None:
    print(m, file=sys.stderr, flush=True)


def die(m: str, permanent: bool = False) -> NoReturn:
    """Always exits. NoReturn tells type checkers the caller stops here."""
    log(f"ERROR: {m}")
    sys.exit(2 if permanent else 1)


def remaining() -> float:
    return JOB_TIMEOUT - (time.time() - STARTED) - 20


# ---------------------------------------------------------------------------

def ollama(prompt: str, system: str, timeout: float) -> str:
    body = json.dumps({
        "model": MODEL,
        "prompt": prompt,
        "system": system,
        "stream": False,
        # Keep the model warm between chunks and between jobs: reloading 13 GB
        # costs more than holding it. Sent per-request because the worker runs
        # under launchd and may not inherit OLLAMA_KEEP_ALIVE from the session.
        "keep_alive": KEEP_ALIVE,
        "options": {"num_ctx": NUM_CTX, "temperature": 0.2},
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read()).get("response", "").strip()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        if exc.code == 404:
            die(f"model {MODEL!r} not pulled — run: ollama pull {MODEL}", permanent=True)
        die(f"ollama HTTP {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        die(f"ollama unreachable at {OLLAMA_URL} ({exc.reason}) — is it running?")
    except TimeoutError:
        die("ollama timed out")


def check_model() -> None:
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=10) as resp:
            tags = {m["name"] for m in json.loads(resp.read()).get("models", [])}
    except Exception as exc:
        die(f"cannot reach ollama at {OLLAMA_URL}: {exc}")
    if MODEL not in tags and f"{MODEL}:latest" not in tags:
        die(f"model {MODEL!r} not installed. Have: {', '.join(sorted(tags)) or 'none'}. "
            f"Run: ollama pull {MODEL}", permanent=True)


# ---------------------------------------------------------------------------

FRONTMATTER = re.compile(r"^---\n.*?\n---\n", re.DOTALL)


def read_note(raw_path: str) -> tuple[Path, str, str]:
    p = Path(raw_path).expanduser()
    if not p.is_absolute():
        p = VAULT / raw_path
    p = p.resolve()

    # Confine reads to the vault: this path arrives from an LLM.
    try:
        p.relative_to(VAULT.resolve())
    except ValueError:
        die(f"path escapes the vault: {p}", permanent=True)
    if not p.is_file():
        die(f"no such note: {p}", permanent=True)

    text = p.read_text(encoding="utf-8", errors="replace")
    front = ""
    m = FRONTMATTER.match(text)
    if m:
        front, text = m.group(0), text[m.end():]
    return p, front, text.strip()


def chunk(text: str) -> list[str]:
    if len(text) <= CHUNK_CHARS:
        return [text]
    out, start = [], 0
    while start < len(text):
        end = min(start + CHUNK_CHARS, len(text))
        if end < len(text):
            # Prefer a sentence or line break near the boundary.
            window = text.rfind(". ", start + CHUNK_CHARS // 2, end)
            if window == -1:
                window = text.rfind("\n", start + CHUNK_CHARS // 2, end)
            if window != -1:
                end = window + 1
        out.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - OVERLAP, start + 1)
    return out


SYSTEM = (
    "You summarise transcripts. The source is often machine-generated captions: "
    "lowercase, unpunctuated, with transcription errors. Read through the noise. "
    "Never invent facts not present in the text. Never mention that you are "
    "summarising, and never add a preamble — output only the summary itself."
)


def summarise(text: str, style: str, title: str) -> tuple[str, int]:
    chunks = chunk(text)
    log(f"{len(text)} chars → {len(chunks)} chunk(s), model {MODEL}")

    if len(chunks) == 1:
        prompt = (
            f"Transcript of: {title}\n\n{text}\n\n---\n{STYLES[style]}"
        )
        return ollama(prompt, SYSTEM, min(remaining(), 600)), 1

    partials = []
    for i, part in enumerate(chunks, 1):
        budget = remaining()
        if budget < 60:
            die("ran out of time budget mid-summarisation")
        log(f"  chunk {i}/{len(chunks)} ({budget / 60:.0f}min left)")
        prompt = (
            f"Section {i} of {len(chunks)} from a transcript of: {title}\n\n"
            f"{part}\n\n---\nList the substantive points made in this section, "
            "densely, one per line. Keep names, numbers, and specific claims."
        )
        partials.append(ollama(prompt, SYSTEM, min(budget, 400)))

    joined = "\n\n".join(f"[Section {i}]\n{s}" for i, s in enumerate(partials, 1))
    prompt = (
        f"Section notes from a transcript of: {title}\n\n{joined}\n\n---\n"
        f"{STYLES[style]} Merge the sections into one continuous summary; do not "
        "refer to section numbers."
    )
    return ollama(prompt, SYSTEM, min(remaining(), 600)), len(chunks)


SUM_BEGIN = "<!-- summary:begin -->"
SUM_END = "<!-- summary:end -->"
LEGACY_MARK = "\n## Summary\n"


def split_summary(text: str) -> tuple[str, str]:
    """Return (text without the summary, summary block).

    Sentinels let both this skill and transcribe_video find the block wherever
    it sits. Notes written before the sentinels existed have it appended at the
    end, so fall back to the heading and wrap what we find.
    """
    i = text.find(SUM_BEGIN)
    if i != -1:
        j = text.find(SUM_END, i)
        if j != -1:
            j += len(SUM_END)
            return (text[:i] + text[j:]).strip(), text[i:j]
    i = text.find(LEGACY_MARK)
    if i != -1:
        return text[:i].strip(), f"{SUM_BEGIN}\n{text[i:].strip()}\n{SUM_END}"
    return text.strip(), ""


BLOCK_META = re.compile(
    r"<!--\s*generated\s+(\S+)\s+by\s+(\S+)(?:\s+style=(\w+))?\s*-->"
)


def make_block(summary: str, model: str, style: str) -> str:
    stamp = time.strftime("%Y-%m-%d")
    return (
        f"{SUM_BEGIN}\n## Summary\n\n"
        f"<!-- generated {stamp} by {model} style={style} -->\n\n"
        f"{summary.strip()}\n{SUM_END}"
    )


def parse_block(block: str) -> tuple[str | None, str]:
    """Return (style, summary text) from an existing block.

    Style is None for blocks written before it was stamped, which makes them
    regenerate once so they pick it up.
    """
    m = BLOCK_META.search(block)
    if not m:
        return None, ""
    text = block[m.end():]
    end = text.find(SUM_END)
    if end != -1:
        text = text[:end]
    return m.group(3), text.strip()


def write_back(
    path: Path, front: str, body: str, summary: str, model: str, style: str
) -> None:
    # Drop any previous summary rather than stacking them up on re-runs, and
    # put the new one above the transcript — otherwise you scroll past 17k
    # characters of captions to reach it.
    source, _ = split_summary(body)
    block = make_block(summary, model, style)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, encoding="utf-8", suffix=".tmp"
    ) as tmp:
        tmp.write(front + "\n" + block + "\n\n" + source.rstrip() + "\n")
        staged = tmp.name
    os.replace(staged, path)


def main() -> None:
    p = json.load(sys.stdin)
    raw = (p.get("path") or "").strip()
    if not raw:
        die("no path given", permanent=True)
    style = p.get("style", "detailed")
    if style not in STYLES:
        die(f"unknown style {style!r} (want: {', '.join(STYLES)})", permanent=True)

    global MODEL
    MODEL = p.get("model") or OLLAMA_MODEL

    path, front, body = read_note(raw)
    title_m = re.search(r'^title:\s*"?(.*?)"?\s*$', front, re.M)
    title = title_m.group(1) if title_m else path.stem

    source, prior = split_summary(body)    # don't summarise a previous summary
    if len(source) < 200:
        die("note is too short to summarise", permanent=True)

    # At-least-once delivery, and chaining transcribe -> summarise, both mean
    # this job can arrive for a note that already has its summary. Regenerating
    # costs ~30s of GPU for an identical result, so only redo the work when the
    # style changed or the caller asked for it.
    if prior and not p.get("force"):
        prior_style, prior_text = parse_block(prior)
        if prior_text and prior_style == style:
            log(f"summary already present (style={style}) — skipping; "
                "pass force=true to regenerate")
            print(json.dumps({
                "summary": prior_text,
                "summary_path": str(path),
                "source_path": str(path),
                "source_chars": len(source),
                "model": MODEL,
                "chunks": 0,
                "style": style,
                "regenerated": False,
            }))
            return

    check_model()
    summary, n_chunks = summarise(source, style, title)
    if not summary or len(summary) < 40:
        die("model returned an empty summary")

    if p.get("write_back", True):
        write_back(path, front, body, summary, MODEL, style)
        log(f"wrote summary into {path}")

    print(json.dumps({
        "summary": summary,
        "summary_path": str(path),
        "source_path": str(path),
        "source_chars": len(source),
        "model": MODEL,
        "chunks": n_chunks,
        "style": style,
        "regenerated": True,
    }))


if __name__ == "__main__":
    main()
