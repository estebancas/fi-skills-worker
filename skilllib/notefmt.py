"""The vault note format.

One note holds both a source text and its summary, and two skills write to the
same file — so the summary is delimited by sentinels rather than by position,
and it sits *above* the source. Reading a note in Obsidian should not mean
scrolling past 17k characters of unpunctuated captions to reach the summary.

    ---
    title: "..."
    ---

    <!-- summary:begin -->
    ## Summary

    <!-- generated 2026-08-01 by gpt-oss:latest style=detailed -->

    ...summary...
    <!-- summary:end -->

    ...source text...

Notes written before the sentinels existed carry a trailing `## Summary`
heading instead; every reader here falls back to it, which migrates the note on
its next write.

This module was extracted after the same parsing had been written three times.
Getting it wrong costs a silently destroyed summary, so it lives in one place.
"""

import os
import re
import tempfile
import time
from pathlib import Path

SUM_BEGIN = "<!-- summary:begin -->"
SUM_END = "<!-- summary:end -->"
LEGACY_MARK = "\n## Summary\n"

FRONTMATTER = re.compile(r"^---\n.*?\n---\n", re.DOTALL)
BLOCK_META = re.compile(
    r"<!--\s*generated\s+(\S+)\s+by\s+(\S+)(?:\s+style=(\w+))?\s*-->"
)


def split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter including its fences, body). Frontmatter is "" if absent."""
    m = FRONTMATTER.match(text)
    if not m:
        return "", text
    return m.group(0), text[m.end():]


def split_summary(text: str) -> tuple[str, str]:
    """Return (text without the summary, summary block).

    The block is returned wrapped in sentinels even when it was found by the
    legacy heading, so callers can treat both layouts identically.
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


def existing_summary(path: Path) -> str:
    """The summary block in a note we are about to overwrite, or "".

    At-least-once delivery and re-asking for a source that already finished
    both land on an existing note. Without this the summary another skill wrote
    is silently lost.
    """
    if not path.is_file():
        return ""
    _, block = split_summary(path.read_text(encoding="utf-8", errors="replace"))
    return block


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


def slugify(title: str, limit: int = 80) -> str:
    slug = re.sub(r"[^\w\s-]", "", title or "")[:limit].strip()
    slug = re.sub(r"[\s_]+", "-", slug).lower()
    return slug or "untitled"


def atomic_write(path: Path, text: str) -> None:
    """Write via a temp file in the destination directory, then rename.

    Delivery is at-least-once, so every write here can happen twice. A rename
    within one filesystem is atomic: a reader sees the old note or the new one,
    never a half-written one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, encoding="utf-8", suffix=".tmp"
    ) as tmp:
        tmp.write(text)
        staged = tmp.name
    os.replace(staged, path)


def compose(front: str, block: str, body: str) -> str:
    """Assemble a note: frontmatter, then the summary, then the source text."""
    parts = [front.rstrip("\n"), ""]
    if block:
        parts += [block, ""]
    parts += [body.strip(), ""]
    return "\n".join(parts)
