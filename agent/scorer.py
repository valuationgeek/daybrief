"""
scorer.py — Stage 5: Composite importance scoring, flag assignment,
            and score-barrier filtering.

Score barrier usage
───────────────────
After scoring, call filter_by_barrier(articles) to drop articles that fall
below the configured minimum score for their category.
Both email_digest.py and obsidian.py call this automatically — you don't
need to call it manually unless you add a new output module.

How the composite score is built
─────────────────────────────────
score = Σ (component_value × weight)

  component         range    controlled by
  ─────────────────────────────────────────────────────────────
  source_credibility  0–1    sources.yaml → sources
  recency             0–1    decays linearly over max_age_hours
  watchlist_match     0–1    watchlist.yaml topics / companies
  sentiment_magnitude 0–1    abs(VADER compound score)
  cluster_size        0–1    how many outlets cover same story

All weights must sum to 1.0 (set in sources.yaml → scoring.weights).
"""

import logging
from datetime import datetime, timezone

from . import config

log = logging.getLogger("daybrief.scorer")


# ─────────────────────────────────────────────────────────────
# Score components
# ─────────────────────────────────────────────────────────────

def _recency_score(published: str) -> float:
    """1.0 = just published; decays linearly to 0.0 at max_age_hours."""
    max_age = config.feeds().get("limits", {}).get("max_age_hours", 12)
    try:
        dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        return max(0.0, 1.0 - (age_hours / max_age))
    except Exception:
        return 0.5


def _watchlist_score(article: dict) -> float:
    """
    1.0  — matches a topic or company in watchlist.yaml
    0.7  — matches a region in watchlist.yaml
    0.0  — no match
    """
    wl   = config.watchlist()
    text = f"{article.get('title','')} {article.get('body','')}".lower()

    for topic in wl.get("topics", []):
        if topic.lower() in text:
            return 1.0
    for company in wl.get("companies", []):
        if company.lower() in text:
            return 1.0
    for region in wl.get("regions", []):
        if region.lower() in text:
            return 0.7
    return 0.0


def _cluster_size_score(article: dict, clusters: list) -> float:
    """
    Bonus for stories covered by multiple outlets.
    2 sources → 0.25 | 3 → 0.50 | 4 → 0.75 | 5+ → 1.0
    """
    cid = article.get("cluster_id")
    if not cid:
        return 0.0
    for c in clusters:
        if c["cluster_id"] == cid:
            n = c.get("source_count", 1)
            return min(1.0, (n - 1) * 0.25)
    return 0.0


# ─────────────────────────────────────────────────────────────
# Score an individual article
# ─────────────────────────────────────────────────────────────

def score_article(article: dict, clusters: list, weights: dict) -> dict:
    """
    Compute composite score and write it to article["score"].
    To add a new scoring component:
      1. Write a function above that returns 0.0–1.0
      2. Add its name and weight to sources.yaml → scoring.weights
      3. Add one line here: new_component * weights.get("your_key", 0.0)
    """
    credibility    = article.get("credibility", 0.60)
    recency        = _recency_score(article.get("published", ""))
    watchlist      = _watchlist_score(article)
    sentiment_mag  = abs(article.get("sentiment") or 0.0)
    cluster_bonus  = _cluster_size_score(article, clusters)

    composite = (
        credibility   * weights.get("source_credibility",  0.30) +
        recency       * weights.get("recency",             0.35) +
        watchlist     * weights.get("watchlist_match",     0.25) +
        sentiment_mag * weights.get("sentiment_magnitude", 0.05) +
        cluster_bonus * weights.get("cluster_size",        0.05)
    )
    article["score"] = round(min(1.0, composite), 4)
    return article


# ─────────────────────────────────────────────────────────────
# Score barrier — filter articles below the minimum
# ─────────────────────────────────────────────────────────────

def get_barrier(category: str) -> float:
    """
    Return the score barrier for a given category key.
    Looks up sources.yaml → scoring.score_barriers.
    Falls back to the 'default' barrier, then 0.0.
    """
    barriers = (
        config.sources()
        .get("scoring", {})
        .get("score_barriers", {})
    )
    return float(barriers.get(category, barriers.get("default", 0.0)))


def filter_by_barrier(articles: list) -> tuple:
    """
    Split articles into (shown, filtered_out) based on per-category barriers.
    'shown'        — score >= barrier for their category
    'filtered_out' — score <  barrier (logged, kept in DB, not shown in output)
    """
    shown        = []
    filtered_out = []

    for a in articles:
        cat     = a.get("category", "")
        barrier = get_barrier(cat)
        if (a.get("score") or 0.0) >= barrier:
            shown.append(a)
        else:
            filtered_out.append(a)

    if filtered_out:
        log.info(
            f"Score barrier filtered out {len(filtered_out)} articles "
            f"({len(shown)} remain for output)"
        )
        for a in filtered_out[:5]:   # log first 5 as a sample
            log.debug(
                f"  Filtered [{a['category']}] score={a.get('score',0):.3f} "
                f"barrier={get_barrier(a['category'])} — \"{a.get('title','')[:60]}\""
            )

    return shown, filtered_out


def filter_clusters_by_barrier(clusters: list) -> list:
    """
    For clusters (multi-source), use the HIGHEST barrier across all categories
    represented in the cluster. This ensures a cross-category cluster is only
    shown if it clears the strictest bar.
    """
    shown = []
    for c in clusters:
        # Use 'categories' list if present, fall back to single 'category'
        cats    = c.get("categories") or [c.get("category", "")]
        barrier = max(get_barrier(cat) for cat in cats if cat)
        if (c.get("score") or 0.0) >= barrier:
            shown.append(c)
        else:
            log.debug(
                f"Cluster {c['cluster_id']} filtered out — "
                f"score={c.get('score',0):.3f} barrier={barrier}"
            )
    return shown


# ─────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────

def score_all(articles: list, clusters: list) -> list:
    """Score every article and assign BREAKING / PRIORITY / CRISIS flags."""
    src_cfg    = config.sources()
    weights    = src_cfg["scoring"]["weights"]
    thresholds = src_cfg["scoring"]

    log.info(f"Scoring {len(articles)} articles...")
    for article in articles:
        score_article(article, clusters, weights)
        assign_flags(article, thresholds)

    # Propagate flags up to clusters
    for cluster in clusters:
        cid     = cluster["cluster_id"]
        related = [a for a in articles if a.get("cluster_id") == cid]
        if any("BREAKING" in a.get("flags", []) for a in related):
            if "BREAKING" not in cluster["flags"]:
                cluster["flags"].append("BREAKING")
        elif any("PRIORITY" in a.get("flags", []) for a in related):
            if "PRIORITY" not in cluster["flags"]:
                cluster["flags"].append("PRIORITY")

    articles.sort(key=lambda a: a.get("score", 0), reverse=True)

    breaking = sum(1 for a in articles if "BREAKING" in a.get("flags", []))
    priority = sum(1 for a in articles if "PRIORITY" in a.get("flags", []))
    log.info(f"Flagged: {breaking} BREAKING, {priority} PRIORITY")
    return articles


def assign_flags(article: dict, thresholds: dict) -> dict:
    flags = []
    score = article.get("score", 0)

    if score >= thresholds.get("breaking_threshold", 0.82):
        flags.append("BREAKING")
    elif score >= thresholds.get("priority_threshold", 0.65):
        flags.append("PRIORITY")

    # CRISIS: highly negative sentiment regardless of score
    if (article.get("sentiment") or 0) <= -0.6:
        flags.append("CRISIS")

    article["flags"] = flags
    return article
