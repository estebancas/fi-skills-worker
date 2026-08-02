"""Summarisation against a local Ollama.

Config is explicit rather than read from the environment at import time: this
module is shared and therefore cached in sys.modules, and the test suite
re-imports skills to pick up patched environments. Import-time config would
freeze the first test's settings into every later one.

Errors raise LLMError rather than calling sys.exit — a library should not
decide a process's exit code. Each skill catches it and maps .permanent onto
the exit-code contract (2 permanent, 1 transient).
"""

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

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

# The source text is described to the model differently depending on where it
# came from. Telling it to read through caption noise makes it hedge on clean
# article prose, and vice versa.
SYSTEMS = {
    "transcript": (
        "You summarise transcripts. The source is often machine-generated captions: "
        "lowercase, unpunctuated, with transcription errors. Read through the noise. "
        "Never invent facts not present in the text. Never mention that you are "
        "summarising, and never add a preamble — output only the summary itself."
    ),
    "article": (
        "You summarise articles and blog posts. The source is text extracted from a "
        "web page, so it may still carry navigation fragments, cookie notices, or "
        "subscription prompts — ignore those and summarise the article itself. "
        "Never invent facts not present in the text. Never mention that you are "
        "summarising, and never add a preamble — output only the summary itself."
    ),
}


class LLMError(Exception):
    """Something went wrong talking to the model.

    permanent=True means retrying is pointless — a missing model, a bad style.
    """

    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


@dataclass(frozen=True)
class Config:
    url: str
    model: str
    num_ctx: int
    keep_alive: str
    chunk_chars: int
    overlap: int = 400

    @classmethod
    def from_env(cls, model_override: str | None = None) -> "Config":
        # Ollama silently truncates to 2048/4096 tokens unless num_ctx is set.
        # This is the single most common cause of "it only summarised the
        # beginning". 32k fits a full-length podcast in one pass on a 24 GB
        # Mac: gpt-oss-20b is 13.8 GB of weights and its KV cache is ~0.8 GB
        # here, against a ~16 GB Metal budget.
        num_ctx = int(os.environ.get("OLLAMA_NUM_CTX", "32768"))
        return cls(
            url=os.environ.get("OLLAMA_URL", "http://localhost:11434"),
            model=model_override or os.environ.get("OLLAMA_MODEL", "gpt-oss:latest"),
            num_ctx=num_ctx,
            # How long ollama holds the model in VRAM after a request. 13 GB
            # stays resident for this long.
            keep_alive=os.environ.get("OLLAMA_KEEP_ALIVE", "30m"),
            # ~4 chars/token, leaving room for the prompt and the output.
            chunk_chars=int(
                os.environ.get("SUMMARY_CHUNK_CHARS", str(num_ctx * 4 // 2))
            ),
        )


def generate(cfg: Config, prompt: str, system: str, timeout: float) -> str:
    body = json.dumps({
        "model": cfg.model,
        "prompt": prompt,
        "system": system,
        "stream": False,
        # Keep the model warm between chunks and between jobs: reloading 13 GB
        # costs more than holding it. Sent per-request because the worker runs
        # under launchd and may not inherit OLLAMA_KEEP_ALIVE from the session.
        "keep_alive": cfg.keep_alive,
        "options": {"num_ctx": cfg.num_ctx, "temperature": 0.2},
    }).encode()
    req = urllib.request.Request(
        f"{cfg.url}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read()).get("response", "").strip()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        if exc.code == 404:
            raise LLMError(
                f"model {cfg.model!r} not pulled — run: ollama pull {cfg.model}",
                permanent=True,
            ) from exc
        raise LLMError(f"ollama HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(
            f"ollama unreachable at {cfg.url} ({exc.reason}) — is it running?"
        ) from exc
    except TimeoutError as exc:
        raise LLMError("ollama timed out") from exc


def check_model(cfg: Config) -> None:
    try:
        with urllib.request.urlopen(f"{cfg.url}/api/tags", timeout=10) as resp:
            tags = {m["name"] for m in json.loads(resp.read()).get("models", [])}
    except Exception as exc:
        raise LLMError(f"cannot reach ollama at {cfg.url}: {exc}") from exc
    if cfg.model not in tags and f"{cfg.model}:latest" not in tags:
        raise LLMError(
            f"model {cfg.model!r} not installed. Have: "
            f"{', '.join(sorted(tags)) or 'none'}. Run: ollama pull {cfg.model}",
            permanent=True,
        )


def chunk(text: str, cfg: Config) -> list[str]:
    if len(text) <= cfg.chunk_chars:
        return [text]
    out, start = [], 0
    while start < len(text):
        end = min(start + cfg.chunk_chars, len(text))
        if end < len(text):
            # Prefer a sentence or line break near the boundary.
            window = text.rfind(". ", start + cfg.chunk_chars // 2, end)
            if window == -1:
                window = text.rfind("\n", start + cfg.chunk_chars // 2, end)
            if window != -1:
                end = window + 1
        out.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - cfg.overlap, start + 1)
    return out


def summarise(
    cfg: Config,
    text: str,
    style: str,
    title: str,
    budget: Callable[[], float],
    *,
    kind: str = "transcript",
    log: Callable[[str], None] = lambda _: None,
) -> tuple[str, int]:
    """Summarise text, chunking and map-reducing when it does not fit.

    `budget` returns the seconds left in the job. It is called between chunks
    so a long map-reduce fails early rather than being killed mid-write.
    """
    if style not in STYLES:
        raise LLMError(
            f"unknown style {style!r} (want: {', '.join(STYLES)})", permanent=True
        )
    system = SYSTEMS.get(kind, SYSTEMS["transcript"])
    chunks = chunk(text, cfg)
    log(f"{len(text)} chars → {len(chunks)} chunk(s), model {cfg.model}")

    if len(chunks) == 1:
        prompt = f"{kind.capitalize()} of: {title}\n\n{text}\n\n---\n{STYLES[style]}"
        return generate(cfg, prompt, system, min(budget(), 600)), 1

    # At the current context size most sources fit in a single pass, which
    # produces a noticeably better summary than merging partials.
    partials = []
    for i, part in enumerate(chunks, 1):
        left = budget()
        if left < 60:
            raise LLMError("ran out of time budget mid-summarisation")
        log(f"  chunk {i}/{len(chunks)} ({left / 60:.0f}min left)")
        prompt = (
            f"Section {i} of {len(chunks)} from a {kind} of: {title}\n\n"
            f"{part}\n\n---\nList the substantive points made in this section, "
            "densely, one per line. Keep names, numbers, and specific claims."
        )
        partials.append(generate(cfg, prompt, system, min(left, 400)))

    joined = "\n\n".join(f"[Section {i}]\n{s}" for i, s in enumerate(partials, 1))
    prompt = (
        f"Section notes from a {kind} of: {title}\n\n{joined}\n\n---\n"
        f"{STYLES[style]} Merge the sections into one continuous summary; do not "
        "refer to section numbers."
    )
    return generate(cfg, prompt, system, min(budget(), 600)), len(chunks)
