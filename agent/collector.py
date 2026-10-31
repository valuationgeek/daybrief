"""
collector.py  —  Stage 1: Fetch all news articles.

Three sections:
  A) BBC RSS + TechCrunch  (no key needed)
  B) New York Times API    (most emailed + world top stories)
  C) NewsData.io Market API (last 24h, top 10% domains)
"""

import hashlib
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import feedparser
import httpx
import trafilatura

from . import config, db
from .net import TRANSIENT_ERRORS, with_retry, resolve_timeout

log = logging.getLogger("daybrief.collector")

# Browser-like User-Agent — avoids blocks from sites like CGTN, and the
# "trafilatura/x.y" default many news sites reject. Used for feeds and pages.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Openings of interstitial pages (cookie walls, bot checks, paywall prompts).
# Specific phrasing, not bare "cookie"/"consent", so a story about a consent
# decree still gets through.
_BLOCK_PAGE_RE = re.compile(
    r"\bcookies?\b.{0,80}\b(?:accept|consent|settings|preferences|policy)\b"
    r"|\b(?:accept|reject|manage)\b.{0,40}\bcookies?\b"
    r"|enable javascript|are you a robot|verify you are (?:a )?human"
    r"|access denied|subscribe to (?:continue|keep) reading|family of brands"
    r"|privacy (?:settings|choices)",
    re.IGNORECASE,
)
_TITLE_WORD_RE = re.compile(r"[A-Za-z0-9]{4,}")


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
    """Fetch and extract an article's full text.

    Returns (text, network_ok). `text` is "" on any miss. `network_ok` is
    False only when the request never got an HTTP response, even after
    retries; that is the signal the enrichment circuit breaker counts. A 403
    paywall, a bot block or an unextractable page is a page-level miss, so
    network_ok stays True. One paywalled source must not switch enrichment
    off for every other site.

    Downloaded with httpx rather than trafilatura.fetch_url. fetch_url
    catches every exception and returns None, so a with_retry around it
    never fired, and an outage looked exactly like a paywall.
    """
    timeout = resolve_timeout(timeout)
    if not url or not url.lower().startswith(("http://", "https://")):
        return "", True
    try:
        resp = with_retry(
            lambda: httpx.get(
                url,
                headers={"User-Agent": _BROWSER_UA},
                timeout=timeout,
                follow_redirects=True,
            ),
            what=f"body fetch {url}",
        )
    except httpx.UnsupportedProtocol:
        return "", True
    except TRANSIENT_ERRORS as e:
        log.debug(f"Body fetch failed for {url}: {e}")
        return "", False
    except Exception as e:   # bad URL, redirect loop, … — a page-level miss
        log.debug(f"Body fetch failed for {url}: {e}")
        return "", True

    ctype = resp.headers.get("content-type", "").lower()
    if resp.status_code >= 400 or (ctype and "html" not in ctype):
        log.debug(f"Body fetch {url}: HTTP {resp.status_code} {ctype or '?'} — page-level miss")
        return "", True
    try:
        text = trafilatura.extract(
            resp.content, url=str(resp.url), include_comments=False, include_tables=False
        )
    except Exception as e:
        log.debug(f"Body extraction failed for {url}: {e}")
        text = None
    return text or "", True


def _usable_body(new, old, title):
    """True if a fetched page should replace the current body.

    Being longer is not enough. With a high enrichment threshold, most RSS
    bodies are re-fetched, and a cookie wall or consent page ("Yahoo is part
    of the Yahoo family of brands…") easily outgrows a good 480-char lead.
    The summary, keywords and sentiment would then describe the cookie
    notice. So the page must also mention the headline, and must not open
    like an interstitial. A rejected page leaves the RSS body as it was.
    """
    if not new or len(new) <= len(old or ""):
        return False
    if _BLOCK_PAGE_RE.search(new[:600]):
        return False
    words = {w.lower() for w in _TITLE_WORD_RE.findall(title or "")}
    if len(words) >= 2:
        text = new.lower()
        if sum(1 for w in words if w in text) < len(words) / 2:
            return False
    return True


def _enrich_bodies(articles):
    """Replace short or truncated bodies with the article page's full text.

    The default threshold is ~9000 chars (~1500 words), not 100: RSS
    <summary> tags are often just a lead paragraph, and when
    trafilatura.extract() can't clean one up, _fetch_all_rss falls back to
    raw_summary[:500] — a hard, mid-sentence truncation that a low threshold
    would miss (495 chars still looks "long enough"). Setting the bar this
    high means most RSS-only bodies get re-fetched, so the fetches have to be
    cheap:

    - non-English articles are skipped (their summary only ever uses ~300
      chars of body, and they bypass the LLM);
    - each URL is fetched once, even if several feeds carry it;
    - fetches run in a small thread pool (limits.enrich_workers);
    - once a host fails at the network level, its remaining URLs are
      skipped instead of each burning a full timeout plus backoff.

    Circuit breaker: consecutive *network* failures across distinct hosts
    (limits.enrich_failure_threshold) mean the network itself is down, so
    enrichment stops and the run finishes on RSS bodies. Any HTTP response,
    including a 403 paywall, resets the count.
    """
    limits    = config.feeds().get("limits", {}) or {}
    threshold = limits.get("enrich_below_chars", 9000)
    by_url    = {}
    for a in articles:
        if a.get("language", "en") != "en" or len(a.get("body") or "") >= threshold:
            continue
        by_url.setdefault(a["url"], []).append(a)
    if not by_url:
        return

    workers   = max(1, int(limits.get("enrich_workers", 4)))
    max_fails = max(1, int(limits.get("enrich_failure_threshold", 8)))
    timeout   = resolve_timeout()
    log.info(
        f"Fetching full text for {len(by_url)} articles with bodies under "
        f"{threshold} chars ({workers} workers)..."
    )

    lock       = threading.Lock()
    down_hosts = set()
    state      = {"streak": 0, "stopped": False}

    def work(url):
        host = urlparse(url).netloc.lower()
        with lock:
            if state["stopped"] or host in down_hosts:
                return url, None   # skipped — breaker open or host unreachable
        text, network_ok = _fetch_body(url, timeout)
        # Breaker state is updated here in the worker, not in the result loop
        # below: by the time a result is consumed, the worker may already be
        # running the next queued URLs, which must see the updated state.
        with lock:
            if network_ok:
                state["streak"] = 0
            elif host not in down_hosts:
                down_hosts.add(host)
                state["streak"] += 1
                if state["streak"] >= max_fails and not state["stopped"]:
                    state["stopped"] = True
                    log.warning(
                        f"Body enrichment stopped after {max_fails} unreachable hosts "
                        "in a row — network likely down; continuing with RSS bodies"
                    )
        return url, (text, network_ok)

    replaced = rejected = misses = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in as_completed([pool.submit(work, url) for url in by_url]):
            url, result = fut.result()
            if result is None or not result[1]:
                continue   # skipped, or a network failure (counted in work)
            text = result[0]
            if not text:
                misses += 1
                continue
            for a in by_url[url]:
                if _usable_body(text, a.get("body"), a.get("title")):
                    a["body"] = text
                    replaced += 1
                else:
                    rejected += 1
                    log.debug(f"Kept RSS body for {url}: fetched page is shorter, off-topic or an interstitial")

    log.info(
        f"Enrichment: {replaced} bodies replaced, {rejected} kept (fetched page shorter, "
        f"off-topic or an interstitial), {misses} page-level misses, "
        f"{len(down_hosts)} unreachable hosts"
    )


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


    for cat_key, cat_cfg in rss_cats.items():
        for feed in cat_cfg.get("feeds", []):
            url         = feed["url"]
            source      = feed["source"]
            credibility = feed.get("credibility", 0.80)
            language    = feed.get("language", "en")
            ua          = feed.get("user_agent", _BROWSER_UA)
            ignore_age  = feed.get("ignore_age", False)

            try:
                # Fetch with httpx rather than letting feedparser download the
                # URL itself: feedparser.parse(url) has no timeout parameter and
                # falls back to urllib's default of "block forever", which can
                # wedge the whole run if the network drops mid-fetch.
                resp = with_retry(
                    lambda: httpx.get(
                        url,
                        headers={"User-Agent": ua},
                        timeout=feed_timeout,
                        follow_redirects=True,
                    ).raise_for_status(),
                    what=f"[RSS] {source}",
                )
                # content-location lets feedparser resolve relative entry links.
                # The server's Content-Type is deliberately NOT forwarded: per
                # RFC 3023 its charset overrides the XML declaration, and servers
                # like RTHK send bare "text/xml" (= us-ascii) for UTF-8 feeds,
                # which marks them bozo. A fixed charset-less application/xml
                # defers to the XML's own encoding declaration; omitting the
                # header entirely would mark every feed bozo ("no Content-type").
                parsed = feedparser.parse(
                    resp.content,
                    response_headers={
                        "content-location": str(resp.url),
                        "content-type":     "application/xml",
                    },
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

    _enrich_bodies(all_articles)
    return all_articles
