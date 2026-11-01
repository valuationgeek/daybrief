"""Offline tests for agent/net.py — retry and timeout behaviour."""

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
