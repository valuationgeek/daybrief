"""Offline tests for agent/preprocessor.py — no network, no LLM."""

from datetime import datetime

from agent.preprocessor import process


def _article(**overrides):
    base = {
        "id": "test001",
        "title": "Federal Reserve Raises Interest Rates",
        "url": "https://example.com/fed",
        "source": "Test Source",
        "category": "business",
        "published": datetime.utcnow().isoformat(),
        "fetched_at": datetime.utcnow().isoformat(),
        "body": (
            "The Federal Reserve raised interest rates by 25 basis points on "
            "Wednesday, marking another consecutive hike as it battles inflation "
            "toward the two percent target set by policymakers."
        ),
        "credibility": 0.9,
        "summary": None,
        "keywords": [],
        "sentiment": None,
        "sentiment_label": None,
        "score": None,
        "flags": [],
        "cluster_id": None,
    }
    base.update(overrides)
    return base


def test_keeps_valid_article_and_sets_word_count():
    result = process([_article()])
    assert len(result) == 1
    assert result[0]["word_count"] == len(result[0]["body"].split())


def test_drops_duplicate_content():
    result = process([_article(), _article(id="test002", url="https://example.com/other")])
    assert len(result) == 1


def test_drops_missing_title_and_short_body():
    result = process(
        [
            _article(title=""),
            _article(id="short", url="https://example.com/short", body="Too short."),
        ]
    )
    assert result == []


def test_strips_boilerplate():
    body = _article()["body"] + " Subscribe to our newsletter for more updates."
    result = process([_article(body=body)])
    assert len(result) == 1
    assert "subscribe" not in result[0]["body"].lower()


def test_normalises_bad_published_date():
    result = process([_article(published="not-a-date")])
    assert len(result) == 1
    # Must be replaced with a parseable ISO timestamp
    datetime.fromisoformat(result[0]["published"])


def test_chinese_body_survives_cleaning():
    body = "香港特區政府今日公布新一輪防疫措施，" * 6   # >80 chars, no ASCII at all
    result = process([_article(title="港府公布新措施", body=body)])
    assert len(result) == 1
    assert result[0]["body"] == body


def test_typography_and_currency_signs_are_kept():
    body = ("The bank’s chief said “rates will rise” — café owners in Zürich expect "
            "£50m and €12bn in new costs, while ¥ deposits fell.")
    result = process([_article(body=body)])
    assert result[0]["body"] == body


def test_invisible_characters_are_removed():
    body = ("The\u200b Federal\ufeff Reserve\x07 raised rates\u00a0\u00a0by 25 basis points "
            "on Wednesday as it battles inflation toward its target.")
    cleaned = process([_article(body=body)])[0]["body"]
    assert cleaned.startswith("The Federal Reserve raised rates by 25 basis points")
    assert not any(ch in cleaned for ch in "\u200b\ufeff\x07\u00a0")


def _clean(text):
    from agent.preprocessor import _clean_text
    return _clean_text(text)


def test_phrase_inside_a_sentence_is_not_boilerplate():
    body = ("Meta changed its terms of service to let it train AI on public posts, and regulators "
            "in Europe said they would review the change before it takes effect next year.")
    assert _clean(body) == body


def test_short_boilerplate_tail_and_lines_are_removed():
    body = ("The Fed held rates steady on Wednesday. Read more: https://example.com/fed\n"
            "Advertisement\n"
            "Officials signalled two cuts later this year.\n"
            "© 2026 Example News. All rights reserved.")
    cleaned = _clean(body)
    assert "Read more" not in cleaned and "Advertisement" not in cleaned and "rights reserved" not in cleaned
    assert "The Fed held rates steady on Wednesday." in cleaned
    assert "Officials signalled two cuts later this year." in cleaned


def test_advertisement_inside_prose_is_kept():
    body = "The advertisement market grew 8% as brands returned to television this quarter."
    assert _clean(body) == body


def test_zero_width_joiners_are_kept():
    persian = "می‌خواهم"          # ZWNJ inside a Persian word
    family = "\U0001F468‍\U0001F469‍\U0001F467"   # ZWJ emoji sequence
    cleaned = _clean(f"{persian} {family} ​visible")
    assert "‌" in cleaned and "‍" in cleaned and "​" not in cleaned
