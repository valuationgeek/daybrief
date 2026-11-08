"""Offline tests for agent/config.py — validation and example-file behaviour."""

from pathlib import Path

import pytest
import yaml

from agent import config

GOOD_FEEDS = {"categories": {"world": {}, "tech": {}}}
GOOD_SOURCES = {
    "scoring": {
        "weights": {"source_credibility": 0.5, "recency": 0.5},
        "score_barriers": {"world": 0.5, "tech": 0.4, "default": 0.4},
        "max_solo_per_category": {"world": 5, "tech": 5, "default": 5},
    }
}


def _patch(monkeypatch, feeds, sources):
    monkeypatch.setattr(config, "feeds", lambda: feeds)
    monkeypatch.setattr(config, "sources", lambda: sources)


def test_validate_clean_config_has_no_warnings(monkeypatch):
    _patch(monkeypatch, GOOD_FEEDS, GOOD_SOURCES)
    assert config.validate() == []


def test_validate_warns_on_missing_category_entry(monkeypatch):
    feeds = {"categories": {"world": {}, "tech": {}, "sports": {}}}
    _patch(monkeypatch, feeds, GOOD_SOURCES)
    warnings = config.validate()
    assert any("sports" in w and "score_barriers" in w for w in warnings)


def test_validate_warns_on_typo_category(monkeypatch):
    sources = {
        "scoring": {
            "weights": {"source_credibility": 1.0},
            "score_barriers": {"world": 0.5, "teck": 0.4, "default": 0.4},
            "max_solo_per_category": {"default": 5},
        }
    }
    _patch(monkeypatch, GOOD_FEEDS, sources)
    warnings = config.validate()
    assert any("teck" in w and "possible typo" in w for w in warnings)


def test_validate_warns_on_bad_weight_sum(monkeypatch):
    sources = {
        "scoring": {
            "weights": {"source_credibility": 0.5, "recency": 0.3},
            "score_barriers": {"world": 0.5, "tech": 0.4, "default": 0.4},
            "max_solo_per_category": {"default": 5},
        }
    }
    _patch(monkeypatch, GOOD_FEEDS, sources)
    warnings = config.validate()
    assert any("weights sum" in w for w in warnings)


def test_missing_settings_points_to_example(monkeypatch, tmp_path):
    (tmp_path / "settings.example.yaml").write_text("agent: {}", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    config.load.cache_clear()
    try:
        with pytest.raises(FileNotFoundError, match="Copy settings.example.yaml"):
            config.load("settings")
    finally:
        config.load.cache_clear()


def test_example_configs_are_valid_yaml():
    root = Path(__file__).parent.parent
    for name in (
        "config/settings.example.yaml",
        "config/watchlist.example.yaml",
        "config/feeds.yaml",
        "config/sources.yaml",
    ):
        data = yaml.safe_load((root / name).read_text(encoding="utf-8"))
        assert isinstance(data, dict), f"{name} did not parse to a mapping"


# ── Secret redaction ─────────────────────────────────────────

@pytest.fixture
def secrets(monkeypatch):
    remembered = set()
    monkeypatch.setattr(config, "_secret_values", remembered)
    return remembered


@pytest.mark.parametrize("line, leaked", [
    ("GET https://api.nytimes.com/svc/topstories/v2/world.json?api-key=Nyt5ecretValue", "Nyt5ecretValue"),
    ("GET https://newsdata.io/api/1/market?language=en&apikey=pub_9f8e7d6c5b", "pub_9f8e7d6c5b"),
    ("POST https://api.telegram.org/bot123456:AAF-secret_part/sendMessage", "AAF-secret_part"),
    # Short and obviously fake: redaction needs 8+ characters, and nothing in
    # the repo should look like a real token to the secret-scanning hook.
    ("Authorization: Bearer FAKE-TOKEN-123", "FAKE-TOKEN-123"),
    ("{'api_key': 'sk-proj-abcdef123456'}", "sk-proj-abcdef123456"),
])
def test_redact_masks_secrets_in_urls_headers_and_dumps(secrets, line, leaked):
    out = config.redact(line)
    assert leaked not in out and "***" in out


def test_redact_masks_every_loaded_secret_wherever_it_appears(secrets, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-1234567890")
    monkeypatch.setenv("DAYBRIEF_SMTP_PASSWORD", "abcd efgh ijkl mnop")
    config._expand_env({"llm": {"openai": {"api_key": "${OPENAI_API_KEY:-}"}},
                        "email": {"smtp": {"password": "${DAYBRIEF_SMTP_PASSWORD:-}"}}})
    out = config.redact("auth failed for sk-test-1234567890 using 'abcd efgh ijkl mnop'")
    assert "sk-test-1234567890" not in out and "abcd efgh ijkl mnop" not in out


def test_redact_leaves_watchlist_terms_and_placeholders_alone(secrets, monkeypatch):
    # A variable name no real .env sets, so the placeholder default is what expands.
    monkeypatch.delenv("DAYBRIEF_TEST_UNSET_KEY", raising=False)
    config._expand_env({"keywords": ["semiconductor tariffs"], "author": "Jane Reporter",
                        "key": "${DAYBRIEF_TEST_UNSET_KEY:-YOUR_NYT_API_KEY}"})
    remembered = len(secrets)   # a count, so a failure never prints a value
    assert remembered == 0
    assert config.redact("semiconductor tariffs by Jane Reporter") == "semiconductor tariffs by Jane Reporter"


def test_logged_lines_and_tracebacks_are_redacted(secrets):
    import io
    import logging

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(config._RedactingFormatter("%(name)s %(message)s"))
    logger = logging.getLogger("httpx.test_redaction")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.warning("HTTP Request: GET https://api.nytimes.com/x.json?api-key=Nyt5ecretValue")
        try:
            raise RuntimeError("401 for url https://newsdata.io/api/1/market?apikey=pub_9f8e7d6c5b")
        except RuntimeError:
            logger.exception("request failed")
    finally:
        logger.removeHandler(handler)
    text = stream.getvalue()
    assert "Nyt5ecretValue" not in text and "pub_9f8e7d6c5b" not in text
    assert text.count("***") >= 2



def test_validate_warns_when_the_old_60s_ollama_timeout_is_still_set(monkeypatch):
    _patch(monkeypatch, GOOD_FEEDS, GOOD_SOURCES)
    monkeypatch.setattr(config, "settings", lambda: {"llm": {"provider": "ollama", "ollama": {"timeout": 60}}})
    assert any("llm.ollama.timeout is 60s" in w for w in config.validate())
    monkeypatch.setattr(config, "settings", lambda: {"llm": {"provider": "ollama", "ollama": {"timeout": 180}}})
    assert config.validate() == []
