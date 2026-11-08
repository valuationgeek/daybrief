"""
preprocessor.py — Stage 2: Clean text, deduplicate, normalise metadata.
"""

import re
import logging
import hashlib
from datetime import datetime

log = logging.getLogger("daybrief.preprocessor")

# Boilerplate: a short trailing segment (at most 150 chars to the end of the
# line) that STARTS with one of these phrases, at the start of a line or of a
# sentence. The old patterns matched anywhere and ate everything after the
# phrase. They were written for short RSS leads, but enrichment now refetches
# nearly every English body, so "Meta changed its terms of service to let it
# train AI on …" lost the rest of its paragraph, or the whole article when
# trafilatura returned it as one line.
_BOILERPLATE = [
    r"subscribe to our newsletter",
    r"sign up for\b[^\n]{0,60}\bnewsletter",
    r"click here to",
    r"read more:",
    r"related:",
    r"sponsored content",
    r"share this article",
    r"follow us on",
    r"©[^\n]{0,80}all rights reserved",
    r"terms of (?:use|service)",
]
_BOILERPLATE_RE = re.compile(
    r"(?:^|(?<=[.!?] ))[ \t]*(?:" + "|".join(_BOILERPLATE) + r")[^\n]{0,150}$"
    r"|^[ \t]*advertisement[ \t]*$",   # only as a line of its own
    re.IGNORECASE | re.MULTILINE,
)

# Characters that are never visible text: C0/C1 control codes (except the
# whitespace ones, which the whitespace collapse handles), zero-width and
# bidi-control marks, and the BOM. Everything else is real text and stays:
# accents, curly quotes, dashes, €/£/¥, CJK. The zero-width joiner and
# non-joiner (U+200D, U+200C) stay too: Persian, the Indic scripts and emoji
# sequences need them to render correctly.
_INVISIBLE_RE = re.compile(
    "[\x00-\x08\x0e-\x1f\x7f-\x9f"
    "\u200b\u200e\u200f\u202a-\u202e\u2060-\u2064\ufeff]"
)


def _clean_text(text: str) -> str:
    if not text:
        return ""
    # Remove boilerplate
    text = _BOILERPLATE_RE.sub("", text)
    # Remove invisible characters. This used to be [^\x20-\x7E\n], which
    # deleted every non-ASCII character: Chinese bodies came out empty and
    # were dropped as "too short", and English text lost its curly quotes,
    # accents, em dashes and currency signs ("£50m" became "50m").
    text = _INVISIBLE_RE.sub("", text)
    # Collapse whitespace
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def _content_hash(title: str, body: str) -> str:
    """Hash for near-duplicate detection on same-source reposts."""
    content = f"{title.lower().strip()} {body[:200].lower().strip()}"
    return hashlib.md5(content.encode()).hexdigest()


def process(articles: list[dict]) -> list[dict]:
    """
    Clean and deduplicate a list of raw article dicts.
    Returns only articles with sufficient content.
    """
    seen_hashes = set()
    result = []

    for article in articles:
        title = article.get("title", "").strip()
        body = _clean_text(article.get("body", ""))
        article["title"] = title
        article["body"] = body

        # Drop if no title or very short body
        if not title:
            log.debug(f"Dropped (no title): {article.get('url')}")
            continue
        if len(body) < 80:
            log.debug(f"Dropped (too short): {title[:60]}")
            continue

        # Near-duplicate check
        chash = _content_hash(title, body)
        if chash in seen_hashes:
            log.debug(f"Dropped (duplicate): {title[:60]}")
            continue
        seen_hashes.add(chash)

        # Normalise published date
        pub = article.get("published", "")
        try:
            dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            article["published"] = dt.isoformat()
        except Exception:
            article["published"] = datetime.utcnow().isoformat()
            article["published_estimated"] = True   # not when it came out — see analyzer._date_line

        # Word count
        article["word_count"] = len(body.split())

        result.append(article)

    log.info(f"Pre-processing: {len(articles)} in → {len(result)} clean articles out")
    return result
