"""Offline tests for agent/net.py — retry and timeout behaviour."""

import socket

import httpx
import pytest

from agent import config, net


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(net.time, "sleep", lambda s: None)
    monkeypatch.setattr(config, "feeds", lambda: {"limits": {"fetch_timeout_seconds": 7}})


def _flaky(failures, exc):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] <= failures:
            raise exc
        return "ok"

    return fn, calls


def test_retries_transient_error_then_succeeds():
    fn, calls = _flaky(2, httpx.ConnectError("reset"))
    assert net.with_retry(fn) == "ok"
    assert calls["n"] == 3


def test_does_not_retry_http_status_errors():
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(404, request=request)
    fn, calls = _flaky(5, httpx.HTTPStatusError("404", request=request, response=response))

    with pytest.raises(httpx.HTTPStatusError):
        net.with_retry(fn)
    assert calls["n"] == 1


def test_reraises_after_all_attempts():
    fn, calls = _flaky(10, TimeoutError("down"))
    with pytest.raises(TimeoutError):
        net.with_retry(fn, attempts=3)
    assert calls["n"] == 3


def test_resolve_timeout_never_none():
    assert net.resolve_timeout() == 7
    assert net.resolve_timeout(3) == 3


def test_zero_retry_attempts_still_makes_the_request(monkeypatch):
    monkeypatch.setattr(config, "feeds", lambda: {"limits": {"retry_attempts": 0}})
    fn, calls = _flaky(0, TimeoutError("unused"))
    assert net.with_retry(fn) == "ok"
    assert calls["n"] == 1


def test_unsupported_protocol_is_not_retried():
    fn, calls = _flaky(5, httpx.UnsupportedProtocol("relative URL"))
    with pytest.raises(httpx.UnsupportedProtocol):
        net.with_retry(fn)
    assert calls["n"] == 1



# ── check_public_url / safe_get ──────────────────────────────

def _resolve_to(monkeypatch, address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    monkeypatch.setattr(net.socket, "getaddrinfo",
                        lambda host, port, *a, **k: [(family, socket.SOCK_STREAM, 6, "", (address, port))])


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254",
                                     "::1", "::ffff:127.0.0.1", "fd00::1", "224.0.0.1", "0.0.0.0"])
def test_non_public_addresses_are_refused(monkeypatch, address):
    _resolve_to(monkeypatch, address)
    with pytest.raises(net.FetchRefused):
        net.check_public_url("https://looks-public.example/a")


def test_public_address_is_allowed(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    net.check_public_url("https://example.com/a")


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///etc/passwd", "ftp://example.com/x", "//example.com"])
def test_non_http_schemes_are_refused(url):
    with pytest.raises(net.FetchRefused):
        net.check_public_url(url)


def test_fetch_refused_is_never_retried():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise net.FetchRefused("private")

    with pytest.raises(net.FetchRefused):
        net.with_retry(fn)
    assert calls["n"] == 1


def test_safe_get_caps_the_size(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=b"x" * 5000, request=req))
    with pytest.raises(net.FetchRefused):
        net.safe_get("https://example.com/big", max_bytes=1000, transport=transport)


def test_safe_get_stops_after_too_many_redirects(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    transport = httpx.MockTransport(lambda req: httpx.Response(302, headers={"location": "/again"}, request=req))
    with pytest.raises(net.FetchRefused):
        net.safe_get("https://example.com/loop", transport=transport)


def test_safe_get_follows_a_public_redirect(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")

    def handler(req):
        if req.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"}, request=req)
        return httpx.Response(200, text="moved here", request=req)

    resp = net.safe_get("https://example.com/old", transport=httpx.MockTransport(handler))
    assert resp.status_code == 200 and resp.content == b"moved here"
    assert str(resp.url) == "https://example.com/new"


def test_safe_get_skips_the_body_of_an_unwanted_content_type(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    transport = httpx.MockTransport(lambda req: httpx.Response(
        200, headers={"content-type": "application/pdf"}, content=b"%PDF" * 1000, request=req))
    resp = net.safe_get("https://example.com/x.pdf", accept=lambda ct: "html" in ct, transport=transport)
    assert resp.status_code == 200 and resp.content == b""


def test_raise_for_status_is_not_retried(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    transport = httpx.MockTransport(lambda req: httpx.Response(404, request=req))
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return net.safe_get("https://example.com/gone", transport=transport).raise_for_status()

    with pytest.raises(net.HTTPStatus):
        net.with_retry(fn)
    assert calls["n"] == 1
