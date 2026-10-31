"""
ollama_client.py — Single place to create the Ollama client.

The key setting is trust_env=False, which tells the underlying
httpx library to ignore ALL system proxy environment variables.
This fixes the HTTP 502 error that appears when a VPN or corporate
proxy intercepts requests to localhost.

Usage everywhere in this project:
    from agent.ollama_client import make_client
    client = make_client()
    response = client.generate(model="llama3.2:3b", prompt="...")
"""

import logging

import ollama
from . import config

log = logging.getLogger("daybrief.ollama")


def make_client(timeout=None) -> ollama.Client:
    """
    Return an ollama.Client that connects directly to localhost,
    bypassing any system proxy or VPN.

    `timeout` overrides the configured per-call ceiling (used by warm_up,
    where a cold model load legitimately takes minutes).
    """
    cfg      = config.settings()["llm"]["ollama"]
    base_url = cfg.get("base_url", "http://localhost:11434")

    # ollama.Client defaults to timeout=None, which httpx reads as "block
    # forever". A generate() call that hits a half-open socket then hangs its
    # worker thread permanently: analyze_all's as_completed() never returns,
    # the run stalls, and main.py's finally never writes the run_log row —
    # the run fails invisibly. Generation is slow, so this is a generous
    # ceiling, not a latency target.
    if timeout is None:
        timeout = cfg.get("timeout", 180)

    return ollama.Client(
        host=base_url,
        trust_env=False,   # <-- bypasses system proxy / VPN (fixes HTTP 502)
        timeout=timeout,
    )


def get_model() -> str:
    """Return the model name from settings.yaml."""
    return config.settings()["llm"]["ollama"]["model"]


def keep_alive() -> str:
    """How long Ollama should hold the model in memory after a call.

    Ollama's default is 5m. Stage 3 → stage 4 (fusion) can idle longer than
    that, and an eviction means the next call pays the full cold-load cost
    again — which is what blows the per-call timeout.
    """
    return config.settings()["llm"]["ollama"].get("keep_alive", "30m")


def generate_options(max_tokens=None) -> dict:
    """Per-call options for client.generate().

    num_predict is the important one: without it Ollama generates until the
    model decides to stop, ignoring the caller's max_tokens entirely.
    """
    opts = {}
    if max_tokens:
        opts["num_predict"] = int(max_tokens)
    return opts


def warm_up() -> bool:
    """Force Ollama to load the model before the analyzer fans out.

    Cold-loading a model can take longer than the per-call timeout, and every
    concurrent worker blocks on that same single load — so without this the
    first `max_workers` articles of a run can time out together and fall back
    to extractive summaries. One serial priming call absorbs the load cost;
    the pool then starts against a resident model.
    """
    cfg = config.settings()["llm"]["ollama"]
    try:
        client = make_client(timeout=cfg.get("warmup_timeout", 600))
        client.generate(
            model=get_model(),
            prompt="ok",
            options={"num_predict": 1},
            keep_alive=keep_alive(),
        )
        log.info(f"Ollama model '{get_model()}' warm and resident")
        return True
    except Exception as e:
        log.warning(f"Ollama warm-up failed ({e}) — first calls may time out")
        return False
