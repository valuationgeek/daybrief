"""
decision.py — Stage 6: Trend detection, alerts, and actionable insights.

Fix #1: Breaking news deduplication — a story flagged BREAKING won't also
        appear as a "top story" in the category insight below it.
"""

import logging
from collections import Counter
from datetime import datetime

from . import config, db

log = logging.getLogger("daybrief.decision")


def detect_trends(articles):
    """Surface keywords whose frequency today spikes above their recent baseline.

    Why a spike *ratio* and not a z-score: KeyBERT emits diverse 1–2 word
    keyphrases, so any given phrase almost always appears exactly once on a
    given day. That makes per-day counts near-constant (variance ~0), so the
    old z-score approach could never fire — the standard deviation of [1,1,1]
    is 0 and the test was skipped. Instead we compare today's count to the
    average daily frequency over the lookback window (absent days counted as
    zero) using a smoothed ratio, which is well-defined even for brand-new
    keywords and constant histories.
    """
    trend_cfg = config.sources().get("trends", {})
    lookback  = trend_cfg.get("lookback_days", 14)
    min_freq  = trend_cfg.get("min_frequency", 2)
    min_spike = trend_cfg.get("min_spike_factor", 2.0)
    smoothing = trend_cfg.get("baseline_smoothing", 0.5)
    today     = datetime.utcnow().date().isoformat()

    today_kw    = Counter()
    all_kw_flat = []
    for a in articles:
        today_kw.update(a.get("keywords", []))
        all_kw_flat.extend(a.get("keywords", []))

    # Record today's frequencies first so future runs have a baseline.
    db.record_keyword_frequencies(today, "all", all_kw_flat)

    trending = []
    for keyword, today_count in today_kw.items():
        # Require a minimum mention count today to filter out one-off noise
        # (the long tail of unique single-mention phrases).
        if today_count < min_freq:
            continue

        history     = db.get_keyword_history(keyword, lookback_days=lookback)
        prior_total = sum(h["count"] for h in history if h["date"] != today)
        baseline    = prior_total / lookback                 # absent days = 0
        spike       = today_count / (baseline + smoothing)   # smoothed ratio

        if spike >= min_spike:
            trending.append({
                "keyword":         keyword,
                "today_count":     today_count,
                "historical_mean": round(baseline, 2),
                "spike_factor":    round(spike, 1),
            })

    # Rank by spike strength, then absolute volume so a 4-mention spike
    # outranks a 2-mention spike at the same ratio.
    trending.sort(key=lambda x: (x["spike_factor"], x["today_count"]), reverse=True)
    log.info(f"Detected {len(trending)} trending topics")
    return trending[:10]


def generate_insights(articles, clusters, trends):
    """
    Generate specific, meaningful insights.
    Key fix: track already_mentioned_urls so the same story never
    appears twice across different insight types.
    """
    insights          = []
    already_mentioned = set()   # tracks article URLs already cited in insights
    feed_cfg          = config.feeds()["categories"]
    wl                = config.watchlist()

    # ── 1. Breaking alerts — name the actual story ────────────
    breaking_all = [a for a in articles if "BREAKING" in a.get("flags", [])]
    for a in breaking_all[:2]:
        title = a.get("title", "")[:80]
        src   = a.get("source", "")
        url   = a.get("url", "")
        insights.append(f"🔴 BREAKING — {src}: \"{title}\"")
        already_mentioned.add(url)   # mark so it won't repeat below

    # ── 2. Multi-source convergence (3+ outlets same story) ───
    large_clusters = sorted(
        [c for c in clusters if c.get("source_count", 0) >= 3],
        key=lambda c: c.get("score", 0), reverse=True,
    )
    for c in large_clusters[:2]:
        sources = ", ".join(c.get("sources", [])[:3])
        kw      = c.get("top_keywords", ["this event"])[0]
        n       = c["source_count"]
        insights.append(
            f"📡 Convergence — {n} outlets ({sources}) all reporting on '{kw}'. "
            f"Likely a significant development worth reading in full."
        )

    # ── 3. Sentiment signal ───────────────────────────────────
    if len(articles) >= 5:
        neg     = [a for a in articles if a.get("sentiment_label") == "negative"]
        pos     = [a for a in articles if a.get("sentiment_label") == "positive"]
        neg_pct = len(neg) / len(articles)
        pos_pct = len(pos) / len(articles)

        if neg_pct >= 0.65:
            worst = min(neg, key=lambda a: a.get("sentiment", 0))
            insights.append(
                f"⚠️ Negative tone dominates today ({int(neg_pct*100)}% of stories). "
                f"Most critical: \"{worst.get('title','')[:70]}\""
            )
        elif pos_pct >= 0.60:
            insights.append(
                f"✅ Broadly positive news day ({int(pos_pct*100)}% positive tone across all categories)."
            )

    # ── 4. Top story per category — skip if already mentioned ─
    for cat_key, cat_meta in feed_cfg.items():
        cat_articles = [a for a in articles if a["category"] == cat_key]
        if not cat_articles:
            continue
        top = sorted(cat_articles, key=lambda a: a.get("score", 0), reverse=True)

        # Find the highest-scored story NOT already cited above
        top_new = [a for a in top if a.get("url", "") not in already_mentioned]
        if not top_new:
            continue

        best  = top_new[0]
        score = best.get("score") or 0
        if score < 0.70:
            continue

        label = cat_meta["label"]
        title = best.get("title", "")[:75]
        src   = best.get("source", "")
        flags = best.get("flags", [])
        flag  = " 🔴" if "BREAKING" in flags else (" 🟡" if "PRIORITY" in flags else "")
        insights.append(f"📌 {label}{flag} — Top story: \"{title}\" ({src})")
        already_mentioned.add(best.get("url", ""))

    # ── 5. Watchlist matches ──────────────────────────────────
    wl_topics    = wl.get("topics", [])
    wl_companies = wl.get("companies", [])
    matched      = []
    for a in articles:
        text          = f"{a.get('title','')} {a.get('body','')}".lower()
        matched_term  = next((t for t in wl_topics    if t.lower() in text), None)
        matched_term  = matched_term or next((c for c in wl_companies if c.lower() in text), None)
        if matched_term:
            matched.append((a, matched_term))

    if matched:
        by_term   = Counter(term for _, term in matched)
        top_terms = by_term.most_common(3)
        term_str  = ", ".join(f"'{t}' ({n})" for t, n in top_terms)
        insights.append(
            f"🔔 Watchlist — {len(matched)} article(s) match your tracked topics: {term_str}"
        )

    # ── 6. Trending keywords ──────────────────────────────────
    for t in trends[:3]:
        insights.append(
            f"📈 Trend spike — '{t['keyword']}' mentioned {t['today_count']}× today "
            f"vs. avg {t['historical_mean']}× ({t['spike_factor']}× above baseline)."
        )

    # ── 7. Market caution signal ──────────────────────────────
    # Categories to watch come from sources.yaml → insights.market_signal_categories.
    # Categories listed there but absent from feeds.yaml simply never match.
    insight_cfg = config.sources().get("insights", {})
    signal_cats = insight_cfg.get("market_signal_categories", ["business"])
    min_neg     = insight_cfg.get("min_negative_for_caution", 3)
    for cat in signal_cats:
        cat_articles = [a for a in articles if a["category"] == cat]
        neg = [a for a in cat_articles if a.get("sentiment_label") == "negative"]
        if len(neg) >= min_neg:
            label = feed_cfg.get(cat, {}).get("label", cat)
            insights.append(
                f"💹 Market caution — {len(neg)} negative-tone stories in "
                f"{label} today. Consider reviewing portfolio exposure."
            )

    return insights


def get_alerts(articles):
    threshold = (
        config.watchlist()
        .get("alerts", {})
        .get("immediate_telegram_threshold", 0.90)
    )
    alerts = [
        a for a in articles
        if (a.get("score") or 0) >= threshold or "BREAKING" in a.get("flags", [])
    ]
    return sorted(alerts, key=lambda a: a.get("score", 0), reverse=True)
