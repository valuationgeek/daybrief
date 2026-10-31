"""
collector.py  —  Stage 1: Fetch all news articles.

Three sections:
  A) BBC RSS + TechCrunch  (no key needed)
  B) New York Times API    (most emailed + world top stories)
  C) NewsData.io Market API (last 24h, top 10% domains)
"""

import hashlib
import logging
from datetime import datetime, timezone, timedelta

import feedparser
import httpx
import trafilatura
from trafilatura.settings import use_config

from . import config, db
from .net import with_retry, resolve_timeout

log = logging.getLogger("daybrief.collector")

_traf_cfg_cache = {}


def _trafilatura_config(timeout):
    """trafilatura.fetch_url takes its timeout from a config object, not a
    keyword argument — without this it silently uses the 30s package default
    and the caller's timeout is ignored."""
    if timeout not in _traf_cfg_cache:
        cfg = use_config()
        cfg.set("DEFAULT", "DOWNLOAD_TIMEOUT", str(int(timeout)))
        _traf_cfg_cache[timeout] = cfg
    return _traf_cfg_cache[timeout]


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _make_id(url):
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _is_recent(date_str, max_age_hours):
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        return dt >= cutoff
    except Exception:
        return True


def _rss_date(entry):
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc).isoformat()
            except Exception:
                pass
    return datetime.utcnow().isoformat()


def _fetch_body(url, timeout=None):
    """Fetch and extract an article's full text. Returns "" on failure —
    an enrichment miss degrades the summary but must never abort the run."""
    timeout = resolve_timeout(timeout)
    try:
        html = with_retry(
            lambda: trafilatura.fetch_url(url, config=_trafilatura_config(timeout)),
            what=f"body fetch {url}",
        )
        if html:
            text = trafilatura.extract(html, include_comments=False, include_tables=False)
            return text or ""
    except Exception as e:
        log.debug(f"Body fetch failed for {url}: {e}")
    return ""


def _blank_article(title, url, source, category, published, body="", credibility=0.80):
    """Standard article dict. All downstream stages expect exactly this shape."""
    return {
        "id":              _make_id(url),
        "title":           title.strip(),
        "url":             url,
        "source":          source,
        "category":        category,
        "published":       published,
        "fetched_at":      datetime.utcnow().isoformat(),
        "body":            body,
        "credibility":     credibility,
        "summary":         None,
        "keywords":        [],
        "sentiment":       None,
        "sentiment_label": None,
        "score":           None,
        "flags":           [],
        "cluster_id":      None,
        "implication":     None,   # filled by analyzer
    }


# ─────────────────────────────────────────────────────────────
# A) BBC RSS Feeds + TechCrunch
# ─────────────────────────────────────────────────────────────

def _fetch_all_rss():
    """
    Fetch all RSS feeds defined in feeds.yaml.
    Uses a browser-like User-Agent by default so sites like CGTN don't block.
    Individual feeds can override with headers: in feeds.yaml if needed.
    """
    cfg          = config.feeds()
    limits       = cfg.get("limits", {})
    rss_cats     = cfg.get("rss_feeds", {})
    max_per_feed = limits.get("max_articles_per_feed", 10)
    max_age      = limits.get("max_age_hours", 24)
    feed_timeout = resolve_timeout()
    articles     = []

    # Browser-like User-Agent — avoids blocks from sites like CGTN
    default_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    for cat_key, cat_cfg in rss_cats.items():
        for feed in cat_cfg.get("feeds", []):
            url         = feed["url"]
            source      = feed["source"]
            credibility = feed.get("credibility", 0.80)
            language    = feed.get("language", "en")
            ua          = feed.get("user_agent", default_ua)
            ignore_age  = feed.get("ignore_age", False)

            try:
                # Fetch with httpx rather than letting feedparser download the
                # URL itself: feedparser.parse(url) has no timeout parameter and
                # falls back to urllib's default of "block forever", which can
                # wedge the whole run if the network drops mid-fetch. Passing
                # the Content-Type header along keeps feedparser's charset
                # detection for feeds that declare their encoding only there.
                resp = with_retry(
                    lambda: httpx.get(
                        url,
                        headers={"User-Agent": ua},
                        timeout=feed_timeout,
                        follow_redirects=True,
                    ).raise_for_status(),
                    what=f"[RSS] {source}",
                )
                parsed = feedparser.parse(
                    resp.content,
                    response_headers={"content-type": resp.headers.get("content-type", "")},
                )

                # feedparser returns bozo=True on parse errors but may still
                # have entries (e.g. slightly malformed XML from some Chinese feeds)
                if parsed.bozo and not parsed.entries:
                    log.warning(f"[RSS] {source}: feed error — {parsed.bozo_exception}")
                    continue

                count = 0
                for entry in parsed.entries[:max_per_feed]:
                    link  = getattr(entry, "link",  None)
                    title = getattr(entry, "title", "").strip()
                    if not link or not title:
                        continue
                    pub = _rss_date(entry)
                    if not ignore_age and not _is_recent(pub, max_age):
                        continue
                    if db.article_exists(link):
                        continue
                    raw_summary = getattr(entry, "summary", "") or ""
                    body    = trafilatura.extract(raw_summary) or raw_summary[:500]
                    article = _blank_article(title, link, source, cat_key, pub, body, credibility)
                    article["language"] = language
                    articles.append(article)
                    count += 1

                log.info(f"[RSS] {source}: {count} new articles")

            except Exception as e:
                log.warning(f"[RSS] {source} failed: {e}")

    return articles


# ─────────────────────────────────────────────────────────────
# B) New York Times API
# ─────────────────────────────────────────────────────────────

def _fetch_nytimes():
    cfg = config.feeds().get("apis", {}).get("nytimes", {})
    if not cfg.get("enabled", False):
        log.info("[NYT] Disabled — skipping")
        return []
    key = cfg.get("key", "")
    if not key or key.startswith("YOUR_"):
        log.warning("[NYT] No API key set — skipping")
        return []

    # Which endpoints to use — controlled by feeds.yaml → apis.nytimes.endpoints
    endpoints   = cfg.get("endpoints", {"world": True, "most_emailed": False})
    # Category keys these articles land in — override in feeds.yaml if your
    # category names differ (must match a key under rss_feeds:).
    world_cat   = cfg.get("world_category", "world")
    emailed_cat = cfg.get("most_emailed_category", "breaking")
    max_results = cfg.get("max_results", 10)
    timeout     = resolve_timeout()
    articles    = []

    # Endpoint: Most Emailed → "Top Stories" category
    # Enabled/disabled via feeds.yaml: apis.nytimes.endpoints.most_emailed
    if endpoints.get("most_emailed", False):
        try:
            url  = f"https://api.nytimes.com/svc/mostpopular/v2/emailed/1.json?api-key={key}"
            resp = with_retry(
                lambda: httpx.get(url, timeout=timeout).raise_for_status(),
                what="[NYT] Most Emailed",
            )
            count = 0
            for item in resp.json().get("results", [])[:max_results]:
                link  = item.get("url", "")
                title = item.get("title", "").strip()
                if not link or not title or db.article_exists(link):
                    continue
                pub  = item.get("published_date", datetime.utcnow().date().isoformat())
                body = item.get("abstract", "") or ""
                articles.append(_blank_article(title, link, "NYT Most Emailed", emailed_cat, pub, body, 0.95))
                count += 1
            log.info(f"[NYT] Most Emailed: {count} new articles")
        except Exception as e:
            log.warning(f"[NYT] Most Emailed failed: {e}")
    else:
        log.info("[NYT] Most Emailed: disabled in feeds.yaml")

    # Endpoint: World Top Stories → "World Headlines" category
    # Enabled/disabled via feeds.yaml: apis.nytimes.endpoints.world
    if endpoints.get("world", True):
        try:
            url  = f"https://api.nytimes.com/svc/topstories/v2/world.json?api-key={key}"
            resp = with_retry(
                lambda: httpx.get(url, timeout=timeout).raise_for_status(),
                what="[NYT] World Top Stories",
            )
            count = 0
            for item in resp.json().get("results", [])[:max_results]:
                link  = item.get("url", "")
                title = item.get("title", "").strip()
                if not link or not title or db.article_exists(link):
                    continue
                pub  = (item.get("published_date") or datetime.utcnow().isoformat())[:10]
                body = item.get("abstract", "") or ""
                articles.append(_blank_article(title, link, "NYT World", world_cat, pub, body, 0.95))
                count += 1
            log.info(f"[NYT] World Top Stories: {count} new articles")
        except Exception as e:
            log.warning(f"[NYT] World Top Stories failed: {e}")
    else:
        log.info("[NYT] World Top Stories: disabled in feeds.yaml")

    return articles


# ─────────────────────────────────────────────────────────────
# C) NewsData.io Market API
#    - endpoint:       /api/1/market   (dedicated market/finance endpoint)
#    - timeframe=24    last 24 hours only
#    - prioritydomain=top   top 10% of news domains by authority
#    - language=en
# ─────────────────────────────────────────────────────────────

def _fetch_newsdata():
    cfg = config.feeds().get("apis", {}).get("newsdata", {})
    if not cfg.get("enabled", False):
        log.info("[NewsData] Disabled — skipping")
        return []
    key = cfg.get("key", "")
    if not key or key.startswith("YOUR_"):
        log.warning("[NewsData] No API key set — skipping")
        return []

    max_results = cfg.get("max_results", 10)
    # Category key these articles land in — override in feeds.yaml if needed.
    category    = cfg.get("category", "business")
    timeout     = resolve_timeout()
    articles    = []

    try:
        params = {
            "apikey":         key,
            "language":       "en",
            "datatype":       "news",
            "removeduplicate": "1",          
            "prioritydomain": "top",         # top 10% of news domains
            "size":           max_results,
        }
        resp = with_retry(
            lambda: httpx.get(
                "https://newsdata.io/api/1/market", params=params, timeout=timeout
            ).raise_for_status(),
            what="[NewsData] Market",
        )
        data = resp.json()

        if data.get("status") != "success":
            log.warning(f"[NewsData] API error: {data.get('message', 'unknown')}")
            return []

        count = 0
        for item in data.get("results", []):
            link  = item.get("link", "")
            title = (item.get("title") or "").strip()
            if not link or not title or db.article_exists(link):
                continue
            pub  = item.get("pubDate", datetime.utcnow().isoformat())
            # Use full content if available, fall back to description
            body = item.get("full_description") or item.get("description") or item.get("content") or ""
            src  = item.get("source_id") or item.get("source_name") or "NewsData"
            articles.append(_blank_article(title, link, f"NewsData/{src}", category, pub, body, 0.82))
            count += 1

        log.info(f"[NewsData] Market (24h, top domains): {count} new articles")

    except Exception as e:
        log.warning(f"[NewsData] Request failed: {e}")

    return articles


# ─────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────

def collect_all():
    """Fetch from all sources and return a combined article list."""
    all_articles = []
    all_articles += _fetch_all_rss()    # all RSS feeds (BBC, CGTN, Yahoo, Sina, etc.)
    all_articles += _fetch_nytimes()
    all_articles += _fetch_newsdata()

    log.info(f"Total collected: {len(all_articles)} new articles")

    # Enrich articles with short/missing body via full-page fetch.
    # The default threshold is ~9000 chars (~1500 words), not 100: RSS
    # <summary> tags are often just a lead paragraph, and when
    # trafilatura.extract() can't clean one up, _fetch_all_rss falls back to
    # raw_summary[:500] — a hard, mid-sentence truncation that a low threshold
    # would miss (495 chars still looks "long enough"). Setting the bar this
    # high means most RSS-only bodies get re-fetched from the source page,
    # trading longer collection time for fuller, untruncated article text.
    limits     = config.feeds().get("limits", {}) or {}
    threshold  = limits.get("enrich_below_chars", 9000)
    needs_body = [a for a in all_articles if len(a.get("body") or "") < threshold]
    if needs_body:
        log.info(f"Fetching full text for {len(needs_body)} articles with bodies under {threshold} chars...")
        timeout = resolve_timeout()
        # Circuit breaker: each _fetch_body can burn ~14s of backoff before
        # giving up, so a sustained outage across this many articles would
        # stall the run for the better part of an hour. A long run of
        # consecutive failures means the network is down, not that these
        # particular URLs are bad — stop enriching and finish on RSS bodies.
        max_consecutive      = limits.get("enrich_failure_threshold", 8)
        consecutive_failures = 0
        for i, a in enumerate(needs_body):
            body = _fetch_body(a["url"], timeout)
            # Only replace with something longer — a paywall stub or cookie
            # banner must not overwrite a decent RSS body.
            if body and len(body) > len(a.get("body") or ""):
                a["body"] = body
            if body:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if consecutive_failures >= max_consecutive:
                    log.warning(
                        f"Body enrichment aborted after {max_consecutive} consecutive "
                        f"failures ({i + 1}/{len(needs_body)} processed) — "
                        "network likely down; continuing with RSS bodies"
                    )
                    break

    return all_articles
