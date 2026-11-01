"""Offline tests for body enrichment in agent/collector.py — httpx is mocked."""

import httpx
import pytest

from agent import collector, config, net

ARTICLE_HTML = (
    "<html><head><title>Binance hack</title></head><body><article>"
    + "<p>Binance is attempting to recover $6.3bn stolen in a hack linked to North Korea. "
    "Investigators from the FBI are tracing the stolen funds across several exchanges.</p>" * 12
    + "</article></body></html>"
)
CONSENT_TEXT = (
    "Yahoo is part of the Yahoo family of brands. When you use our sites and apps, we use "
    "cookies to provide our sites and apps to you. Select 'Accept all' to agree. " * 8
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    limits = {"fetch_timeout_seconds": 5, "retry_attempts": 2, "enrich_workers": 1,
              "enrich_failure_threshold": 3, "enrich_below_chars": 9000}
    monkeypatch.setattr(config, "feeds", lambda: {"limits": limits})
    monkeypatch.setattr(net.time, "sleep", lambda s: None)
    return limits


def _mock_get(monkeypatch, handler):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return handler(httpx.Request("GET", url))

    monkeypatch.setattr(collector.httpx, "get", get)
    return calls


# ── _fetch_body ──────────────────────────────────────────────

def test_fetch_body_extracts_article_text(monkeypatch):
    _mock_get(monkeypatch, lambda req: httpx.Response(
        200, headers={"content-type": "text/html"}, text=ARTICLE_HTML, request=req))
    text, network_ok = collector._fetch_body("https://example.com/a")
    assert network_ok and "North Korea" in text


def test_paywall_is_a_page_level_miss_not_a_network_failure(monkeypatch):
    _mock_get(monkeypatch, lambda req: httpx.Response(403, text="Forbidden", request=req))
    assert collector._fetch_body("https://www.nytimes.com/x.html") == ("", True)


def test_non_html_response_is_a_page_level_miss(monkeypatch):
    _mock_get(monkeypatch, lambda req: httpx.Response(
        200, headers={"content-type": "application/pdf"}, content=b"%PDF", request=req))
    assert collector._fetch_body("https://example.com/report.pdf") == ("", True)


def test_transport_error_is_retried_then_reported_as_network_failure(monkeypatch):
    def fail(req):
        raise httpx.ConnectError("connection reset", request=req)

    calls = _mock_get(monkeypatch, fail)
    assert collector._fetch_body("https://example.com/a") == ("", False)
    assert len(calls) == 2   # retry_attempts: 2 — the retry actually happens now


def test_relative_url_is_never_fetched(monkeypatch):
    calls = _mock_get(monkeypatch, lambda req: httpx.Response(200, request=req))
    assert collector._fetch_body("/news/123.html") == ("", True)
    assert calls == []


# ── _usable_body ─────────────────────────────────────────────

def test_consent_page_never_replaces_a_good_rss_body():
    rss = "Binance is attempting to recover $6.3bn stolen in a hack linked to North Korea."
    assert not collector._usable_body(CONSENT_TEXT, rss, "Binance attempting to recover $6.3bn stolen in hack")


def test_off_topic_page_is_rejected_even_without_markers():
    page = "Completely unrelated text about gardening and tomatoes. " * 40
    assert not collector._usable_body(page, "short", "Binance attempting to recover stolen funds")


def test_real_article_page_replaces_the_body():
    page = "Binance said it is attempting to recover the stolen funds after the hack. " * 20
    assert collector._usable_body(page, "short lead", "Binance attempting to recover stolen funds")


def test_shorter_page_keeps_the_rss_body():
    assert not collector._usable_body("Binance hack.", "Binance hack, longer RSS lead.", "Binance hack")


# ── _enrich_bodies ───────────────────────────────────────────

def _articles(urls, **extra):
    return [{"url": u, "title": "Binance attempting to recover stolen funds", "body": "lead", **extra}
            for u in urls]


def _fake_fetch(monkeypatch, result_for):
    calls = []

    def fetch(url, timeout=None):
        calls.append(url)
        return result_for(url)

    monkeypatch.setattr(collector, "_fetch_body", fetch)
    return calls


def test_paywalled_pages_do_not_trip_the_breaker(monkeypatch):
    calls = _fake_fetch(monkeypatch, lambda url: ("", True))
    collector._enrich_bodies(_articles([f"https://www.nytimes.com/{i}.html" for i in range(10)]))
    assert len(calls) == 10


def test_unreachable_hosts_trip_the_breaker(monkeypatch):
    calls = _fake_fetch(monkeypatch, lambda url: ("", False))
    collector._enrich_bodies(_articles([f"https://host{i}.example/a" for i in range(10)]))
    assert len(calls) == 3   # enrich_failure_threshold: 3, then the rest are skipped


def test_down_host_is_skipped_after_its_first_failure(monkeypatch):
    calls = _fake_fetch(monkeypatch, lambda url: ("", False))
    collector._enrich_bodies(_articles([f"https://down.example/{i}" for i in range(5)]))
    assert len(calls) == 1


def test_duplicate_url_is_fetched_once_and_applied_to_both(monkeypatch):
    page = "Binance said it is attempting to recover the stolen funds after the hack. " * 20
    calls = _fake_fetch(monkeypatch, lambda url: (page, True))
    articles = _articles(["https://example.com/same", "https://example.com/same"])
    collector._enrich_bodies(articles)
    assert len(calls) == 1
    assert all(a["body"] == page for a in articles)


def test_non_english_and_long_bodies_are_not_refetched(monkeypatch):
    calls = _fake_fetch(monkeypatch, lambda url: ("", True))
    articles = _articles(["https://example.cn/a"], language="zh")
    articles += [{"url": "https://example.com/long", "title": "t", "body": "x" * 9500}]
    collector._enrich_bodies(articles)
    assert calls == []
