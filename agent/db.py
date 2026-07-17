"""
db.py — SQLite database layer
Stores articles, keyword trends, cluster history, and run logs.
"""

import sqlite3
import json
import hashlib
from datetime import datetime, timedelta
from pathlib import Path


DB_PATH = Path(__file__).parent.parent / "db" / "news.sqlite"


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create all tables if they don't exist."""
    conn = get_connection()
    cur = conn.cursor()

    cur.executescript("""
        CREATE TABLE IF NOT EXISTS articles (
            id          TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            url         TEXT UNIQUE NOT NULL,
            source      TEXT,
            category    TEXT,
            published   TEXT,
            fetched_at  TEXT,
            body        TEXT,
            summary     TEXT,
            keywords    TEXT,         -- JSON list
            sentiment   REAL,
            sentiment_label TEXT,
            impact      TEXT,
            score       REAL,
            flags       TEXT,         -- JSON list: BREAKING, PRIORITY, TRENDING
            cluster_id  TEXT,
            credibility REAL
        );

        CREATE TABLE IF NOT EXISTS clusters (
            cluster_id      TEXT PRIMARY KEY,
            run_date        TEXT,
            category        TEXT,
            categories      TEXT,     -- JSON list of all categories covered
            article_ids     TEXT,     -- JSON list
            sources         TEXT,     -- JSON list
            titles          TEXT,     -- JSON list
            urls            TEXT,     -- JSON list (Fix #6 — source links)
            unified_summary TEXT,
            top_keywords    TEXT,     -- JSON list
            score           REAL,
            flags           TEXT      -- JSON list
        );

        CREATE TABLE IF NOT EXISTS keyword_freq (
            date        TEXT,
            keyword     TEXT,
            category    TEXT,
            count       INTEGER,
            PRIMARY KEY (date, keyword, category)
        );

        CREATE TABLE IF NOT EXISTS run_log (
            run_id      TEXT PRIMARY KEY,
            started_at  TEXT,
            finished_at TEXT,
            status      TEXT,
            articles_fetched  INTEGER,
            articles_new      INTEGER,
            clusters_formed   INTEGER,
            errors      TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_articles_category ON articles(category);
        CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published);
        CREATE INDEX IF NOT EXISTS idx_articles_score ON articles(score DESC);
        CREATE INDEX IF NOT EXISTS idx_kfreq_date ON keyword_freq(date);
    """)

    conn.commit()
    conn.close()


def article_url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def article_exists(url: str) -> bool:
    conn = get_connection()
    row = conn.execute(
        "SELECT 1 FROM articles WHERE url = ?", (url,)
    ).fetchone()
    conn.close()
    return row is not None


def save_article(article: dict):
    conn = get_connection()
    conn.execute("""
        INSERT OR REPLACE INTO articles
        (id, title, url, source, category, published, fetched_at,
         body, summary, impact, keywords, sentiment, sentiment_label,
         score, flags, cluster_id, credibility)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        article.get("id"),
        article.get("title"),
        article.get("url"),
        article.get("source"),
        article.get("category"),
        article.get("published"),
        article.get("fetched_at", datetime.utcnow().isoformat()),
        article.get("body"),
        article.get("summary"),
        article.get("impact", ""),
        json.dumps(article.get("keywords", [])),
        article.get("sentiment"),
        article.get("sentiment_label"),
        article.get("score"),
        json.dumps(article.get("flags", [])),
        article.get("cluster_id"),
        article.get("credibility"),
    ))
    conn.commit()
    conn.close()


def save_cluster(cluster: dict):
    conn = get_connection()
    conn.execute("""
        INSERT OR REPLACE INTO clusters
        (cluster_id, run_date, category, categories, article_ids,
         sources, titles, urls, unified_summary, top_keywords, score, flags)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        cluster["cluster_id"],
        cluster.get("run_date", datetime.utcnow().date().isoformat()),
        cluster.get("category"),
        json.dumps(cluster.get("categories", [])),
        json.dumps(cluster.get("article_ids", [])),
        json.dumps(cluster.get("sources", [])),
        json.dumps(cluster.get("titles", [])),
        json.dumps(cluster.get("urls", [])),
        cluster.get("unified_summary"),
        json.dumps(cluster.get("top_keywords", [])),
        cluster.get("score"),
        json.dumps(cluster.get("flags", [])),
    ))
    conn.commit()
    conn.close()


def record_keyword_frequencies(date_str: str, category: str, keywords: list[str]):
    conn = get_connection()
    from collections import Counter
    counts = Counter(keywords)
    for kw, cnt in counts.items():
        conn.execute("""
            INSERT INTO keyword_freq (date, keyword, category, count)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(date, keyword, category) DO UPDATE SET count = count + ?
        """, (date_str, kw, category, cnt, cnt))
    conn.commit()
    conn.close()


def get_keyword_history(keyword: str, lookback_days: int = 7) -> list[dict]:
    conn = get_connection()
    since = (datetime.utcnow() - timedelta(days=lookback_days)).date().isoformat()
    rows = conn.execute(
        "SELECT date, SUM(count) as count FROM keyword_freq "
        "WHERE keyword = ? AND date >= ? GROUP BY date ORDER BY date",
        (keyword, since)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_today_articles(category: str = None) -> list[dict]:
    conn = get_connection()
    today = datetime.utcnow().date().isoformat()
    if category:
        rows = conn.execute(
            "SELECT * FROM articles WHERE fetched_at LIKE ? AND category = ? ORDER BY score DESC",
            (f"{today}%", category)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM articles WHERE fetched_at LIKE ? ORDER BY score DESC",
            (f"{today}%",)
        ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["keywords"] = json.loads(d["keywords"] or "[]")
        d["flags"] = json.loads(d["flags"] or "[]")
        result.append(d)
    return result


def get_today_clusters(category: str = None) -> list[dict]:
    conn = get_connection()
    today = datetime.utcnow().date().isoformat()
    if category:
        rows = conn.execute(
            "SELECT * FROM clusters WHERE run_date = ? AND category = ? ORDER BY score DESC",
            (today, category)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM clusters WHERE run_date = ? ORDER BY score DESC",
            (today,)
        ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["article_ids"]  = json.loads(d.get("article_ids")  or "[]")
        d["top_keywords"] = json.loads(d.get("top_keywords") or "[]")
        d["flags"]        = json.loads(d.get("flags")        or "[]")
        d["sources"]      = json.loads(d.get("sources")      or "[]")
        d["titles"]       = json.loads(d.get("titles")       or "[]")
        d["urls"]         = json.loads(d.get("urls")         or "[]")
        d["categories"]   = json.loads(d.get("categories")   or "[]")
        result.append(d)
    return result


def log_run(run: dict):
    conn = get_connection()
    conn.execute("""
        INSERT OR REPLACE INTO run_log
        (run_id, started_at, finished_at, status,
         articles_fetched, articles_new, clusters_formed, errors)
        VALUES (?,?,?,?,?,?,?,?)
    """, (
        run["run_id"],
        run.get("started_at"),
        run.get("finished_at"),
        run.get("status", "ok"),
        run.get("articles_fetched", 0),
        run.get("articles_new", 0),
        run.get("clusters_formed", 0),
        run.get("errors", ""),
    ))
    conn.commit()
    conn.close()


def purge_old_articles(days: int = 90):
    """Remove articles older than `days` to keep DB lean."""
    conn = get_connection()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    conn.execute("DELETE FROM articles WHERE fetched_at < ?", (cutoff,))
    conn.execute("DELETE FROM keyword_freq WHERE date < ?", (cutoff[:10],))
    conn.commit()
    conn.close()
