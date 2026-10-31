"""Offline tests for agent/text.py — LLM-output clean-up helpers."""

from agent.text import (
    clip, drop_partial_tail, extractive_summary, first_sentences, fix_stale_descriptors,
    strip_meta, strip_ungrounded_dates,
)

BODY = (
    "Prosecutors at the U.S. Attorney's Office said the case was closed on Monday. "
    "In the Oct. 10 filing, revenue was $18.63 billion, up 3.7% from a year earlier. "
    "J. Smith, the company's lawyer, declined to comment on the matter. "
    "A fourth sentence that should not appear in a three-sentence summary."
)


# ── first_sentences ──────────────────────────────────────────

def test_first_sentences_keeps_abbreviations_intact():
    out = first_sentences(BODY)
    assert "the U.S. Attorney's Office" in out
    assert "the U. Attorney" not in out


def test_first_sentences_does_not_split_on_month_or_decimal():
    out = first_sentences(BODY)
    assert "In the Oct. 10 filing, revenue was $18.63 billion, up 3.7% from a year earlier." in out


def test_first_sentences_returns_three_sentences():
    out = first_sentences(BODY)
    assert out.endswith("declined to comment on the matter.")
    assert "fourth sentence" not in out


def test_first_sentences_empty_or_short_body_returns_empty():
    assert first_sentences("") == ""
    assert first_sentences("Too short.") == ""


# ── drop_partial_tail ────────────────────────────────────────

def test_drop_partial_tail_removes_dangling_clause():
    text = "Revenue rose 5% in the U.S. market. Shares are down 27% year over year, but remains"
    assert drop_partial_tail(text) == "Revenue rose 5% in the U.S. market."


def test_drop_partial_tail_never_cuts_at_a_decimal():
    text = "Sales hit $18.63 billion. Margins were 3.7"
    assert drop_partial_tail(text) == "Sales hit $18.63 billion."


def test_drop_partial_tail_keeps_text_without_any_terminator():
    assert drop_partial_tail("a fragment with no end") == "a fragment with no end"


def test_drop_partial_tail_leaves_complete_text_unchanged():
    text = 'He said "it is done." Then he left!'
    assert drop_partial_tail(text) == text


# ── strip_meta ───────────────────────────────────────────────

def test_strip_meta_removes_word_count_asides():
    assert strip_meta("Markets fell sharply. (148 words)") == "Markets fell sharply."
    assert strip_meta("Markets fell sharply. (Word count: 87 words)") == "Markets fell sharply."


def test_strip_meta_removes_self_referential_notes():
    out = strip_meta("Markets fell sharply.\n(Note: The summary is 42 words.)")
    assert out == "Markets fell sharply."


def test_strip_meta_removes_truncation_comments():
    out = strip_meta('He said "we will [the rest of the quote is cut off in the provided text]".')
    assert "cut off" not in out


def test_strip_meta_removes_sentiment_verdict_lines():
    out = strip_meta("Markets fell sharply.\nSentiment: negative (due to losses)")
    assert out == "Markets fell sharply."


def test_strip_meta_keeps_genuine_parentheticals():
    text = "The Federal Reserve (the Fed) held rates. Investor sentiment soured (again)."
    assert strip_meta(text) == text


# ── fix_stale_descriptors ────────────────────────────────────

def test_fix_stale_descriptors_drops_ungrounded_descriptor():
    out = fix_stale_descriptors("Former President X spoke on Monday.", "President X spoke on Monday.")
    assert out == "President X spoke on Monday."


def test_fix_stale_descriptors_keeps_grounded_descriptor():
    summary = "The former chief executive resigned."
    assert fix_stale_descriptors(summary, "The former chief executive resigned on Friday.") == summary


# ── splitter context rules (ported from the upstream pipeline) ──

def test_lowercase_no_ends_a_sentence():
    out = first_sentences("The board was asked to approve it and said no. Officials then left the meeting early.")
    assert out == "The board was asked to approve it and said no. Officials then left the meeting early."


def test_no_before_a_number_is_an_abbreviation():
    out = first_sentences("The company is now ranked No. 1 in the market by revenue. Rivals trail far behind it.")
    assert "ranked No. 1 in the market" in out


def test_class_a_then_sentence_opener_splits():
    text = "The company sold shares of Class A. The offering raised two billion dollars for the firm."
    assert drop_partial_tail(text + " It also") == text


# ── strip_ungrounded_dates ───────────────────────────────────

def test_ungrounded_leading_date_is_removed():
    out = strip_ungrounded_dates("On October 31, 2026, a tech sell-off hit stocks.", "Stocks fell on Friday.")
    assert out == "A tech sell-off hit stocks."


def test_grounded_date_is_kept_in_any_spelling():
    summary = "On October 30, the Dow fell 1.1%."
    assert strip_ungrounded_dates(summary, "The Dow fell 1.1% on Oct. 30.") == summary


def test_mid_sentence_date_and_its_comma_are_removed():
    out = strip_ungrounded_dates("Apple announced on Oct 31, that it would cut jobs.", "Apple will cut jobs.")
    assert out == "Apple announced that it would cut jobs."


# ── clip / extractive_summary ────────────────────────────────

def test_clip_cuts_on_a_word_boundary():
    out = clip("alpha beta gamma " * 40, limit=50)
    assert out.endswith("…") and len(out) <= 51
    assert not out[:-1].endswith(("alph", "bet", "gamm"))


def test_extractive_summary_is_bounded_without_sentence_breaks():
    body = "revenue rose 3% and 2024 sales grew while costs fell " * 200   # ~9,000 chars, no capitals
    out = extractive_summary(body)
    assert len(out) <= 301


def test_extractive_summary_non_english_is_clipped():
    assert len(extractive_summary("中文新闻内容" * 100, english=False)) <= 301


def test_curly_closing_quote_ends_a_sentence():
    text = "The minister said “we will act soon.” The markets rallied on the news today."
    assert first_sentences(text, count=1) == "The minister said “we will act soon.”"
    assert drop_partial_tail("He said “it is over.” Then he left the") == "He said “it is over.”"


def test_ascii_closing_quote_stays_with_its_sentence():
    text = 'The minister said "we will act soon." The markets rallied on the news today.'
    assert first_sentences(text, count=1) == 'The minister said "we will act soon."'
