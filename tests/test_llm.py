"""Offline tests for agent/llm.py — both providers with fake clients, and the breaker."""

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
def fresh_breaker(monkeypatch):
    sleeps = []
    monkeypatch.setattr(llm.time, "sleep", sleeps.append)
    monkeypatch.setattr(config, "settings", lambda: {"llm": {"ollama": {"model": "m"}}})
    llm.reset_breaker()
    yield sleeps
    llm.reset_breaker()


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
    assert llm.breaker_open()
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
    assert not llm.breaker_open()


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



def test_failed_call_is_retried_after_a_backoff(monkeypatch, ollama_settings, fresh_breaker):
    ok = SimpleNamespace(response="ok.", done_reason="stop")
    _install(monkeypatch, FakeClient([ConnectionError("refused"), ok]))
    assert llm.generate("prompt") == "ok."
    assert fresh_breaker == [3]   # llm.ollama.retry_delay default


def _open_breaker(monkeypatch, clock):
    monkeypatch.setattr(llm.time, "monotonic", lambda: clock["now"])
    for _ in range(3):
        llm.record_result(False)
    assert llm.breaker_open()


def test_open_breaker_lets_one_probe_through_after_the_cooldown(monkeypatch, ollama_settings):
    clock = {"now": 1000.0}
    _open_breaker(monkeypatch, clock)
    client = FakeClient([SimpleNamespace(response="back.", done_reason="stop")])
    _install(monkeypatch, client)

    clock["now"] += 30
    assert llm.generate("prompt") == "" and client.calls == []   # still cooling down
    clock["now"] += 31
    assert llm.generate("prompt") == "back."                        # the probe
    assert not llm.breaker_open() and len(client.calls) == 1


def test_failed_probe_keeps_the_breaker_open_for_another_cooldown(monkeypatch, ollama_settings):
    clock = {"now": 1000.0}
    _open_breaker(monkeypatch, clock)
    client = FakeClient([httpx.ReadTimeout("still down")])
    _install(monkeypatch, client)

    clock["now"] += 61
    assert llm.generate("prompt") == ""
    assert llm.breaker_open() and len(client.calls) == 1
    clock["now"] += 30
    assert llm.generate("prompt") == "" and len(client.calls) == 1


def test_call_queued_behind_a_failure_rechecks_the_breaker(monkeypatch, ollama_settings):
    import contextlib

    client = FakeClient([SimpleNamespace(response="never sent.", done_reason="stop")])
    _install(monkeypatch, client)

    @contextlib.contextmanager
    def slot_that_others_failed_behind():
        for _ in range(3):   # while this call waited, three others failed
            llm.record_result(False)
        yield

    monkeypatch.setattr(ollama_client, "slot", slot_that_others_failed_behind)
    assert llm.generate("prompt") == ""
    assert client.calls == []


# ── OpenAI-compatible provider ───────────────────────────────

def _fake_openai(monkeypatch, outcomes):
    import sys
    import types

    made = []

    class Completions:
        def create(self, **kwargs):
            result = outcomes.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    class OpenAI:
        def __init__(self, **kwargs):
            made.append(kwargs)
            self.chat = SimpleNamespace(completions=Completions())

    module = types.ModuleType("openai")
    module.OpenAI = OpenAI
    monkeypatch.setitem(sys.modules, "openai", module)
    monkeypatch.setattr(config, "settings", lambda: {"llm": {"provider": "openai",
                                                             "openai": {"api_key": "k", "model": "m"}}})
    return made


def _completion(text, reason="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason=reason)])


def test_openai_client_retries_once_not_twice(monkeypatch):
    made = _fake_openai(monkeypatch, [_completion("A summary.")])
    assert llm.generate("prompt") == "A summary."
    assert made[0]["max_retries"] == 1 and made[0]["timeout"] == 120


def test_breaker_covers_the_openai_provider(monkeypatch):
    made = _fake_openai(monkeypatch, [TimeoutError("hung")] * 3)
    for _ in range(3):
        assert llm.generate("prompt") == ""
    assert llm.breaker_open()
    assert llm.generate("prompt") == "" and len(made) == 3   # no fourth client, no fourth timeout
