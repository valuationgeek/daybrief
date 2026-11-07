"""Offline tests for body enrichment in agent/collector.py — the network and DNS are mocked."""

import socket

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


def _dns(monkeypatch, address="93.184.216.34", only=None):
    """Host names resolve to `address` (public by default). With `only`, just that host does."""
    real = net.socket.getaddrinfo

    def getaddrinfo(host, port, *args, **kwargs):
        if only is not None and host != only:
            return real(host, port, *args, **kwargs)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(net.socket, "getaddrinfo", getaddrinfo)


def _mock_get(monkeypatch, handler, address="93.184.216.34"):
    """Serve every request from `handler` through every httpx.Client safe_get makes."""
    calls = []

    def record(request):
        calls.append(str(request.url))
        return handler(request)

    real_client = httpx.Client

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(record)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    _dns(monkeypatch, address)
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


def test_link_to_a_private_address_is_refused_without_a_request(monkeypatch):
    calls = _mock_get(monkeypatch, lambda req: httpx.Response(200, text=ARTICLE_HTML, request=req),
                      address="192.168.1.1")
    assert collector._fetch_body("http://router.example/admin") == ("", True)
    assert calls == []


def test_redirect_to_a_private_address_is_refused(monkeypatch):
    def handler(req):
        if req.url.host == "news.example":
            return httpx.Response(302, headers={"location": "http://ollama.internal:11434/api/ps"}, request=req)
        return httpx.Response(200, text="secret", request=req)

    calls = _mock_get(monkeypatch, handler)
    _dns(monkeypatch, "93.184.216.34")
    public = net.socket.getaddrinfo

    def getaddrinfo(host, port, *args, **kwargs):
        if host == "ollama.internal":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        return public(host, port, *args, **kwargs)

    monkeypatch.setattr(net.socket, "getaddrinfo", getaddrinfo)
    assert collector._fetch_body("https://news.example/story") == ("", True)
    assert calls == ["https://news.example/story"]


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

    def fetch(url, timeout=None, client=None):
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


def test_non_english_with_a_real_body_and_long_bodies_are_not_refetched(monkeypatch):
    calls = _fake_fetch(monkeypatch, lambda url: ("", True))
    articles = [{"url": "https://example.cn/a", "title": "t", "body": "字" * 400, "language": "zh"},
                {"url": "https://example.com/long", "title": "t", "body": "x" * 9500}]
    collector._enrich_bodies(articles)
    assert calls == []


def test_title_only_non_english_entry_is_rescued(monkeypatch):
    page = "央行宣布下调利率，以支持经济增长。" * 20
    calls = _fake_fetch(monkeypatch, lambda url: (page, True))
    articles = [{"url": "https://example.cn/rate", "title": "央行降息", "body": "", "language": "zh"}]
    collector._enrich_bodies(articles)
    assert calls == ["https://example.cn/rate"] and articles[0]["body"] == page


def test_missing_publication_date_is_marked_estimated():
    a = collector._blank_article("T", "https://example.com/a", "S", "world", None)
    assert a["published_estimated"] and a["published"]
    b = collector._blank_article("T", "https://example.com/b", "S", "world", "2026-11-07T08:00:00+00:00")
    assert not b["published_estimated"]


def test_malformed_link_does_not_abort_enrichment(monkeypatch):
    page = "Binance said it is attempting to recover the stolen funds after the hack. " * 20
    calls = _fake_fetch(monkeypatch, lambda url: (page, True))
    articles = _articles(["http://[2001:db8::1/story", "https://example.com/ok"])
    collector._enrich_bodies(articles)
    assert calls == ["https://example.com/ok"]
    assert articles[1]["body"] == page


# ── RSS summaries ────────────────────────────────────────────

def test_strip_html_unescapes_before_stripping_and_keeps_comparisons():
    raw = "&lt;iframe src='https://evil.example'&gt;&lt;/iframe&gt; <p>Profit < 5% while revenue > 10%</p>"
    text = collector._strip_html(raw)
    assert "<iframe" not in text and "<p>" not in text
    assert "Profit < 5% while revenue > 10%" in text
