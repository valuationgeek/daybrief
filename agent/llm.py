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

from . import config

log = logging.getLogger("daybrief.llm")


def generate(prompt: str, max_tokens: int = 300) -> str:
    """Generate text with the configured provider. Returns "" on failure."""
    provider = config.settings()["llm"].get("provider", "ollama")
    if provider == "openai":
        return _generate_openai(prompt, max_tokens)
    if provider != "ollama":
        log.warning(f"Unknown llm.provider '{provider}' — falling back to ollama")
    return _generate_ollama(prompt)


def _generate_ollama(prompt: str) -> str:
    from .ollama_client import make_client, get_model

    try:
        client = make_client()
        response = client.generate(model=get_model(), prompt=prompt)
        return (response.response or "").strip()
    except Exception as e:
        log.warning(f"Ollama call failed: {e}")
        return ""


def _generate_openai(prompt: str, max_tokens: int = 300) -> str:
    from openai import OpenAI

    cfg = config.settings()["llm"]["openai"]
    client = OpenAI(
        api_key=cfg["api_key"],
        # empty/missing base_url → SDK default (api.openai.com)
        base_url=cfg.get("base_url") or None,
    )
    try:
        resp = client.chat.completions.create(
            model=cfg.get("model", "gpt-4.1-mini"),
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning(f"OpenAI-compatible call failed: {e}")
        return ""
