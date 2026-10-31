"""Offline tests for agent/llm.py — the Ollama path with a fake client."""

from types import SimpleNamespace

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


@pytest.fixture
def ollama_settings(monkeypatch):
    settings = {"llm": {"provider": "ollama", "ollama": {"model": "test-model", "keep_alive": "15m"}}}
    monkeypatch.setattr(config, "settings", lambda: settings)


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
