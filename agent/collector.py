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

from . import config, db

log = logging.getLogger("daybrief.collector")


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


def _fetch_body(url, timeout=15):
    try:
        html = trafilatura.fetch_url(url)
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
                parsed = feedparser.parse(
                    url,
                    request_headers={"User-Agent": ua},
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
    timeout     = config.feeds().get("limits", {}).get("fetch_timeout_seconds", 15)
    articles    = []

    # Endpoint: Most Emailed → "Top Stories" category
    # Enabled/disabled via feeds.yaml: apis.nytimes.endpoints.most_emailed
    if endpoints.get("most_emailed", False):
        try:
            url  = f"https://api.nytimes.com/svc/mostpopular/v2/emailed/1.json?api-key={key}"
            resp = httpx.get(url, timeout=timeout)
            resp.raise_for_status()
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
            resp = httpx.get(url, timeout=timeout)
            resp.raise_for_status()
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
    timeout     = config.feeds().get("limits", {}).get("fetch_timeout_seconds", 15)
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
        resp = httpx.get("https://newsdata.io/api/1/market", params=params, timeout=timeout)
        resp.raise_for_status()
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

    # Enrich articles with short/missing body via full-page fetch
    needs_body = [a for a in all_articles if len(a.get("body", "")) < 100]
    if needs_body:
        log.info(f"Fetching full text for {len(needs_body)} short-body articles...")
        timeout = config.feeds().get("limits", {}).get("fetch_timeout_seconds", 15)
        for a in needs_body:
            body = _fetch_body(a["url"], timeout)
            if body:
                a["body"] = body

    return all_articles
