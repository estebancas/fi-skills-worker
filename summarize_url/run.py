#!/usr/bin/env python3
"""
~/skills-worker/summarize_url/run.py

Fetches an article or blog post, summarises it with the local Ollama model, and
writes a note into the vault — summary on top, extracted text below, the same
layout transcript notes use.

Params (JSON on stdin):
    source    the URL                                       (required)
    style     "brief" | "detailed" | "bullets"              (default: detailed)
    model     override OLLAMA_MODEL                         (optional)
    force     re-summarise when a summary already exists    (default: false)
    refetch   re-download even when the note exists         (default: false)

Result:
    {"note_path": ..., "url": ..., "title": ..., "site": ..., "chars": N,
     "summary": ..., "model": ..., "chunks": N, "style": ...,
     "regenerated": bool, "fetched": bool}

Exit codes: 0 ok / 1 transient / 2 permanent

Why curl and not urllib: this machine's Python has no root certificates
(`ssl.get_default_verify_paths().cafile` is None and the framework cert
directory is empty), so every HTTPS request from it fails to verify. curl uses
the macOS system trust store. Shelling out to a real tool for network work is
also what transcribe_video already does with yt-dlp.
"""

import hashlib
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import NoReturn

# Skills run with their own directory as cwd, so put the repo root on the path
# before importing the shared modules.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from skilllib import llm, notefmt  # noqa: E402

VAULT = Path(os.environ.get("VAULT_DIR", Path.home() / "vault")).expanduser()
TRANSCRIPTS = VAULT / "transcripts"

CURL = "/usr/bin/curl"
UA = os.environ.get("FETCH_USER_AGENT", (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
))
FETCH_TIMEOUT = int(os.environ.get("FETCH_TIMEOUT", "60"))
MAX_BYTES = int(os.environ.get("FETCH_MAX_BYTES", str(10 * 1024 * 1024)))
# Below this, extraction produced navigation chrome rather than an article —
# almost always a page that renders its content with JavaScript.
MIN_CHARS = int(os.environ.get("FETCH_MIN_CHARS", "500"))
ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE_URLS", "") not in ("", "0", "no", "false")

JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT", "1800"))
STARTED = time.time()

OK_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "text/markdown")

# X serves nothing useful without JavaScript, and its free embed endpoint
# returns a single post with no replies. Rejecting is more honest than
# capturing a thread's first line and calling it the thread.
X_HOSTS = {"x.com", "twitter.com", "mobile.twitter.com", "nitter.net"}

# Tailscale's range. Python's is_private covers it, but it is the specific
# network this machine is on, so it is named rather than implied.
CGNAT = ipaddress.ip_network("100.64.0.0/10")


def log(m: str) -> None:
    print(m, file=sys.stderr, flush=True)


def die(m: str, permanent: bool = False) -> NoReturn:
    """Always exits. NoReturn tells type checkers the caller stops here."""
    log(f"ERROR: {m}")
    sys.exit(2 if permanent else 1)


def remaining() -> float:
    return JOB_TIMEOUT - (time.time() - STARTED) - 20


# ---------------------------------------------------------------------------
# the URL, which arrives from a Telegram message via an LLM
# ---------------------------------------------------------------------------

TRACKING = re.compile(r"^(utm_\w+|fbclid|gclid|mc_[ce]id|igshid|si|ref_src|ref_url)$")


def normalise(raw: str) -> str:
    """Canonical form for identity: no fragment, no tracking parameters.

    Two pastes of the same article with different campaign tags must land on
    the same note, or the vault fills with duplicates.
    """
    u = urllib.parse.urlsplit(raw.strip())
    query = [(k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True)
             if not TRACKING.match(k)]
    return urllib.parse.urlunsplit((
        u.scheme.lower(),
        (u.netloc or "").lower(),
        u.path or "/",
        urllib.parse.urlencode(query),
        "",
    ))


def check_url(raw: str) -> str:
    """Validate and normalise, or exit. Returns the normalised URL."""
    if not raw:
        die("no source URL given", permanent=True)
    u = urllib.parse.urlsplit(raw.strip())
    if u.scheme not in ("http", "https"):
        die(f"unsupported scheme {u.scheme or '(none)'}:// — only http and https "
            "are fetched", permanent=True)
    host = (u.hostname or "").lower()
    if not host:
        die(f"no host in URL: {raw}", permanent=True)
    if host in X_HOSTS or host.removeprefix("www.") in X_HOSTS:
        die("X/Twitter is not supported: its public endpoint returns a single "
            "post with no replies, and the full thread needs an authenticated "
            "API. Paste an article or blog URL instead.", permanent=True)
    guard_host(host)
    return normalise(raw)


def guard_host(host: str) -> None:
    """Refuse anything that resolves inside this network.

    Without this, a URL from a chat message reaches Ollama on 127.0.0.1:11434
    and every service on the tailnet.
    """
    if ALLOW_PRIVATE:
        return
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        # DNS can also be down because this laptop is off the network.
        die(f"cannot resolve {host}: {exc}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip in CGNAT):
            die(f"{host} resolves to {ip}, which is inside this network — "
                "refusing to fetch it", permanent=True)


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------

# curl exit codes that will never succeed on a retry.
CURL_PERMANENT = {
    1: "unsupported protocol",
    3: "malformed URL",
    47: "too many redirects",
    63: f"page is larger than FETCH_MAX_BYTES ({MAX_BYTES})",
}


def fetch(url: str, timeout: int) -> tuple[int, str, str, bytes]:
    """Download a page. Returns (status, content_type, final_url, body)."""
    fd, tmp = tempfile.mkstemp(suffix=".page")
    os.close(fd)
    cmd = [
        CURL, "-sS", "-L", "--compressed",
        "--max-redirs", "5",
        "--max-time", str(timeout),
        "--max-filesize", str(MAX_BYTES),
        "--proto", "=http,https",
        "--proto-redir", "=http,https",
        "-A", UA,
        "-H", "Accept-Language: en;q=0.9,*;q=0.5",
        "-o", tmp,
        "-w", "%{http_code}\t%{content_type}\t%{url_effective}",
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15)
        if proc.returncode != 0:
            why = CURL_PERMANENT.get(proc.returncode)
            detail = (proc.stderr or "").strip()[:300]
            die(f"fetch failed ({why or f'curl exit {proc.returncode}'}): {detail}",
                permanent=why is not None)
        parts = (proc.stdout or "").split("\t")
        status = int(parts[0]) if parts and parts[0].isdigit() else 0
        ctype = parts[1] if len(parts) > 1 else ""
        final = parts[2] if len(parts) > 2 else url
        return status, ctype, final, Path(tmp).read_bytes()
    except subprocess.TimeoutExpired:
        die(f"fetch timed out after {timeout}s")
    except FileNotFoundError:
        die(f"{CURL} not found", permanent=True)
    finally:
        Path(tmp).unlink(missing_ok=True)


# 404 will not become a 200; a 403 is a paywall or a bot check, which a retry
# reproduces exactly. Everything else — 429, 5xx — is worth another attempt.
def check_response(status: int, ctype: str, final_url: str) -> None:
    if status in (401, 403):
        die(f"HTTP {status}: the page requires login or blocks automated "
            "fetching", permanent=True)
    if status in (404, 410):
        die(f"HTTP {status}: page not found", permanent=True)
    if status < 200 or status >= 300:
        die(f"HTTP {status} from {final_url}")

    base = ctype.split(";")[0].strip().lower()
    if base and base not in OK_TYPES:
        die(f"content type {base!r} is not text — only HTML and plain text are "
            "summarised", permanent=True)

    # -L can land somewhere else entirely; re-check before using the content.
    host = (urllib.parse.urlsplit(final_url).hostname or "").lower()
    if host:
        guard_host(host)


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------

DROP = {"script", "style", "noscript", "nav", "header", "footer", "aside",
        "form", "iframe", "svg", "button", "select", "template", "object",
        "canvas", "video", "audio", "picture"}
BLOCK = {"p", "div", "section", "article", "main", "li", "ul", "ol", "tr", "td",
         "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "figcaption",
         "dd", "dt", "table", "br", "hr"}
REGIONS = ("article", "main")

CHARSET_META = re.compile(rb"""<meta[^>]+charset=["']?\s*([\w-]+)""", re.I)


def decode(raw: bytes, ctype: str) -> str:
    """Header charset, then <meta charset>, then UTF-8 with replacement."""
    m = re.search(r"charset=([\w-]+)", ctype or "", re.I)
    candidates = [m.group(1)] if m else []
    m2 = CHARSET_META.search(raw[:4096])
    if m2:
        candidates.append(m2.group(1).decode("ascii", "replace"))
    for enc in candidates:
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


class Extractor(HTMLParser):
    """Pulls readable text and metadata out of an article page.

    No dependency is available for this — no readability, no trafilatura — so
    it is deliberately simple: drop the furniture, then prefer whatever sits
    inside <article>, falling back to <main> and then the whole document.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.meta: dict[str, str] = {}
        self.refresh: str = ""      # <meta http-equiv="refresh">, which curl -L ignores
        self.regions: dict[str, list[tuple[int, int]]] = {t: [] for t in REGIONS}
        self._open: dict[str, list[int]] = {t: [] for t in REGIONS}
        self._drop = 0
        self._in_title = False
        self._title: list[str] = []

    # -- metadata ----------------------------------------------------------
    def _meta_tag(self, attrs: dict[str, str]) -> None:
        content = attrs.get("content") or ""
        if (attrs.get("http-equiv") or "").lower() == "refresh" and not self.refresh:
            m = re.search(r"url\s*=\s*['\"]?([^'\";]+)", content, re.I)
            if m:
                self.refresh = m.group(1).strip()
            return
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if not key or not content:
            return
        mapping = {
            "og:title": "title",
            "og:site_name": "site",
            "article:published_time": "published",
            "datepublished": "published",
            "date": "published",
            "author": "author",
            "article:author": "author",
            "og:description": "description",
        }
        field = mapping.get(key)
        if field and field not in self.meta:
            self.meta[field] = content.strip()

    # -- parser hooks ------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        # Meta tags carry no visible text, so they are read even inside a
        # dropped element — a <meta refresh> commonly hides inside <noscript>.
        if tag == "meta":
            self._meta_tag({k.lower(): (v or "") for k, v in attrs})
            return
        if tag in DROP:
            self._drop += 1
            return
        if self._drop:
            return
        if tag == "title":
            self._in_title = True
            return
        if tag in REGIONS:
            self._open[tag].append(len(self.out))
        if tag in BLOCK:
            self.out.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag == "meta":
            self._meta_tag({k.lower(): (v or "") for k, v in attrs})
        elif tag == "br" and not self._drop:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in DROP:
            self._drop = max(0, self._drop - 1)
            return
        if self._drop:
            return
        if tag == "title":
            self._in_title = False
            return
        if tag in REGIONS and self._open[tag]:
            self.regions[tag].append((self._open[tag].pop(), len(self.out)))
        if tag in BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self._title.append(data)
            return
        if self._drop:
            return
        self.out.append(data)

    # -- results -----------------------------------------------------------
    def text(self) -> str:
        """The longest <article>, else the longest <main>, else everything."""
        best = ""
        for tag in REGIONS:
            for start, end in self.regions[tag]:
                candidate = clean("".join(self.out[start:end]))
                if len(candidate) > len(best):
                    best = candidate
            if len(best) >= MIN_CHARS:
                return best
        whole = clean("".join(self.out))
        return best if len(best) >= MIN_CHARS else whole

    def title(self) -> str:
        raw = self.meta.get("title") or "".join(self._title)
        # <title> is frequently wrapped across lines; frontmatter needs one.
        return re.sub(r"\s+", " ", raw).strip()


def clean(text: str) -> str:
    text = text.replace(" ", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract(raw: bytes, ctype: str, url: str) -> tuple[str, dict[str, str]]:
    base = ctype.split(";")[0].strip().lower()
    text_src = decode(raw, ctype)
    host = (urllib.parse.urlsplit(url).hostname or "").removeprefix("www.")

    if base in ("text/plain", "text/markdown"):
        body = clean(text_src)
        meta = {"title": Path(urllib.parse.urlsplit(url).path).stem or host,
                "site": host}
    else:
        parser = Extractor()
        parser.feed(text_src)
        parser.close()
        body = parser.text()
        meta = dict(parser.meta)
        meta["title"] = parser.title() or host
        meta.setdefault("site", host)
        if parser.refresh:
            meta["_refresh"] = urllib.parse.urljoin(url, parser.refresh)

    if "published" in meta:
        meta["published"] = meta["published"][:10]
    return body, meta


# ---------------------------------------------------------------------------
# the note
# ---------------------------------------------------------------------------

def load_page(url: str, timeout: int, hops: int = 2) -> tuple[str, dict[str, str]]:
    """Fetch and extract, following <meta refresh> that curl -L cannot see.

    A landing page that redirects this way extracts to "Click here to be
    redirected", which would otherwise be reported as a page needing
    JavaScript — a confusing diagnosis for what is really a moved article.
    """
    seen: set[str] = set()
    body, meta = "", {}
    for _ in range(max(1, hops)):
        status, ctype, final, raw = fetch(url, timeout)
        check_response(status, ctype, final)
        body, meta = extract(raw, ctype, final)
        nxt = meta.pop("_refresh", "")
        if not nxt or len(body) >= MIN_CHARS or nxt in seen:
            return body, meta
        seen.add(url)
        log(f"following meta refresh → {nxt}")
        url = check_url(nxt)
    return body, meta


def note_path(url: str, title: str) -> Path:
    """Stable for a URL, so the same article always lands on the same note."""
    url_id = hashlib.sha256(url.encode()).hexdigest()[:11]
    return TRANSCRIPTS / f"{url_id}-{notefmt.slugify(title)}.md"


def yaml_str(value: str) -> str:
    return value.replace('"', "'").replace("\n", " ").strip()


def frontmatter(url: str, meta: dict[str, str], chars: int) -> str:
    lines = [
        "---",
        f'title: "{yaml_str(meta.get("title", ""))}"',
        f'site: "{yaml_str(meta.get("site", ""))}"',
    ]
    if meta.get("author"):
        lines.append(f'author: "{yaml_str(meta["author"])}"')
    lines += [
        f"url: {url}",
        f"url_id: {hashlib.sha256(url.encode()).hexdigest()[:11]}",
    ]
    if meta.get("published"):
        lines.append(f"published: {meta['published']}")
    lines += [
        "source_type: article",
        "fetch_method: curl+html",
        f"chars: {chars}",
        f"fetched_at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "---",
    ]
    return "\n".join(lines)


def read_existing(dest: Path) -> tuple[str, str, str] | None:
    """Return (frontmatter, article text, summary block) for an existing note."""
    if not dest.is_file():
        return None
    text = dest.read_text(encoding="utf-8", errors="replace")
    front, body = notefmt.split_frontmatter(text)
    source, block = notefmt.split_summary(body)
    return front, source, block


# ---------------------------------------------------------------------------

def main() -> None:
    p = json.load(sys.stdin)
    style = p.get("style", "detailed")
    if style not in llm.STYLES:
        die(f"unknown style {style!r} (want: {', '.join(llm.STYLES)})", permanent=True)

    url = check_url((p.get("source") or "").strip())
    cfg = llm.Config.from_env(p.get("model"))
    force = bool(p.get("force"))
    refetch = bool(p.get("refetch"))

    # Delivery is at-least-once and re-asking is cheap, so look for the note
    # before spending a request on the network or 30s on the GPU.
    dest = None
    prior = None
    for candidate in TRANSCRIPTS.glob("*.md") if TRANSCRIPTS.is_dir() else []:
        head = candidate.read_text(encoding="utf-8", errors="replace")[:1200]
        if re.search(rf"^url:\s*{re.escape(url)}\s*$", head, re.M):
            dest, prior = candidate, read_existing(candidate)
            break

    fetched = False
    meta: dict[str, str] = {}
    if prior and not refetch:
        front, body, block = prior
        if block and not force:
            prior_style, prior_text = notefmt.parse_block(block)
            if prior_text and prior_style == style:
                log(f"summary already present (style={style}) — skipping; "
                    "pass force=true to regenerate")
                print(json.dumps({
                    "note_path": str(dest), "url": url,
                    "title": title_of(front), "site": site_of(front),
                    "chars": len(body), "summary": prior_text,
                    "model": cfg.model, "chunks": 0, "style": style,
                    "regenerated": False, "fetched": False,
                }))
                return
        log("re-using the stored article text; pass refetch=true to re-download")
    else:
        body, meta = load_page(url, min(FETCH_TIMEOUT, max(int(remaining()), 5)))
        fetched = True
        if len(body) < MIN_CHARS:
            die(f"only {len(body)} characters of text extracted — the page "
                "appears to need JavaScript to render its content",
                permanent=True)
        front = frontmatter(url, meta, len(body))
        dest = note_path(url, meta.get("title", ""))
        log(f"{len(body)} chars from {meta.get('site', '')}")

    title = meta.get("title") or title_of(front)
    try:
        llm.check_model(cfg)
        summary, n_chunks = llm.summarise(
            cfg, body, style, title, remaining, kind="article", log=log
        )
    except llm.LLMError as exc:
        die(str(exc), permanent=exc.permanent)

    if not summary or len(summary) < 40:
        die("model returned an empty summary")

    assert dest is not None
    notefmt.atomic_write(
        dest,
        notefmt.compose(front, notefmt.make_block(summary, cfg.model, style), body),
    )
    log(f"wrote {dest}")

    print(json.dumps({
        "note_path": str(dest), "url": url, "title": title,
        "site": meta.get("site") or site_of(front), "chars": len(body),
        "summary": summary, "model": cfg.model, "chunks": n_chunks,
        "style": style, "regenerated": True, "fetched": fetched,
    }))


def title_of(front: str) -> str:
    m = re.search(r'^title:\s*"?(.*?)"?\s*$', front, re.M)
    return m.group(1) if m else ""


def site_of(front: str) -> str:
    m = re.search(r'^site:\s*"?(.*?)"?\s*$', front, re.M)
    return m.group(1) if m else ""


if __name__ == "__main__":
    main()
