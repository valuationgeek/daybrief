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
"""

import logging

import httpx

from . import config
from .text import drop_partial_tail

log = logging.getLogger("daybrief.llm")


def generate(prompt: str, max_tokens: int = 300) -> str:
    """Generate text with the configured provider. Returns "" on failure.

    max_tokens caps the output on every provider. If the cap (rather than
    the model) ends the text, the dangling last sentence is trimmed off.
    """
    provider = config.settings()["llm"].get("provider", "ollama")
    if provider == "openai":
        return _generate_openai(prompt, max_tokens)
    if provider != "ollama":
        log.warning(f"Unknown llm.provider '{provider}' — falling back to ollama")
    return _generate_ollama(prompt, max_tokens)


def _generate_ollama(prompt: str, max_tokens: int = 300, retries: int = 1) -> str:
    """One retry covers a transient error (e.g. the model being evicted
    mid-run, a connection reset) before the caller falls back to its own
    default. A timeout is not retried: the call has already burned the full
    per-call ceiling, and a second one would double the stall per article.

    Calls go through ollama_client's concurrency gate and circuit breaker.
    """
    from . import ollama_client as oc

    if oc.breaker_open():
        return ""

    last_err = None
    for attempt in range(retries + 1):
        try:
            client = oc.make_client()
            with oc.slot():
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
            oc.record_result(True)
            return text
        except httpx.TimeoutException as e:
            # ollama.Client only wraps HTTPStatusError/ConnectError, so a
            # timeout surfaces as the raw httpx exception.
            last_err = e
            log.warning("Ollama call timed out — not retrying")
            break
        except Exception as e:
            last_err = e
            if attempt < retries:
                log.warning(f"Ollama call failed ({e}) — retry {attempt + 1}/{retries}")

    log.warning(f"Ollama call failed after {attempt + 1} attempt(s): {last_err}")
    oc.record_result(False)
    return ""


def _generate_openai(prompt: str, max_tokens: int = 300) -> str:
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
        return text
    except Exception as e:
        log.warning(f"OpenAI-compatible call failed: {e}")
        return ""
