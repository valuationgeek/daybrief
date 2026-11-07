"""
llm.py — Single entry point for LLM text generation.

Every stage that needs the LLM (analyzer, fusion) calls generate() here.
The provider is chosen by settings.yaml → llm.provider:

  "ollama" — local model via the Ollama daemon (default, free)
  "openai" — any OpenAI-compatible chat-completions API: OpenAI itself,
             OpenRouter, Groq, vLLM, LM Studio, … (the path used by
             headless/cloud deployments where no Ollama is available)

generate() returns "" on any provider failure — callers decide their own
fallback (e.g. analyzer falls back to lead sentences, fusion to joined
summaries). Only the configured provider is tried; there is no silent
cross-provider fallback.

The "openai" provider is not tied to OpenAI the company: point
settings.yaml → llm.openai.base_url at any endpoint that speaks the
/chat/completions format and it works unchanged.

A circuit breaker in front of both providers stops a dead or hung endpoint
from costing a full timeout on every remaining article. See breaker_admit.
"""

import logging
import threading
import time

import httpx

from . import config
from .text import drop_partial_tail

log = logging.getLogger("daybrief.llm")


def generate(prompt: str, max_tokens: int = 300) -> str:
    """Generate text with the configured provider. Returns "" on failure.

    max_tokens caps the output on every provider. If the cap (rather than
    the model) ends the text, the dangling last sentence is trimmed off.
    """
    admitted = breaker_admit()
    if admitted is None:
        return ""
    provider = config.settings()["llm"].get("provider", "ollama")
    if provider == "openai":
        text, ok = _generate_openai(prompt, max_tokens)
    else:
        if provider != "ollama":
            log.warning(f"Unknown llm.provider '{provider}' — falling back to ollama")
        text, ok = _generate_ollama(prompt, max_tokens, probe=admitted == "probe")
    if ok is not None:
        record_result(ok)
    return text


# ─────────────────────────────────────────────────────────────
# Circuit breaker, shared by every provider and every caller
# ─────────────────────────────────────────────────────────────
#
# After `failure_threshold` calls in a row fail (after their own retries),
# the breaker opens and calls return "" at once, so the run finishes on
# extractive fallbacks instead of paying a timeout per article. It doesn't
# stay open for the run: every `breaker_cooldown_seconds` one call is let
# through as a probe, and if it succeeds the breaker closes. A few seconds
# of Ollama restarting (an update, a crash) then costs one cooldown, not
# every remaining article.

_breaker_lock = threading.Lock()
_breaker      = {"failures": 0, "open": False, "opened_at": 0.0, "probing": False}


def _breaker_settings():
    llm_cfg = config.settings().get("llm", {})
    # failure_threshold used to live under llm.ollama; existing configs still work.
    threshold = llm_cfg.get("failure_threshold", (llm_cfg.get("ollama") or {}).get("failure_threshold", 3))
    cooldown  = llm_cfg.get("breaker_cooldown_seconds", 60)
    return max(1, int(threshold)), max(0.0, float(cooldown))


def breaker_admit():
    """May a call go out now? "call", "probe" (the one call allowed through
    an open breaker after its cooldown), or None (return "" right away)."""
    _, cooldown = _breaker_settings()
    with _breaker_lock:
        if not _breaker["open"]:
            return "call"
        if _breaker["probing"] or time.monotonic() - _breaker["opened_at"] < cooldown:
            return None
        _breaker["probing"] = True
        return "probe"


def breaker_open() -> bool:
    with _breaker_lock:
        return _breaker["open"]


def record_result(ok: bool) -> None:
    """Feed the breaker the outcome of one generate call (after retries)."""
    threshold, cooldown = _breaker_settings()
    with _breaker_lock:
        if ok:
            if _breaker["open"]:
                log.info("LLM is answering again — closing the circuit breaker")
            _breaker.update(failures=0, open=False, probing=False)
            return
        _breaker["failures"] += 1
        if _breaker["probing"]:
            # The probe failed: stay open and wait another cooldown.
            _breaker.update(probing=False, opened_at=time.monotonic())
            return
        if not _breaker["open"] and _breaker["failures"] >= threshold:
            _breaker.update(open=True, opened_at=time.monotonic())
            log.warning(
                f"LLM failed {threshold} calls in a row — extractive fallbacks until it "
                f"answers again (one probe call every {cooldown:g}s)"
            )


def reset_breaker() -> None:
    """Start a run with a closed breaker and a freshly sized Ollama gate."""
    with _breaker_lock:
        _breaker.update(failures=0, open=False, opened_at=0.0, probing=False)
    from . import ollama_client
    ollama_client.reset_slots()


# ─────────────────────────────────────────────────────────────
# Providers. Each returns (text, ok): ok is None when the call was skipped
# and says nothing about the provider's health.
# ─────────────────────────────────────────────────────────────

def _generate_ollama(prompt: str, max_tokens: int = 300, retries: int = 1, probe: bool = False):
    """One retry covers a transient error (e.g. the model being evicted
    mid-run, a connection reset) before the caller falls back to its own
    default. It waits llm.ollama.retry_delay seconds first (default 3), so a
    retry fired in the same millisecond can't fail for the same reason. A
    timeout is not retried: the call has already burned the full per-call
    ceiling, and a second one would double the stall per article.

    Calls go through ollama_client's concurrency gate. A call that waited
    for a slot checks the breaker again before sending, so calls queued
    behind a failing one don't each go on to fail too.
    """
    from . import ollama_client as oc

    delay    = float(config.settings()["llm"].get("ollama", {}).get("retry_delay", 3))
    last_err = None
    for attempt in range(retries + 1):
        try:
            client = oc.make_client()
            with oc.slot():
                if not probe and breaker_open():
                    return "", None
                response = client.generate(
                    model=oc.get_model(),
                    prompt=prompt,
                    options=oc.generate_options(max_tokens),
                    keep_alive=oc.keep_alive(),
                )
            text = (response.response or "").strip()
            if getattr(response, "done_reason", None) == "length":
                log.warning(
                    f"Ollama hit num_predict={max_tokens} — trimming partial "
                    f"last sentence from a {len(text.split())}-word output"
                )
                text = drop_partial_tail(text)
            return text, True
        except httpx.TimeoutException as e:
            # ollama.Client only wraps HTTPStatusError/ConnectError, so a
            # timeout surfaces as the raw httpx exception.
            last_err = e
            log.warning("Ollama call timed out — not retrying")
            break
        except Exception as e:
            last_err = e
            if attempt < retries:
                log.warning(f"Ollama call failed ({e}) — retrying in {delay:g}s ({attempt + 1}/{retries})")
                time.sleep(delay)
                if not probe and breaker_open():
                    break

    log.warning(f"Ollama call failed after {attempt + 1} attempt(s): {last_err}")
    return "", False


def _generate_openai(prompt: str, max_tokens: int = 300):
    from openai import OpenAI

    cfg = config.settings()["llm"]["openai"]
    try:
        client = OpenAI(
            api_key=cfg["api_key"],
            # empty/missing base_url → SDK default (api.openai.com)
            base_url=cfg.get("base_url") or None,
            # The SDK default is 10 minutes per attempt — far too long for
            # a single summary on a scheduled run.
            timeout=cfg.get("timeout", 120),
            # The SDK retries twice by default, timeouts included, so one hung
            # call cost three timeouts. The breaker handles a dead endpoint.
            max_retries=int(cfg.get("max_retries", 1)),
        )
        resp = client.chat.completions.create(
            model=cfg.get("model", "gpt-4.1-mini"),
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        choice = resp.choices[0]
        text = (choice.message.content or "").strip()
        if choice.finish_reason == "length":
            log.warning(
                f"OpenAI-compatible call hit max_tokens={max_tokens} — trimming "
                f"partial last sentence from a {len(text.split())}-word output"
            )
            text = drop_partial_tail(text)
        return text, True
    except Exception as e:
        log.warning(f"OpenAI-compatible call failed: {e}")
        return "", False
