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
  strip_ungrounded_dates: drop calendar dates the source never states
  log_ungrounded_numbers: log (never remove) figures not in the source
  clip / extractive_summary : bounded fallbacks when the LLM is unavailable
"""

import logging
import re

log = logging.getLogger("daybrief.text")

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
#
# Matched case-sensitively: news text capitalises the real abbreviations, and
# a case-insensitive "No"/"Sun"/"St" also swallowed genuine sentence ends like
# "the board said no." Words that double as ordinary English only count as
# abbreviations in context: "No." before a number ("No. 1"), a month before a
# day ("Oct. 31"), a weekday before a comma or day ("Fri., Oct 30").
_ABBREVS = (
    "Mr", "Mrs", "Ms", "Dr", "Prof", "Sr", "Jr", "St", "Rev", "Gen", "Sen",
    "Rep", "Gov", "Lt", "Col", "Capt", "Sgt", "Inc", "Ltd", "Corp", "Co",
    "Plc", "LLC", "vs", "etc", "al", "approx", "Vol", "pp",
)
_MONTH_ABBREVS = ("Jan", "Feb", "Mar", "Apr", "Jun", "Jul", "Aug", "Sept", "Sep",
                  "Oct", "Nov", "Dec")
_DAY_ABBREVS   = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_ABBREV_RE = re.compile(
    r"\b(?:" + "|".join(_ABBREVS) + r")\."
    r"|\bNo\.(?=\s*\d)"
    r"|\b(?:" + "|".join(_MONTH_ABBREVS) + r")\.(?=\s*\d)"
    r"|\b(?:" + "|".join(_DAY_ABBREVS) + r")\.(?=\s*[,\d])"
)
# Runs of single letters each followed by a dot ("U.S.", "a.m.", "e.g."),
# plus a lone capital initial before a name ("J. Smith"). The lone initial is
# NOT masked when the next word is a common sentence opener, so "Class A. The
# offering" still splits. Telling "Plan B. Regulators said" from "J. Smith"
# in general would need real NLP — that residual case is accepted.
_SENT_OPENERS = ("The", "A", "An", "It", "He", "She", "They", "We", "This", "That",
                 "These", "Those", "In", "On", "At", "But", "And", "Officials", "Shares")
_DOTTED_RE = re.compile(
    r"\b(?:[A-Za-z]\.){2,}"
    r"|\b[A-Z]\.(?=\s+(?!(?:" + "|".join(_SENT_OPENERS) + r")\b)[A-Z])"
)
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


# The model copies dates into summaries that the article never states —
# typically the publication date from the prompt, restated as the event date
# ("On October 31, 2026, a tech sell-off…" for an AP story about Friday's
# Oct 30 close). The prompt forbids it; this is the deterministic backstop.
_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December")
_MONTH_RX = (
    r"(?:" + "|".join(_MONTH_NAMES)
    + r"|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)\b\.?"
)
_ORD = r"(?:st|nd|rd|th)?"
_DATE_RE = re.compile(
    rf"(?P<m1>{_MONTH_RX})\s+(?P<d1>\d{{1,2}}){_ORD}(?!\d)(?:,?\s+(?P<y1>\d{{4}})\b)?"
    rf"|(?<!\d)(?P<d2>\d{{1,2}}){_ORD}\s+(?P<m2>{_MONTH_RX})(?:,?\s+(?P<y2>\d{{4}})\b)?"
    r"|\b(?P<y3>\d{4})-(?P<mo3>\d{2})-(?P<d3>\d{2})\b"
)

# After removing " on <date>", a following ", that…" / ", after…" comma was
# only the date's closing comma and must go too.
_CONNECTIVE_RE = re.compile(
    r",\s+(?:that|to|after|before|following|amid|as|when|with|in|for|from|"
    r"despite|because|due|according)\b"
)
_MONTH_YEAR_LEAD_RE = re.compile(
    r"(?:^|(?<=[.!?]\s))(?:In|As of|During)\s+"
    r"(?P<m>" + "|".join(_MONTH_NAMES) + r")\s+(?P<y>\d{4}),\s+"
)
_CLAUSE_THEN_VERB_RE = re.compile(
    r",\s+(?:(?!(?:his|its|this|thus|plus|as|us)\b)\w+s\b"
    r"|(?:will|would|have|had|are|were|can|could|may|might|should)\b)"
)
_WEEKDAY_COMMA_RE = re.compile(
    r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), $"
)


def _month_index(name: str) -> int:
    return [m[:3].lower() for m in _MONTH_NAMES].index(name[:3].lower()) + 1


def _date_in_source(month: int, day: int, source: str) -> bool:
    """True if `source` names this month/day in any common spelling
    ("October 31", "Oct. 31", "31 October", "2026-10-31"). The year is not
    required — articles routinely omit it."""
    full = _MONTH_NAMES[month - 1]
    spellings = rf"(?:{full}|{full[:3]}{'t?' if month == 9 else ''})\.?"
    rx = (
        rf"\b{spellings}\s+0?{day}{_ORD}(?!\d)"
        rf"|(?<!\d)0?{day}{_ORD}\s+{spellings}\b"
        rf"|\d{{4}}-{month:02d}-{day:02d}\b"
    )
    return re.search(rx, source, re.IGNORECASE) is not None


def strip_ungrounded_dates(summary: str, source_text: str) -> str:
    """Remove calendar dates the source text never states.

    Only phrasings that delete cleanly are removed — a sentence-leading
    "On <date>, ", a mid-sentence " on <date>", or a parenthetical
    ", <date>," — so the grammar survives. Any other ungrounded date is
    logged and left in place.
    """
    if not summary or not source_text:
        return summary

    changed = False
    for m in reversed(list(_DATE_RE.finditer(summary))):
        try:
            if m.group("y3"):
                month, day = int(m.group("mo3")), int(m.group("d3"))
            else:
                month = _month_index(m.group("m1") or m.group("m2"))
                day   = int(m.group("d1") or m.group("d2"))
        except (ValueError, IndexError):
            continue
        if not 1 <= month <= 12 or _date_in_source(month, day, source_text):
            continue

        before, after = summary[:m.start()], summary[m.end():]
        lead = re.search(r"(?:^|(?<=[.!?]\s))(?:On|As of|By)\s+$", before)
        mid  = re.search(r"\s+(?:on|as of)\s+$", before)
        if lead:
            # "On Oct 31, 2026, a sell-off…" → "A sell-off…"
            after   = after.lstrip(",").lstrip()
            summary = before[:lead.start()] + after[:1].upper() + after[1:]
        elif mid:
            # "announced on Oct 31, 2026, that…" → "announced that…", but
            # "fell 2% on Oct 31, its worst day" keeps the comma.
            if _CONNECTIVE_RE.match(after):
                after = after[1:]
            head = before[:mid.start()]
            # "the model, announced on Oct 31, claims…" would leave an empty
            # "model, announced, claims" — drop the bare participle clause.
            # Only when a verb follows ("claims", "shows", "has"): in
            # "Johnson, R-La., reaffirmed on Oct 31, his commitment" the
            # "-ed" word is the main verb and must stay.
            # Otherwise, after an "-ed" verb the comma only closed the date:
            # "announced on Oct 31, 2026, a partnership" → "announced a…".
            clause = re.search(r",\s+\w+ed$", head)
            if clause and _CLAUSE_THEN_VERB_RE.match(after):
                head, after = head[:clause.start()], after[1:]
            elif re.search(r"\w+ed$", head) and after.startswith(","):
                after = after[1:]
            summary = head + after
        elif _WEEKDAY_COMMA_RE.search(before):
            # "on Friday, Oct 31, after…" → "on Friday after…"
            summary = before[:-2] + (after[1:] if after.startswith(",") else after)
        elif before.endswith(", ") and after.startswith(","):
            summary = before[:-2] + after[1:]
        elif before.endswith(" (") and after.startswith(")"):
            # "(September 17, 2026)"
            summary = before[:-2] + after[1:]
        else:
            log.warning(f"Ungrounded date {m.group(0)!r} left in summary (no clean cut)")
            continue
        log.warning(f"Stripped ungrounded date {m.group(0)!r} from summary")
        changed = True

    # With only the month in the prompt, the model may open "In October
    # 2026, …" instead — accurate, but the same prompt-copied filler.
    for m in reversed(list(_MONTH_YEAR_LEAD_RE.finditer(summary))):
        spell = rf"(?:{m.group('m')}|{m.group('m')[:3]})\.?"
        if re.search(rf"\b{spell}\s+{m.group('y')}\b", source_text, re.IGNORECASE):
            continue
        after   = summary[m.end():]
        summary = summary[:m.start()] + after[:1].upper() + after[1:]
        log.warning(f"Stripped ungrounded opener {m.group(0).strip()!r} from summary")
        changed = True

    if changed:
        summary = re.sub(r" {2,}", " ", summary).replace(" ,", ",")
        # "to be, as of <date>. However" leaves "to be,. However"
        summary = re.sub(r",\s*([.,;:])", r"\1", summary).strip()
    return summary


_NUM_RE = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")


def log_ungrounded_numbers(summary: str, source_text: str, title: str = "") -> None:
    """Log figures in the summary that don't appear in the source.

    Log-only by design: the model legitimately rounds (2.86 → 2.9) and
    computes, so removing them would cut real facts. This gives a trail to
    grep when a figure in the digest looks wrong. 1–2 digit integers are
    skipped as counting/rounding noise.
    """
    if not summary or not source_text:
        return
    src = re.sub(r"(?<=\d),(?=\d)", "", source_text)
    missing = []
    for raw in _NUM_RE.findall(summary):
        n = raw.replace(",", "")
        if "." not in n and len(n) <= 2:
            continue
        if not re.search(r"(?<![\d.])" + re.escape(n) + r"(?!\d)", src):
            missing.append(raw.rstrip(","))
    if missing:
        log.info(f"Figures not found verbatim in source for {title[:60]!r}: {missing}")


def clip(text: str, limit: int = 300) -> str:
    """Cut `text` to about `limit` chars on a word boundary, marked with "…".

    Text without spaces near the cut (Chinese) gets a hard cut instead.
    """
    if not text or len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space >= limit - 60:
        cut = cut[:space]
    return cut.rstrip(" ,;:") + "…"


# A real three-sentence lead is well under this. Anything longer means the
# splitter found no boundaries (lowercase/digit sentence starts, unbroken
# text) and would otherwise ship the whole — now fully enriched — body.
_EXTRACTIVE_MAX = 600


def extractive_summary(body: str, english: bool = True) -> str:
    """Bounded fallback summary for when the LLM is unavailable.

    English bodies get their first sentences; anything the splitter can't
    handle (non-English, no sentence breaks) gets a 300-char word-boundary
    clip instead — never the whole body.
    """
    summary = first_sentences(body) if english else ""
    if not summary or len(summary) > _EXTRACTIVE_MAX:
        summary = clip(body)
    return summary
