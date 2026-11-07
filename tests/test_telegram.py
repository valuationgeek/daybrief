"""Offline tests for message formatting in agent/outputs/telegram_bot.py."""

import pytest

from agent import config
from agent.outputs.telegram_bot import MAX_MSG_LEN, _format_article, _format_digest


@pytest.fixture(autouse=True)
def patched_feeds(monkeypatch):
    monkeypatch.setattr(config, "feeds", lambda: {"categories": {"world": {"label": "World & Politics"}}})


def test_format_article_survives_failed_analysis_fields():
    # An article whose analysis raised can reach outputs with None fields.
    article = {"title": "Headline", "url": "https://example.com", "summary": None,
               "score": None, "flags": None, "sentiment_label": None}
    out = _format_article(article)
    assert "Headline" in out
    assert "Score: 0.00" in out


def test_format_article_escapes_html_and_markdown_specials():
    article = {"title": "S&P_500 *rally* [live] <b>", "source": "A&B",
               "url": "https://example.com/?a=1&b=2", "summary": "x < y", "score": 0.5}
    out = _format_article(article)
    assert "S&amp;P_500 *rally* [live] &lt;b&gt;" in out
    assert 'href="https://example.com/?a=1&amp;b=2"' in out
    assert "x &lt; y" in out


def test_format_digest_stays_within_limit_and_keeps_tags_balanced():
    articles = [
        {"title": f"Story {i} " + "x" * 200, "url": f"https://example.com/{i}",
         "source": "Src", "category": "world", "score": 0.9, "flags": []}
        for i in range(5)
    ]
    insights = ["insight " + "y" * 900 for _ in range(4)]
    clusters = [{"top_keywords": ["kw"], "source_count": 3, "unified_summary": "z" * 400,
                 "score": 0.8, "flags": []} for _ in range(3)]
    trends = [{"keyword": "k<1>", "spike_factor": 3.2}]

    out = _format_digest(articles, clusters, insights, trends, "2026-10-31")

    assert len(out) <= MAX_MSG_LEN
    for tag in ("b", "i", "a", "code"):
        assert out.count(f"<{tag}>") + out.count(f"<{tag} ") == out.count(f"</{tag}>")


def test_unsafe_link_is_dropped_and_none_label_has_no_icon():
    out = _format_article({"title": "T", "url": "javascript:alert(1)", "source": "S",
                           "summary": "s", "score": 0.5, "sentiment_label": None})
    assert "javascript:" not in out and "<a href" not in out
    assert "😐" not in out


def test_cluster_heading_is_the_lead_title():
    clusters = [{"headline": "Fed holds rates <steady>", "top_keywords": ["branson did"],
                 "source_count": 3, "unified_summary": "s", "score": 0.9, "flags": []}]
    out = _format_digest([], clusters, [], [], "2026-11-07")
    assert "Fed holds rates &lt;steady&gt;" in out and "branson did" not in out
