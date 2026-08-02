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
import time
from pathlib import Path
from typing import NoReturn

# Skills run with their own directory as cwd, so put the repo root on the path
# before importing the shared modules.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from skilllib import llm, notefmt  # noqa: E402

VAULT = Path(os.environ.get("VAULT_DIR", Path.home() / "vault")).expanduser()

JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT", "1800"))
STARTED = time.time()

STYLES = llm.STYLES


def log(m: str) -> None:
    print(m, file=sys.stderr, flush=True)


def die(m: str, permanent: bool = False) -> NoReturn:
    """Always exits. NoReturn tells type checkers the caller stops here."""
    log(f"ERROR: {m}")
    sys.exit(2 if permanent else 1)


def remaining() -> float:
    return JOB_TIMEOUT - (time.time() - STARTED) - 20


# ---------------------------------------------------------------------------

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
    front, body = notefmt.split_frontmatter(text)
    return p, front, body.strip()


def write_back(
    path: Path, front: str, body: str, summary: str, model: str, style: str
) -> None:
    # Drop any previous summary rather than stacking them up on re-runs, and
    # put the new one above the transcript — otherwise you scroll past 17k
    # characters of captions to reach it.
    source, _ = notefmt.split_summary(body)
    block = notefmt.make_block(summary, model, style)
    notefmt.atomic_write(path, notefmt.compose(front, block, source))


def main() -> None:
    p = json.load(sys.stdin)
    raw = (p.get("path") or "").strip()
    if not raw:
        die("no path given", permanent=True)
    style = p.get("style", "detailed")
    if style not in STYLES:
        die(f"unknown style {style!r} (want: {', '.join(STYLES)})", permanent=True)

    cfg = llm.Config.from_env(p.get("model"))

    path, front, body = read_note(raw)
    title_m = re.search(r'^title:\s*"?(.*?)"?\s*$', front, re.M)
    title = title_m.group(1) if title_m else path.stem

    source, prior = notefmt.split_summary(body)  # don't summarise a previous summary
    if len(source) < 200:
        die("note is too short to summarise", permanent=True)

    # At-least-once delivery, and chaining transcribe -> summarise, both mean
    # this job can arrive for a note that already has its summary. Regenerating
    # costs ~30s of GPU for an identical result, so only redo the work when the
    # style changed or the caller asked for it.
    if prior and not p.get("force"):
        prior_style, prior_text = notefmt.parse_block(prior)
        if prior_text and prior_style == style:
            log(f"summary already present (style={style}) — skipping; "
                "pass force=true to regenerate")
            print(json.dumps({
                "summary": prior_text,
                "summary_path": str(path),
                "source_path": str(path),
                "source_chars": len(source),
                "model": cfg.model,
                "chunks": 0,
                "style": style,
                "regenerated": False,
            }))
            return

    try:
        llm.check_model(cfg)
        summary, n_chunks = llm.summarise(
            cfg, source, style, title, remaining, kind="transcript", log=log
        )
    except llm.LLMError as exc:
        die(str(exc), permanent=exc.permanent)

    if not summary or len(summary) < 40:
        die("model returned an empty summary")

    if p.get("write_back", True):
        write_back(path, front, body, summary, cfg.model, style)
        log(f"wrote summary into {path}")

    print(json.dumps({
        "summary": summary,
        "summary_path": str(path),
        "source_path": str(path),
        "source_chars": len(source),
        "model": cfg.model,
        "chunks": n_chunks,
        "style": style,
        "regenerated": True,
    }))


if __name__ == "__main__":
    main()
