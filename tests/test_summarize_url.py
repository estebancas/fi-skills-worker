"""summarize_url/run.py

The guarded behaviours, in rough order of how much a regression would hurt:

  1. A URL from a chat message must not reach this network   (SSRF: Ollama on
     127.0.0.1, and every service on the tailnet)
  2. A re-run must not clobber or stack a summary            (silent data loss)
  3. An existing same-style summary is reused without a fetch or a model call
  4. A page that renders with JavaScript is refused, not summarised from its
     navigation bar

Hermetic: fetch() is mocked at the single function wrapping curl, and the model
at llm.generate — no network, no Ollama.
"""

import io
import json

import pytest

from skilllib import notefmt

SKILL = "summarize_url/run.py"

ARTICLE = """<!doctype html>
<html><head>
<title>The Real Title</title>
<meta property="og:site_name" content="Example Blog">
<meta property="article:published_time" content="2026-01-15T09:00:00Z">
<meta name="author" content="A Writer">
</head><body>
<nav>Home About Contact</nav>
<header>Site banner</header>
<script>var tracking = 1;</script>
<article>
<h1>The Real Title</h1>
<p>%s</p>
<p>Second paragraph with an &amp; entity and a &lt;tag&gt; escape.</p>
</article>
<footer>Copyright nobody</footer>
</body></html>
""" % ("The substance of the article. " * 30)


@pytest.fixture
def su(load, vault):
    return load(SKILL, {
        "VAULT_DIR": str(vault),
        "JOB_TIMEOUT": "1800",
        # Test URLs are unroutable by design; the guard is tested separately.
        "ALLOW_PRIVATE_URLS": "1",
    })


@pytest.fixture
def guarded(load, vault):
    """Same skill with the private-address guard active."""
    return load(SKILL, {"VAULT_DIR": str(vault), "ALLOW_PRIVATE_URLS": "0"})


def page(su, monkeypatch, html=ARTICLE, status=200, ctype="text/html; charset=utf-8",
         final="https://example.test/post"):
    calls = []

    def fake_fetch(url, timeout):
        calls.append(url)
        body = html.encode() if isinstance(html, str) else html
        return status, ctype, final, body

    monkeypatch.setattr(su, "fetch", fake_fetch)
    return calls


# ---------------------------------------------------------------------------
# the URL — this is the security test
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://example.test/x",
    "javascript:alert(1)",
])
def test_only_http_and_https_are_fetched(guarded, url):
    with pytest.raises(SystemExit) as exc:
        guarded.check_url(url)
    assert exc.value.code == 2, "a bad scheme will never become good"


@pytest.mark.parametrize("host", [
    "127.0.0.1",        # Ollama listens here
    "10.0.0.5",
    "192.168.1.1",
    "169.254.169.254",  # cloud metadata
    "100.64.0.1",       # the tailnet's range
])
def test_private_destinations_are_refused(guarded, host):
    with pytest.raises(SystemExit) as exc:
        guarded.check_url(f"http://{host}/anything")
    assert exc.value.code == 2


def test_allow_private_urls_opens_the_guard(su):
    assert su.check_url("http://127.0.0.1:8080/x").startswith("http://127.0.0.1")


def test_final_url_is_re_checked_after_redirects(guarded):
    """curl -L can land anywhere; the body must not be used if it landed inside."""
    with pytest.raises(SystemExit) as exc:
        guarded.check_response(200, "text/html", "http://127.0.0.1:11434/api/tags")
    assert exc.value.code == 2


@pytest.mark.parametrize("url", [
    "https://x.com/user/status/123",
    "https://twitter.com/user/status/123",
    "https://www.x.com/user/status/123",
])
def test_x_is_rejected_with_an_explanation(guarded, url, capsys):
    with pytest.raises(SystemExit) as exc:
        guarded.check_url(url)
    assert exc.value.code == 2
    assert "no replies" in capsys.readouterr().err


def test_missing_source_is_permanent(guarded):
    with pytest.raises(SystemExit) as exc:
        guarded.check_url("")
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# normalise — one article, one note
# ---------------------------------------------------------------------------

def test_normalise_strips_tracking_parameters(su):
    a = su.normalise("https://ex.test/post?utm_source=twitter&utm_medium=social&id=7")
    assert a == "https://ex.test/post?id=7"


def test_normalise_strips_the_fragment(su):
    assert su.normalise("https://ex.test/post#section-3") == "https://ex.test/post"


def test_normalise_lowercases_the_host_only(su):
    """Paths are case-sensitive; hosts are not."""
    assert su.normalise("https://EX.Test/Post") == "https://ex.test/Post"


def test_same_article_different_campaign_tags_is_one_note(su):
    a = su.normalise("https://ex.test/p?utm_source=a")
    b = su.normalise("https://ex.test/p?fbclid=xyz")
    assert su.note_path(a, "T") == su.note_path(b, "T")


# ---------------------------------------------------------------------------
# check_response
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [401, 403, 404, 410])
def test_dead_or_walled_pages_are_permanent(su, status):
    with pytest.raises(SystemExit) as exc:
        su.check_response(status, "text/html", "https://ex.test/p")
    assert exc.value.code == 2, "a retry reproduces this exactly"


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_server_trouble_is_transient(su, status):
    with pytest.raises(SystemExit) as exc:
        su.check_response(status, "text/html", "https://ex.test/p")
    assert exc.value.code == 1, "worth another attempt later"


def test_non_text_content_is_permanent(su, capsys):
    with pytest.raises(SystemExit) as exc:
        su.check_response(200, "application/pdf", "https://ex.test/p.pdf")
    assert exc.value.code == 2
    assert "application/pdf" in capsys.readouterr().err, "say what it actually was"


def test_html_and_plain_text_pass(su):
    su.check_response(200, "text/html; charset=utf-8", "https://ex.test/p")
    su.check_response(200, "text/plain", "https://ex.test/p.txt")


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------

def test_extract_prefers_the_article_element(su):
    body, meta = su.extract(ARTICLE.encode(), "text/html", "https://ex.test/p")
    assert "The substance of the article." in body
    assert "Home About Contact" not in body, "<nav> must be dropped"
    assert "Site banner" not in body, "<header> must be dropped"
    assert "Copyright nobody" not in body, "<footer> must be dropped"
    assert "tracking" not in body, "<script> must be dropped"


def test_extract_decodes_entities(su):
    body, _ = su.extract(ARTICLE.encode(), "text/html", "https://ex.test/p")
    assert "an & entity" in body and "a <tag> escape" in body


def test_extract_keeps_paragraph_breaks(su):
    body, _ = su.extract(ARTICLE.encode(), "text/html", "https://ex.test/p")
    assert "\n" in body.strip(), "paragraphs must not run together"


def test_extract_reads_metadata(su):
    _, meta = su.extract(ARTICLE.encode(), "text/html", "https://ex.test/p")
    assert meta["title"] == "The Real Title"
    assert meta["site"] == "Example Blog"
    assert meta["author"] == "A Writer"
    assert meta["published"] == "2026-01-15", "timestamp trimmed to a date"


def test_og_title_wins_over_the_title_tag(su):
    html = ('<html><head><title>Tab Title | Site</title>'
            '<meta property="og:title" content="Article Title">'
            '</head><body><article><p>x</p></article></body></html>')
    _, meta = su.extract(html.encode(), "text/html", "https://ex.test/p")
    assert meta["title"] == "Article Title"


def test_title_is_collapsed_to_one_line(su):
    """Frontmatter is line-based; a wrapped <title> would corrupt it."""
    html = "<html><head><title>Long\n  Wrapped\n  Title</title></head><body>x</body></html>"
    _, meta = su.extract(html.encode(), "text/html", "https://ex.test/p")
    assert meta["title"] == "Long Wrapped Title"


def test_longest_article_wins_when_there_are_several(su):
    """Index pages wrap every card in <article>; the real one is the long one."""
    html = ("<html><body>"
            "<article><p>teaser</p></article>"
            f"<article><p>{'the actual content ' * 40}</p></article>"
            "<article><p>another teaser</p></article>"
            "</body></html>")
    body, _ = su.extract(html.encode(), "text/html", "https://ex.test/p")
    assert "the actual content" in body
    assert "teaser" not in body


def test_main_is_used_when_there_is_no_article(su):
    html = ("<html><body><nav>menu</nav>"
            f"<main><p>{'main content here ' * 40}</p></main>"
            "</body></html>")
    body, _ = su.extract(html.encode(), "text/html", "https://ex.test/p")
    assert "main content here" in body
    assert "menu" not in body


def test_whole_body_is_used_when_neither_is_present(su):
    html = f"<html><body><p>{'bare paragraph text ' * 40}</p></body></html>"
    body, _ = su.extract(html.encode(), "text/html", "https://ex.test/p")
    assert "bare paragraph text" in body


def test_plain_text_is_passed_through(su):
    body, meta = su.extract(b"just some text\n\nin a file", "text/plain",
                            "https://ex.test/notes.txt")
    assert body == "just some text\n\nin a file"
    assert meta["site"] == "ex.test"


# ---------------------------------------------------------------------------
# charset
# ---------------------------------------------------------------------------

def test_decode_uses_the_header_charset(su):
    assert su.decode("caf\xe9".encode("latin-1"), "text/html; charset=latin-1") == "café"


def test_decode_falls_back_to_the_meta_charset(su):
    raw = b'<meta charset="latin-1">' + "caf\xe9".encode("latin-1")
    assert "café" in su.decode(raw, "text/html")


def test_decode_never_raises_on_bad_bytes(su):
    assert su.decode(b"\xff\xfe broken", "text/html")


# ---------------------------------------------------------------------------
# meta refresh — curl -L cannot see it
# ---------------------------------------------------------------------------

REDIRECT = ('<html><head><title>Redirect</title><noscript>'
            '<meta http-equiv="refresh" content="0; url=https://ex.test/real">'
            '</noscript></head><body><p>Click here to be redirected.</p></body></html>')


def test_meta_refresh_inside_noscript_is_followed(su, monkeypatch):
    """The redirect hides in <noscript>, which is otherwise dropped wholesale."""
    seen = []

    def fake_fetch(url, timeout):
        seen.append(url)
        html = REDIRECT if len(seen) == 1 else ARTICLE
        return 200, "text/html", url, html.encode()

    monkeypatch.setattr(su, "fetch", fake_fetch)
    body, _ = su.load_page("https://ex.test/old", 30)
    assert seen == ["https://ex.test/old", "https://ex.test/real"]
    assert "The substance of the article." in body


def test_meta_refresh_is_not_followed_when_the_page_has_content(su, monkeypatch):
    html = REDIRECT.replace("<body>", f"<body><article><p>{'real text ' * 60}</p></article>")
    seen = page(su, monkeypatch, html=html)
    su.load_page("https://ex.test/p", 30)
    assert len(seen) == 1, "a page with content is not a redirect stub"


def test_meta_refresh_target_is_guarded(guarded, monkeypatch):
    """A redirect must not be a way around the private-address check."""
    import socket

    def dns(host, *a, **k):
        """ex.test is public; everything else resolves as itself."""
        ip = "93.184.216.34" if host == "ex.test" else host
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(guarded.socket, "getaddrinfo", dns)
    html = REDIRECT.replace("https://ex.test/real", "http://127.0.0.1:11434/api/tags")
    monkeypatch.setattr(guarded, "fetch",
                        lambda url, timeout: (200, "text/html", url, html.encode()))
    with pytest.raises(SystemExit) as exc:
        guarded.load_page("https://ex.test/old", 30)
    assert exc.value.code == 2, "the redirect target must be checked too"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

GENERATED = "GENERATED SUMMARY TEXT that is comfortably over the minimum length."
URL = "https://example.test/post"


def run_main(su, monkeypatch, capsys, params, summary=GENERATED):
    monkeypatch.setattr(su.llm, "check_model", lambda c: None)
    monkeypatch.setattr(su.llm, "generate", lambda c, p, s, t: summary)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(params)))
    su.main()
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_main_writes_a_note(su, monkeypatch, capsys, vault):
    page(su, monkeypatch)
    out = run_main(su, monkeypatch, capsys, {"source": URL})
    note = vault / "transcripts" / out["note_path"].split("/")[-1]
    text = note.read_text(encoding="utf-8")
    assert out["regenerated"] is True and out["fetched"] is True
    assert text.startswith("---"), "frontmatter first"
    assert text.index(GENERATED) < text.index("The substance of the article.")
    assert "source_type: article" in text
    assert f"url: {URL}" in text


def test_main_skips_when_the_same_style_is_already_there(su, monkeypatch, capsys):
    page(su, monkeypatch)
    run_main(su, monkeypatch, capsys, {"source": URL})

    def boom(*a, **k):
        raise AssertionError("must not fetch or call the model on the skip path")

    monkeypatch.setattr(su, "fetch", boom)
    monkeypatch.setattr(su.llm, "check_model", boom)
    monkeypatch.setattr(su.llm, "generate", boom)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"source": URL})))
    su.main()
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["regenerated"] is False and out["fetched"] is False


def test_main_style_change_resummarises_without_refetching(su, monkeypatch, capsys):
    calls = page(su, monkeypatch)
    run_main(su, monkeypatch, capsys, {"source": URL, "style": "detailed"})
    out = run_main(su, monkeypatch, capsys, {"source": URL, "style": "bullets"},
                   summary="A DIFFERENT SUMMARY, also comfortably over the minimum.")
    assert out["regenerated"] is True
    assert out["fetched"] is False, "the article text is already stored"
    assert len(calls) == 1, "only the first run hits the network"


def test_main_force_regenerates_the_same_style(su, monkeypatch, capsys):
    page(su, monkeypatch)
    run_main(su, monkeypatch, capsys, {"source": URL})
    out = run_main(su, monkeypatch, capsys, {"source": URL, "force": True},
                   summary="A REPLACEMENT SUMMARY, comfortably over the minimum.")
    assert out["regenerated"] is True
    assert "REPLACEMENT" in out["summary"]


def test_main_refetch_downloads_again(su, monkeypatch, capsys):
    calls = page(su, monkeypatch)
    run_main(su, monkeypatch, capsys, {"source": URL})
    run_main(su, monkeypatch, capsys, {"source": URL, "refetch": True})
    assert len(calls) == 2


def test_main_never_stacks_summary_blocks(su, monkeypatch, capsys, vault):
    """The failure this guards is silent: two blocks, the older one stale."""
    page(su, monkeypatch)
    out = run_main(su, monkeypatch, capsys, {"source": URL, "style": "detailed"})
    run_main(su, monkeypatch, capsys, {"source": URL, "style": "bullets"},
             summary="SECOND SUMMARY, comfortably over the minimum length here.")
    text = (vault / "transcripts" / out["note_path"].split("/")[-1]).read_text(
        encoding="utf-8")
    assert text.count(notefmt.SUM_BEGIN) == 1
    assert GENERATED not in text and "SECOND SUMMARY" in text
    assert "The substance of the article." in text, "article text survives"


def test_main_one_note_per_url(su, monkeypatch, capsys, vault):
    page(su, monkeypatch)
    run_main(su, monkeypatch, capsys, {"source": URL})
    run_main(su, monkeypatch, capsys, {"source": URL + "?utm_source=twitter"})
    assert len(list((vault / "transcripts").glob("*.md"))) == 1


def test_main_rejects_a_javascript_shell_page(su, monkeypatch):
    page(su, monkeypatch, html="<html><body><div>Loading…</div></body></html>")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"source": URL})))
    with pytest.raises(SystemExit) as exc:
        su.main()
    assert exc.value.code == 2, "no amount of retrying will render JavaScript"


def test_main_rejects_an_unknown_style(su, monkeypatch):
    monkeypatch.setattr("sys.stdin",
                        io.StringIO(json.dumps({"source": URL, "style": "haiku"})))
    with pytest.raises(SystemExit) as exc:
        su.main()
    assert exc.value.code == 2


def test_main_empty_model_output_is_transient(su, monkeypatch):
    page(su, monkeypatch)
    monkeypatch.setattr(su.llm, "check_model", lambda c: None)
    monkeypatch.setattr(su.llm, "generate", lambda c, p, s, t: "")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"source": URL})))
    with pytest.raises(SystemExit) as exc:
        su.main()
    assert exc.value.code == 1, "a bad generation should be retried"


def test_main_uses_the_article_system_prompt(su, monkeypatch, capsys):
    seen = {}
    page(su, monkeypatch)
    monkeypatch.setattr(su.llm, "check_model", lambda c: None)

    def capture(cfg, prompt, system, timeout):
        seen["system"] = system
        return GENERATED

    monkeypatch.setattr(su.llm, "generate", capture)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"source": URL})))
    su.main()
    capsys.readouterr()
    assert seen["system"] == su.llm.SYSTEMS["article"]


def test_result_json_is_the_last_stdout_line(su, monkeypatch, capsys):
    """The worker parses the last stdout line — nothing may follow it."""
    page(su, monkeypatch)
    monkeypatch.setattr(su.llm, "check_model", lambda c: None)
    monkeypatch.setattr(su.llm, "generate", lambda c, p, s, t: GENERATED)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"source": URL})))
    su.main()
    lines = [x for x in capsys.readouterr().out.strip().splitlines() if x.strip()]
    assert json.loads(lines[-1])["note_path"]


def test_model_param_overrides_the_default(su, monkeypatch, capsys):
    page(su, monkeypatch)
    out = run_main(su, monkeypatch, capsys, {"source": URL, "model": "other:7b"})
    assert out["model"] == "other:7b"
