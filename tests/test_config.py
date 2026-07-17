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
