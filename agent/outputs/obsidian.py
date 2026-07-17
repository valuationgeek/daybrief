"""
outputs/obsidian.py — Write daily digest as Obsidian-compatible Markdown.

Filename format:  YYYY-MM-DD HH-MM {TZ} (run N).md
  e.g.  2026-05-12 07-30 HKT.md
  If re-run same day:  2026-05-12 07-30 HKT (run 2).md

Index:  appends each run as a new line — never overwrites old entries.
"""

import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader

from .. import config

log = logging.getLogger("daybrief.obsidian")
TEMPLATE_NAME = "daily_note.md.j2"


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _local_now():
    tz_str = config.settings()["agent"].get("timezone", "UTC")
    try:
        tz = ZoneInfo(tz_str)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz)


def _format_title_datetime(dt):
    """Human-readable datetime for the note heading."""
    tz_abbr = dt.strftime("%Z") or "Local"
    return dt.strftime(f"%Y-%m-%d %I:%M %p {tz_abbr}")


def _make_filename(dt, output_dir):
    """
    Build a unique filename for this run.
    Base:  2026-05-12 07-30 HKT.md
    If that already exists (re-run same minute): add (run 2), (run 3) …
    """
    tz_abbr  = dt.strftime("%Z") or "Local"
    base     = dt.strftime(f"%Y-%m-%d %H-%M {tz_abbr}")
    candidate = output_dir / f"{base}.md"
    if not candidate.exists():
        return candidate
    # File exists — find next available run number
    n = 2
    while True:
        candidate = output_dir / f"{base} (run {n}).md"
        if not candidate.exists():
            return candidate
        n += 1


def _flag_emoji(flags):
    if "BREAKING" in flags:
        return "🔴"
    if "PRIORITY" in flags:
        return "🟡"
    if "CRISIS" in flags:
        return "⚠️"
    return "🟢"


def _sentiment_emoji(label):
    return {"positive": "😊", "negative": "😟", "neutral": "😐"}.get(label or "neutral", "😐")


# ─────────────────────────────────────────────────────────────
# Main write function
# ─────────────────────────────────────────────────────────────

def write(articles, clusters, insights, trends, run_date=None):
    cfg = config.settings()["obsidian"]
    if not cfg.get("enabled", True):
        log.info("Obsidian output disabled — skipping")
        return

    now        = _local_now()
    today      = run_date or now.date().isoformat()
    title_dt   = _format_title_datetime(now)

    vault_raw  = cfg.get("vault_path", "output/obsidian")
    vault_path = Path(vault_raw.replace("\\", "/")).expanduser().resolve()
    subfolder  = cfg.get("subfolder", "Daily")
    output_dir = vault_path / subfolder
    output_dir.mkdir(parents=True, exist_ok=True)

    # Unique filename — never overwrites an existing file
    if run_date and run_date.startswith("TEST-"):
        # During testing: use a fixed test filename (safe to overwrite)
        output_path = output_dir / f"{run_date}.md"
    else:
        output_path = _make_filename(now, output_dir)

    # Build per-category data for the template
    feed_cfg      = config.feeds()["categories"]
    solo_limits   = config.sources().get("scoring", {}).get("max_solo_per_category", {})
    # solo_limits may be a dict (per-category) or a plain int (legacy fallback)
    def _get_solo_limit(cat_key):
        if isinstance(solo_limits, dict):
            return int(solo_limits.get(cat_key, solo_limits.get("default", 10)))
        return int(solo_limits)   # legacy single-value fallback

    categories = []
    for cat_key, cat_meta in feed_cfg.items():
        cat_articles  = [a for a in articles if a["category"] == cat_key]
        cat_clusters  = [c for c in clusters if c.get("category") == cat_key]
        clustered_ids = {aid for c in cat_clusters for aid in c.get("article_ids", [])}
        solo_articles = [a for a in cat_articles if a["id"] not in clustered_ids]
        limit         = _get_solo_limit(cat_key)

        categories.append({
            "key":           cat_key,
            "label":         cat_meta["label"],
            "clusters":      sorted(cat_clusters,  key=lambda c: c.get("score", 0), reverse=True),
            "solo_articles": sorted(solo_articles, key=lambda a: a.get("score", 0), reverse=True)[:limit],
            "total":         len(cat_articles),
        })

    env = Environment(
        loader=FileSystemLoader(str(Path(__file__).parent.parent.parent / "templates")),
        autoescape=False,
    )
    env.globals["flag_emoji"]      = _flag_emoji
    env.globals["sentiment_emoji"] = _sentiment_emoji
    env.globals["zip"]             = zip

    template = env.get_template(TEMPLATE_NAME)
    content  = template.render(
        today          = today,
        title_dt       = title_dt,
        created        = now.isoformat(),
        filename       = output_path.stem,
        index_filename = cfg.get("index_filename", "index"),
        categories     = categories,
        insights       = insights,
        trends         = trends,
        total_articles = len(articles),
        total_clusters = len(clusters),
        breaking_count = sum(1 for a in articles if "BREAKING" in a.get("flags", [])),
    )

    output_path.write_text(content, encoding="utf-8")
    log.info(f"Obsidian note written → {output_path}")

    if cfg.get("write_index", True):
        index_name = cfg.get("index_filename", "index")
        _update_index(output_dir / f"{index_name}.md", output_path.stem, today, len(articles), title_dt)


# ─────────────────────────────────────────────────────────────
# Index — always appends, never overwrites old entries
# ─────────────────────────────────────────────────────────────

def _update_index(index_path, filename_stem, today, count, title_dt):
    """
    Append a new line for every run.
    Format:  - [[2026-05-12 07-30 HKT]] — 2026-05-12 · 42 articles
    Old entries are always preserved.
    """
    entry = f"- [[{filename_stem}]] — {title_dt} · {count} articles\n"

    if index_path.exists():
        existing = index_path.read_text(encoding="utf-8")
        # Only skip if this exact filename is already recorded
        if filename_stem in existing:
            return
        index_path.write_text(existing.rstrip() + "\n" + entry, encoding="utf-8")
    else:
        header = "# News Digest Index\n\nEach line is one run. Most recent at the bottom.\n\n"
        index_path.write_text(header + entry, encoding="utf-8")
