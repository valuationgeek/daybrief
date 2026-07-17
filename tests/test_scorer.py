"""Offline tests for agent/scorer.py — config functions are monkeypatched."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from agent import config
from agent.scorer import (
    _recency_score,
    _watchlist_score,
    assign_flags,
    filter_by_barrier,
    score_article,
)

WEIGHTS = {
    "source_credibility": 0.30,
    "recency": 0.25,
    "watchlist_match": 0.25,
    "sentiment_magnitude": 0.15,
    "cluster_size": 0.05,
}


@pytest.fixture
def patched_config(monkeypatch):
    monkeypatch.setattr(config, "feeds", lambda: {"limits": {"max_age_hours": 12}})
    monkeypatch.setattr(
        config,
        "watchlist",
        lambda: {"topics": ["interest rates"], "companies": ["NVIDIA"], "regions": ["Europe"]},
    )
    monkeypatch.setattr(
        config,
        "sources",
        lambda: {"scoring": {"score_barriers": {"business": 0.50, "default": 0.40}}},
    )


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def test_recency_decays_linearly(patched_config):
    assert _recency_score(_iso(0)) == pytest.approx(1.0, abs=0.01)
    assert _recency_score(_iso(6)) == pytest.approx(0.5, abs=0.01)
    assert _recency_score(_iso(24)) == 0.0


def test_watchlist_match_levels(patched_config):
    topic = {"title": "Interest rates rise again", "body": ""}
    region = {"title": "Summit held in Europe", "body": ""}
    none = {"title": "Local bake sale", "body": "cakes"}
    assert _watchlist_score(topic) == 1.0
    assert _watchlist_score(region) == 0.7
    assert _watchlist_score(none) == 0.0


def test_score_article_composite(patched_config):
    article = {
        "credibility": 1.0,
        "published": _iso(0),
        "title": "Interest rates decision",
        "body": "",
        "sentiment": 0.5,
        "cluster_id": None,
    }
    score_article(article, [], WEIGHTS)
    # credibility 1.0*0.30 + recency ~1.0*0.25 + watchlist 1.0*0.25
    # + |sentiment| 0.5*0.15 + cluster 0.0*0.05  =  ~0.875
    assert article["score"] == pytest.approx(0.875, abs=0.01)


def test_assign_flags():
    thresholds = {"breaking_threshold": 0.82, "priority_threshold": 0.65}
    breaking = assign_flags({"score": 0.90, "sentiment": 0.0}, thresholds)
    priority = assign_flags({"score": 0.70, "sentiment": 0.0}, thresholds)
    crisis = assign_flags({"score": 0.10, "sentiment": -0.7}, thresholds)
    assert breaking["flags"] == ["BREAKING"]
    assert priority["flags"] == ["PRIORITY"]
    assert crisis["flags"] == ["CRISIS"]


def test_filter_by_barrier_uses_category_and_default(patched_config):
    shown, filtered = filter_by_barrier(
        [
            {"category": "business", "score": 0.60, "title": "a"},
            {"category": "business", "score": 0.45, "title": "b"},
            {"category": "brand_new", "score": 0.42, "title": "c"},  # default barrier 0.40
        ]
    )
    assert [a["title"] for a in shown] == ["a", "c"]
    assert [a["title"] for a in filtered] == ["b"]


def test_shipped_weights_sum_to_one():
    src = yaml.safe_load(
        (Path(__file__).parent.parent / "config" / "sources.yaml").read_text(encoding="utf-8")
    )
    total = sum(src["scoring"]["weights"].values())
    assert total == pytest.approx(1.0, abs=0.001)
