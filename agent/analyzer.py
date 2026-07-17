"""
analyzer.py — Stage 3: Summarization, keyword extraction, sentiment.

Each article gets:
  summary    : ≤150 words, factual, no filler
  read_angle : 1 crisp sentence — WHO should read this and WHY it matters to them
               (replaces the old "impact" which just repeated the summary)
  keywords   : up to 8 keyphrases (KeyBERT)
  sentiment / sentiment_label : VADER score

Language handling:
  Articles with language != "en" skip LLM summarization entirely.
  Their title is used as the summary and read_angle is left blank.
  This preserves Chinese articles as-is without translation.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from keybert import KeyBERT

from .llm import generate

log = logging.getLogger("daybrief.analyzer")

_kbert = None
_vader = SentimentIntensityAnalyzer()

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
_META_RES = [_WORDCOUNT_RE, _SENTIMENT_RE]


def _strip_meta(summary: str) -> str:
    """Remove meta lines (word counts, sentiment verdicts) some local models emit."""
    if not summary:
        return summary
    cleaned = _WORDCOUNT_RE.sub("", summary).rstrip()
    lines = [
        ln for ln in cleaned.splitlines()
        if not any(rx.match(ln.strip()) for rx in _META_RES)
    ]
    return "\n".join(lines).strip()


def _get_keybert():
    global _kbert
    if _kbert is None:
        log.info("Loading KeyBERT model...")
        _kbert = KeyBERT(model="all-MiniLM-L6-v2")
    return _kbert


# ─────────────────────────────────────────────────────────────
# Prompts
# ─────────────────────────────────────────────────────────────

_SUMMARY_PROMPT = """\
You are a professional news editor. Summarize the article below using ONLY \
information explicitly stated in the article.

This article was published on {date}. It describes the state of the world as \
of that date, which may be more recent than your training data.

Rules:
- Maximum 150 words
- ONLY use facts, names, numbers, and quotes that appear verbatim in the article
- Do NOT invent, infer, or add any figures, names, ticker symbols, or details \
not present in the article text
- Refer to people, companies, and their titles or roles EXACTLY as the article \
does. Do NOT add descriptors such as "former", "current", "late", or job \
titles from your own background knowledge — if the article says "President X", \
write "President X"
- Start directly with the news — no openers like "This article...", \
"The article reports...", "In summary..."
- Do NOT end with filler like "Further updates are expected" or "Stay tuned"
- Plain prose, no bullet points
- Do NOT include a word count, character count, or any meta-commentary about the summary itself
- Keep the sign of every figure exactly as the article states it (e.g. a \
"-3%" move stays negative) — do NOT flip, add, or remove "+"/"-" signs
- Do NOT append a sentiment label, rating, or verdict of any kind — output \
only the summary prose

Title: {title}

Article:
{body}

Summary:"""


_READ_ANGLE_PROMPT = """\
Read this news summary and write ONE short sentence (max 50 words) that tells \
potential types of readers why THIS story is worth their time. The reason(s) must state a downstream impact, risk, implications or \
action—not a fact already in the summary.

Format: "[Audience(s)] should note [specific reason(s)]."
Length: Maximum 50 words. Exactly one sentence ending with a period.
Audience(s): Must be a precise, identifiable demographic, professional role, or stakeholder group.
Content Focus: Extract downstream impact, risk, opportunity, or actionable consequence. Do NOT merely paraphrase or restate surface-level facts already present in the summary.
Grounding: Do NOT introduce facts, titles, or descriptors (e.g. "former", "current") that are not present in the summary.
Output: Return ONLY the formatted sentence. No greetings, explanations, markdown, or extra text.

Summary: {summary}

Read angle:"""


# ─────────────────────────────────────────────────────────────
# Summarization + Read Angle
# ─────────────────────────────────────────────────────────────

def _pub_date_str(published):
    """Human-readable publication date for the prompt; today if unknown."""
    try:
        dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        dt = datetime.now()
    return dt.strftime("%B %d, %Y")


def _summarize_and_angle(title, body, published=""):
    """
    Returns (summary, read_angle).

    summary     : ≤150 word factual summary, facts-only, no filler
    read_angle  : 1 sentence — specific audience + specific reason to read
                  e.g. "Investors in HK equities should note the rate decision cited."
    """
    # Step 1: summary
    # body cap ~12000 chars (~2000 words at ~6 chars/word) so long-form pieces
    # still get a fuller summary rather than being cut off at just the
    # opening section.
    summary = generate(
        _SUMMARY_PROMPT.format(
            title=title, body=body[:12000], date=_pub_date_str(published)
        ),
        max_tokens=250,
    )
    if not summary:
        sentences = [s.strip() for s in body.split(".") if len(s.strip()) > 20]
        summary   = ". ".join(sentences[:3]) + "."
    summary = _strip_meta(summary)

    # Step 2: read angle (uses summary as input — no re-sending full article)
    read_angle = generate(
        _READ_ANGLE_PROMPT.format(summary=summary),
        max_tokens=50,
    )
    read_angle = _strip_meta(read_angle)

    return summary, read_angle


# ─────────────────────────────────────────────────────────────
# Keyword extraction
# ─────────────────────────────────────────────────────────────

def _extract_keywords(text, top_n=8):
    if not text or len(text.split()) < 10:
        return []
    try:
        results = _get_keybert().extract_keywords(
            text,
            keyphrase_ngram_range=(1, 2),
            stop_words="english",
            top_n=top_n,
            use_mmr=True,
            diversity=0.5,
        )
        return [kw for kw, _ in results]
    except Exception as e:
        log.warning(f"KeyBERT failed: {e}")
        return []


# ─────────────────────────────────────────────────────────────
# Sentiment
# ─────────────────────────────────────────────────────────────

def _analyze_sentiment(text):
    if not text:
        return 0.0, "neutral"
    compound = _vader.polarity_scores(text[:1000])["compound"]
    label    = "positive" if compound >= 0.05 else ("negative" if compound <= -0.05 else "neutral")
    return round(compound, 4), label


# ─────────────────────────────────────────────────────────────
# Main entry points
# ─────────────────────────────────────────────────────────────

def analyze_article(article):
    title    = article.get("title", "")
    body     = article.get("body", "")
    language = article.get("language", "en")
    combined = f"{title}. {body}"

    if language != "en":
        # Non-English articles: skip LLM, keep original text as-is
        # Summary = title (no translation), read_angle = blank
        article["summary"]     = body[:300] if body else title
        article["read_angle"]  = ""
        article["impact"]      = ""   # keep for DB compatibility
    else:
        summary, read_angle    = _summarize_and_angle(title, body, article.get("published", ""))
        article["summary"]     = summary
        article["read_angle"]  = read_angle
        article["impact"]      = read_angle   # keep for DB compatibility

    article["keywords"]        = _extract_keywords(combined)
    article["sentiment"], article["sentiment_label"] = _analyze_sentiment(combined)
    return article


def analyze_all(articles, max_workers=3):
    log.info(f"Analyzing {len(articles)} articles...")
    results = [None] * len(articles)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(analyze_article, a): i for i, a in enumerate(articles)}
        for future in as_completed(futures):
            idx = futures[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                log.error(f"Analysis failed for article idx {idx}: {e}")
                results[idx] = articles[idx]

    return [r for r in results if r is not None]
