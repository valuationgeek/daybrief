"""Offline tests for agent/outputs/safe_text.py and the templates that use it."""

import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from agent import config
from agent.outputs import email_digest, safe_text
from agent.outputs.labels import sentiment_emoji

TEMPLATES = Path(__file__).parent.parent / "templates"
# A live Markdown link or embed needs an unescaped "](". Escaped text keeps the
# words ("javascript:") but can't link anywhere.
LIVE_LINK = re.compile(r"(?<!\\)\]\(")
HOSTILE_TITLE = "Markets ![x](https://track.example/p.png) <img src=x onerror=alert(1)> [click](javascript:alert(1))"


def test_safe_url_allows_only_http_and_https():
    assert safe_text.safe_url("https://example.com/a b") == "https://example.com/a%20b"
    assert safe_text.safe_url("HTTP://example.com") == "HTTP://example.com"
    for bad in ("javascript:alert(1)", "file:///etc/passwd", "data:text/html,x", "obsidian://open", "", None):
        assert safe_text.safe_url(bad) == ""


def test_md_text_neutralises_links_embeds_html_and_tables():
    out = safe_text.md_text(HOSTILE_TITLE + " | cell")
    assert not LIVE_LINK.search(out) and "<img" not in out
    assert r"\|" in out and "&lt;img" in out


def test_md_code_strips_backticks_and_dataview_prefixes():
    assert safe_text.md_code("$= dv.pages()") == "dv.pages()"
    assert safe_text.md_code("= this.file.name") == "this.file.name"
    assert "`" not in safe_text.md_code("a`b")


def test_md_link_drops_unsafe_targets_and_encodes_parens():
    assert safe_text.md_link("t", "javascript:alert(1)") == "t"
    assert safe_text.md_link("t", "https://example.com/a_(b)") == "[t](https://example.com/a_%28b%29)"


def test_none_label_shows_no_icon():
    assert sentiment_emoji(None) == ""
    assert sentiment_emoji("negative") == "😟"


def _article(**extra):
    base = {"id": "a1", "title": HOSTILE_TITLE, "url": "javascript:alert(1)", "source": "Feed <b>",
            "category": "world", "published": "2026-11-07T08:00:00", "summary": "$= dv.pages()",
            "read_angle": "", "keywords": ["`x`"], "sentiment_label": None, "score": 0.9, "flags": []}
    base.update(extra)
    return base


def test_obsidian_note_renders_hostile_feed_text_inert():
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=False)
    env.globals.update(flag_emoji=lambda f: "🟢", sentiment_emoji=sentiment_emoji, zip=zip)
    safe_text.register(env)
    cluster = {"headline": HOSTILE_TITLE, "unified_summary": "<script>x</script>", "sources": ["S"],
               "titles": [HOSTILE_TITLE], "urls": ["javascript:alert(1)"], "top_keywords": [],
               "source_count": 2, "score": 0.8, "flags": []}
    note = env.get_template("daily_note.md.j2").render(
        today="2026-11-07", created="x", total_articles=1, total_clusters=1, breaking_count=0,
        title_dt="x", insights=["see ![img](https://t.example/x.png)"], trends=[], filename="f",
        index_filename="i",
        categories=[{"label": "World", "total": 1, "clusters": [cluster], "solo_articles": [_article()]}],
    )
    for bad in ("![", "<img", "<script", "\n$="):
        assert bad not in note, bad
    assert not LIVE_LINK.search(note)   # every link in this note had an unsafe target


def test_email_drops_unsafe_links_and_hides_missing_label(monkeypatch):
    monkeypatch.setattr(config, "feeds", lambda: {"categories": {"world": {"label": "World"}}})
    monkeypatch.setattr(config, "sources", lambda: {"scoring": {}})
    monkeypatch.setattr(config, "settings", lambda: {"agent": {"timezone": "UTC"}})
    html = email_digest._build_html([_article()], [], [], [], "2026-11-07", {})
    assert 'href="javascript' not in html and "<img src=x" not in html
    assert 'class="badge neu"' not in html   # None label: no "neutral" badge
