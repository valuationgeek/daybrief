"""
text.py — Deterministic clean-up of LLM output and source text.

Pure functions with no heavy imports, shared by llm.py, analyzer.py and
fusion.py (and unit-testable without the ML stack):

  strip_meta            : drop word counts, sentiment verdicts and other
                          self-referential asides local models append
  fix_stale_descriptors : drop "former"/"late"/"outgoing" the model added
                          from background knowledge, not the source
  first_sentences       : abbreviation-safe extractive summary (LLM fallback)
  drop_partial_tail     : trim the dangling clause a length-capped
                          generation leaves behind
"""

import re

# Some local models append a meta line like "(Word count: 87 words)" or
# "Sentiment: + (positive due to ...)" to the summary despite the prompt
# forbidding it. Strip such lines after generation.
_WORDCOUNT_RE = re.compile(
    r"\s*[\(\[]?\s*word\s*count\s*[:=]?\s*\d+\s*(?:words?)?\s*[\)\]]?\.?\s*$",
    re.IGNORECASE,
)
# Line-start anchored: only strips lines that ARE a sentiment verdict, never
# prose that merely mentions sentiment (e.g. "investor sentiment soured").
_SENTIMENT_RE = re.compile(r"^\s*[\(\[]?\s*sentiment\s*[:=].*$", re.IGNORECASE)
# Parenthetical/bracketed asides some models tack onto the end, e.g.
# "(148 words)", "(Note: The summary is 42 words.)",
# "(Note: The summary adheres to the constraints...)", "(truncated for brevity)".
# Kept specific (self-referential phrasing) rather than a bare "word"/"note"
# keyword match, so genuine parenthetical content in article prose survives.
_META_PAREN_RES = [
    re.compile(
        r"\s*[\(\[]\s*(?:note\s*[:\-]?\s*)?(?:the\s+)?summary\s+(?:is|adheres|uses|contains)\b[^()\[\]]*[\)\]]\.?",
        re.IGNORECASE,
    ),
    re.compile(
        r"\s*[\(\[]\s*(?:note\s*[:\-]?\s*)?(?:approx(?:imately)?\.?\s*)?\d+\s*words?\s*[\)\]]\.?",
        re.IGNORECASE,
    ),
    re.compile(
        r"\s*[\(\[]\s*truncated\b[^()\[\]]*[\)\]]\.?",
        re.IGNORECASE,
    ),
    # e.g. "[the rest of the quote is cut off in the provided text]" or
    # "(Note: The article snippet is incomplete...)" — the model commenting
    # that its input looks cut off, regardless of word order/phrasing.
    re.compile(
        r"\s*[\(\[]"
        r"(?=[^()\[\]]*\b(?:cut\s*off|truncated|incomplete)\b)"
        r"(?=[^()\[\]]*\b(?:quote|text|snippet|excerpt|article|provided)\b)"
        r"[^()\[\]]*[\)\]]\.?",
        re.IGNORECASE,
    ),
]
_META_RES = [_WORDCOUNT_RE, _SENTIMENT_RE]

# Local models sometimes prepend a stale-knowledge title descriptor (e.g.
# "former President X") from their training data even when the source never
# uses that word. The prompts forbid it, but the rule doesn't always win
# against a strong prior. Deterministic backstop: if the word appears nowhere
# in the source text, it wasn't grounded in the article and gets dropped.
_STALE_DESCRIPTOR_RE = re.compile(r"\b(former|late|outgoing)\s+", re.IGNORECASE)

# ── Sentence splitting ──
# A naive `text.split(".")` shreds every abbreviation: "the U.S. Attorney's
# Office" becomes "…the U" / "S" / " Attorney's Office…", and "Oct. 10" or
# "$18.63" get cut mid-sentence. So: mask the dots inside abbreviations,
# split only on terminal punctuation followed by whitespace and a capitalised
# word, then unmask.
_ABBREVS = (
    "Mr", "Mrs", "Ms", "Dr", "Prof", "Sr", "Jr", "St", "Rev", "Gen", "Sen",
    "Rep", "Gov", "Lt", "Col", "Capt", "Sgt", "Inc", "Ltd", "Corp", "Co",
    "Plc", "LLC", "vs", "etc", "al", "approx", "est", "No", "Vol", "pp",
    "Jan", "Feb", "Mar", "Apr", "Jun", "Jul", "Aug", "Sep", "Sept", "Oct",
    "Nov", "Dec", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun",
)
_ABBREV_RE = re.compile(r"\b(?:" + "|".join(_ABBREVS) + r")\.", re.IGNORECASE)
# Runs of single letters each followed by a dot ("U.S.", "a.m.", "e.g."),
# plus a lone capital initial before a name ("J. Smith").
_DOTTED_RE = re.compile(r"\b(?:[A-Za-z]\.){2,}|\b[A-Z]\.(?=\s+[A-Z])")
# Next char must be a capital letter, not a digit — otherwise "Oct. 10" would
# still split for any abbreviation missing from the list above.
_SENT_END_RE = re.compile(r"(?<=[.!?])[\"')\]]?\s+(?=[\"'(\[]?[A-Z])")
# A decimal point ("$18.63", "3.7%") is never a sentence boundary.
_DECIMAL_RE = re.compile(r"(?<=\d)\.(?=\d)")
# Sentence terminator: must end the string or be followed by whitespace,
# optionally through a closing quote/bracket.
_TERMINATOR_RE = re.compile(r"[.!?][\"')\]]?(?=\s|$)")
_DOT_MASK = "\x00"   # never occurs in article text


def _mask_abbrevs(text: str) -> str:
    masked = _DOTTED_RE.sub(lambda m: m.group(0).replace(".", _DOT_MASK), text)
    return _ABBREV_RE.sub(lambda m: m.group(0).replace(".", _DOT_MASK), masked)


def first_sentences(text: str, count: int = 3, min_len: int = 20) -> str:
    """Extractive summary: the first `count` real sentences of `text`.

    Abbreviation-safe (see the regexes above). Each sentence keeps its own
    terminator, so nothing is truncated mid-clause. Returns "" when no
    sentence is longer than `min_len`.
    """
    if not text:
        return ""
    masked = _DECIMAL_RE.sub(_DOT_MASK, _mask_abbrevs(text))
    parts  = [s.strip() for s in _SENT_END_RE.split(masked) if len(s.strip()) > min_len]
    return " ".join(parts[:count]).replace(_DOT_MASK, ".").strip()


def drop_partial_tail(text: str) -> str:
    """Drop a trailing partial sentence left behind by a length-capped generation.

    When a provider hits its token cap it stops mid-clause — "…down 27% year
    over year, but remains" — and the dangling fragment would ship straight
    to the digest. Cut back to the last real sentence terminator instead,
    with the same abbreviation and decimal masking as `first_sentences` so
    "U.S." / "Oct." / "$18.63" are never mistaken for the end.

    Masking is char-for-char, so offsets in the masked copy map directly onto
    the original. If no terminator survives, the whole output is one partial
    sentence — keep it rather than returning nothing.
    """
    if not text:
        return text
    masked = _DECIMAL_RE.sub(_DOT_MASK, _mask_abbrevs(text))
    ends = [m.end() for m in _TERMINATOR_RE.finditer(masked)]
    if not ends:
        return text.strip()
    return text[:ends[-1]].strip()


def fix_stale_descriptors(summary: str, source_text: str) -> str:
    """Remove title/role descriptors the model added from background knowledge
    rather than the source text (see _STALE_DESCRIPTOR_RE)."""
    if not summary or not source_text:
        return summary

    def _maybe_strip(m):
        word = m.group(1)
        if re.search(r"\b" + re.escape(word) + r"\b", source_text, re.IGNORECASE):
            return m.group(0)  # grounded in the source — keep it
        return ""

    return _STALE_DESCRIPTOR_RE.sub(_maybe_strip, summary)


def strip_meta(summary: str) -> str:
    """Remove meta lines/asides (word counts, sentiment verdicts, self-referential
    notes) some local models emit despite the prompt forbidding them."""
    if not summary:
        return summary
    cleaned = _WORDCOUNT_RE.sub("", summary).rstrip()
    for rx in _META_PAREN_RES:
        cleaned = rx.sub("", cleaned)
    lines = [
        ln for ln in cleaned.splitlines()
        if not any(rx.match(ln.strip()) for rx in _META_RES)
    ]
    return "\n".join(lines).strip()
