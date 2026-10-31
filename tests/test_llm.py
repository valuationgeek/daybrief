"""Offline tests for agent/llm.py — the Ollama path with a fake client."""

import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from agent import config, llm, ollama_client


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture(autouse=True)
def fresh_breaker():
    ollama_client.reset_breaker()
    yield
    ollama_client.reset_breaker()


@pytest.fixture
def ollama_settings(monkeypatch):
    settings = {"llm": {"provider": "ollama", "ollama": {"model": "test-model", "keep_alive": "15m"}}}
    monkeypatch.setattr(config, "settings", lambda: settings)
    return settings


def _install(monkeypatch, client):
    monkeypatch.setattr(ollama_client, "make_client", lambda timeout=None: client)


def test_max_tokens_reaches_ollama_as_num_predict(monkeypatch, ollama_settings):
    client = FakeClient([SimpleNamespace(response="A summary.", done_reason="stop")])
    _install(monkeypatch, client)

    assert llm.generate("prompt", max_tokens=400) == "A summary."
    assert client.calls[0]["options"] == {"num_predict": 400}
    assert client.calls[0]["keep_alive"] == "15m"
    assert client.calls[0]["model"] == "test-model"


def test_length_capped_output_is_trimmed(monkeypatch, ollama_settings):
    text = "First sentence is complete. Second one stops in the mid"
    _install(monkeypatch, FakeClient([SimpleNamespace(response=text, done_reason="length")]))

    assert llm.generate("prompt", max_tokens=10) == "First sentence is complete."


def test_naturally_finished_output_is_not_trimmed(monkeypatch, ollama_settings):
    text = "Ends without a period"
    _install(monkeypatch, FakeClient([SimpleNamespace(response=text, done_reason="stop")]))

    assert llm.generate("prompt") == text


def test_one_retry_then_success(monkeypatch, ollama_settings):
    client = FakeClient([TimeoutError("stalled"), SimpleNamespace(response="ok.", done_reason="stop")])
    _install(monkeypatch, client)

    assert llm.generate("prompt") == "ok."
    assert len(client.calls) == 2


def test_persistent_failure_returns_empty_string(monkeypatch, ollama_settings):
    _install(monkeypatch, FakeClient([TimeoutError("a"), TimeoutError("b")]))

    assert llm.generate("prompt") == ""


def test_make_client_always_has_a_timeout(monkeypatch):
    monkeypatch.setattr(config, "settings", lambda: {"llm": {"ollama": {"model": "m"}}})
    client = ollama_client.make_client()
    assert client._client.timeout.read == 180


def test_timeout_is_not_retried(monkeypatch, ollama_settings):
    client = FakeClient([httpx.ReadTimeout("slow"), SimpleNamespace(response="late.", done_reason="stop")])
    _install(monkeypatch, client)

    assert llm.generate("prompt") == ""
    assert len(client.calls) == 1


def test_breaker_opens_after_consecutive_failures(monkeypatch, ollama_settings):
    client = FakeClient([ConnectionError("down")] * 6)
    _install(monkeypatch, client)

    for _ in range(3):
        assert llm.generate("prompt") == ""
    assert ollama_client.breaker_open()
    calls_before = len(client.calls)
    assert llm.generate("prompt") == ""
    assert len(client.calls) == calls_before   # breaker open — client never touched


def test_success_resets_the_failure_count(monkeypatch, ollama_settings):
    ok = SimpleNamespace(response="ok.", done_reason="stop")
    _install(monkeypatch, FakeClient([ConnectionError("a"), ConnectionError("b"), ok,
                                      ConnectionError("c"), ConnectionError("d")]))
    llm.generate("prompt")   # fails (2 attempts)
    llm.generate("prompt")   # succeeds
    llm.generate("prompt")   # fails
    assert not ollama_client.breaker_open()


def test_slot_caps_concurrent_generate_calls(monkeypatch, ollama_settings):
    state = {"now": 0, "peak": 0}
    lock = threading.Lock()

    class SlowClient:
        def generate(self, **kwargs):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.05)
            with lock:
                state["now"] -= 1
            return SimpleNamespace(response="ok.", done_reason="stop")

    _install(monkeypatch, SlowClient())
    threads = [threading.Thread(target=llm.generate, args=("p",)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["peak"] == 1   # max_concurrent defaults to 1
