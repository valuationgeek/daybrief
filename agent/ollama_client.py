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

import ollama
from . import config


def make_client() -> ollama.Client:
    """
    Return an ollama.Client that connects directly to localhost,
    bypassing any system proxy or VPN.
    """
    cfg      = config.settings()["llm"]["ollama"]
    base_url = cfg.get("base_url", "http://localhost:11434")

    return ollama.Client(
        host=base_url,
        trust_env=False,   # <-- bypasses system proxy / VPN (fixes HTTP 502)
    )


def get_model() -> str:
    """Return the model name from settings.yaml."""
    return config.settings()["llm"]["ollama"]["model"]
