"""Offline tests for agent/analyzer.py — the LLM, KeyBERT and VADER are faked."""

import pytest

from agent import analyzer


@pytest.fixture
def llm(monkeypatch):
    """Script the LLM's replies and record the prompts it was sent."""
    prompts, replies = [], []

    def generate(prompt, max_tokens=300):
        prompts.append(prompt)
        return replies.pop(0) if replies else ""

    monkeypatch.setattr(analyzer, "generate", generate)
    monkeypatch.setattr(analyzer, "_extract_keywords", lambda text: [])
    monkeypatch.setattr(analyzer, "_analyze_sentiment", lambda text: (0.2, "positive"))
    return prompts, replies


BODY = ("Boris Johnson attacked the chancellor's budget on Tuesday, calling the tax rises "
        "a mistake that would slow growth across the country. ") * 3


def test_descriptor_grounded_only_in_the_headline_is_kept(llm):
    prompts, replies = llm
    replies += ["Former Prime Minister Boris Johnson attacked the budget.", "Voters should note it."]
    summary, _ = analyzer._summarize_and_angle("Former PM Johnson attacks budget", BODY)
    assert summary.startswith("Former Prime Minister")


def test_estimated_date_gives_the_prompt_no_publication_line(llm):
    prompts, replies = llm
    replies += ["Summary.", "Angle."]
    analyzer._summarize_and_angle("T", BODY, "2026-11-07T07:00:00", estimated=True)
    assert "was published in" not in prompts[0]
    analyzer._summarize_and_angle("T", BODY, "2026-11-07T07:00:00", estimated=False)
    assert "was published in November 2026" in prompts[2]


def test_no_read_angle_call_after_the_summary_fell_back(llm):
    prompts, _ = llm   # no replies: the summary call returns ""
    summary, angle = analyzer._summarize_and_angle("Johnson attacks budget", BODY)
    assert len(prompts) == 1 and angle == ""
    assert summary.startswith("Boris Johnson attacked")


def test_non_english_article_has_no_sentiment_label(llm):
    article = {"title": "央行降息", "body": "央行宣布下调利率。" * 20, "language": "zh"}
    analyzer.analyze_article(article)
    assert article["sentiment_label"] is None and article["sentiment"] == 0.0
    assert llm[0] == []   # non-English skips the LLM


def test_fusion_clusters_english_articles_only(monkeypatch):
    np = pytest.importorskip("numpy")
    pytest.importorskip("sklearn")
    from agent import fusion

    monkeypatch.setattr(fusion, "_embed_articles", lambda arts: np.ones((len(arts), 4)))
    monkeypatch.setattr(fusion, "generate", lambda prompt, max_tokens=300: "Unified.")
    arts = [{"id": str(i), "title": f"t{i}", "url": f"u{i}", "source": f"s{i}", "category": "world",
             "language": lang, "score": 0.5, "summary": "s", "keywords": []}
            for i, lang in enumerate(["en", "en", "zh"])]
    arts, clusters = fusion.fuse_all(arts)
    assert len(clusters) == 1 and clusters[0]["source_count"] == 2
    assert arts[2].get("cluster_id") is None
