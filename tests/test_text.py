"""Offline tests for agent/text.py — LLM-output clean-up helpers."""

from agent.text import drop_partial_tail, first_sentences, fix_stale_descriptors, strip_meta

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
