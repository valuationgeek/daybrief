"""
outputs/email_digest.py — Generate and send the HTML email digest.

Fix #2:  Summary: / Impact: labels added
Fix #9:  total_articles = only articles actually shown (not total fetched)
Fix #10: send() is guarded by a run-level flag so it fires exactly once
"""

import smtplib
import logging
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader

from .. import config

log = logging.getLogger("daybrief.email")


def _local_now():
    tz_str = config.settings()["agent"].get("timezone", "UTC")
    try:
        tz = ZoneInfo(tz_str)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz)


# Guard: set to True after first successful send this process run
_sent_this_run = False


def _flag_emoji(flags):
    if "BREAKING" in flags:
        return "🔴"
    if "PRIORITY" in flags:
        return "🟡"
    if "CRISIS" in flags:
        return "⚠️"
    return "🟢"


def _sentiment_class(label):
    return {"positive": "pos", "negative": "neg", "neutral": "neu"}.get(label or "neutral", "neu")


def _build_html(articles, clusters, insights, trends, today, cfg_display):
    feed_cfg     = config.feeds()["categories"]
    categories   = []
    shown_ids    = set()

    max_clusters = cfg_display.get("max_clusters_per_category", 3)

    # Per-category solo limits — reads from sources.yaml scoring.max_solo_per_category
    solo_limits  = config.sources().get("scoring", {}).get("max_solo_per_category", {})
    def _get_solo_limit(cat_key):
        if isinstance(solo_limits, dict):
            return int(solo_limits.get(cat_key, solo_limits.get("default", 10)))
        return int(solo_limits)   # legacy single-value fallback

    for cat_key, cat_meta in feed_cfg.items():
        cat_articles  = [a for a in articles if a["category"] == cat_key]
        cat_clusters  = sorted(
            [c for c in clusters if c.get("category") == cat_key],
            key=lambda c: c.get("score", 0), reverse=True,
        )[:max_clusters]

        clustered_ids = {aid for c in cat_clusters for aid in c.get("article_ids", [])}
        solo = sorted(
            [a for a in cat_articles if a["id"] not in clustered_ids],
            key=lambda a: a.get("score", 0), reverse=True,
        )[:_get_solo_limit(cat_key)]

        # Track what's actually shown for the header count
        for c in cat_clusters:
            shown_ids.update(c.get("article_ids", []))
        for a in solo:
            shown_ids.add(a["id"])

        categories.append({
            "key":           cat_key,
            "label":         cat_meta["label"],
            "clusters":      cat_clusters,
            "solo_articles": solo,
        })

    env = Environment(
        loader=FileSystemLoader(str(Path(__file__).parent.parent.parent / "templates")),
        autoescape=True,
    )
    env.globals["flag_emoji"]      = _flag_emoji
    env.globals["sentiment_class"] = _sentiment_class
    env.globals["zip"]             = zip

    template = env.get_template("email.html.j2")
    return template.render(
        today          = today,
        generated_at   = _local_now().strftime("%Y-%m-%d %I:%M %p %Z"),
        categories     = categories,
        insights       = insights,
        trends         = trends,
        total_articles = len(shown_ids),      # Fix #9 — only count shown articles
        breaking_count = sum(1 for a in articles if "BREAKING" in a.get("flags", [])),
    )


def _send_smtp(html_body, subject):
    cfg       = config.settings()
    smtp      = cfg["email"]["smtp"]
    from_addr = cfg["email"]["from_address"]
    to_addrs  = cfg["email"]["to_addresses"]

    msg             = MIMEMultipart("alternative")
    msg["Subject"]  = subject
    msg["From"]     = from_addr
    msg["To"]       = ", ".join(to_addrs)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(smtp["host"], smtp["port"]) as server:
        server.ehlo()
        server.starttls()
        server.login(smtp["username"], smtp["password"])
        server.sendmail(from_addr, to_addrs, msg.as_string())

    log.info(f"Email sent to: {to_addrs}")


def _send_sendgrid(html_body, subject):
    import sendgrid
    from sendgrid.helpers.mail import Mail
    cfg      = config.settings()["email"]
    sg       = sendgrid.SendGridAPIClient(api_key=cfg["sendgrid"]["api_key"])
    mail     = Mail(
        from_email   = cfg["from_address"],
        to_emails    = cfg["to_addresses"],
        subject      = subject,
        html_content = html_body,
    )
    resp = sg.send(mail)
    log.info(f"SendGrid response: {resp.status_code}")


def send(articles, clusters, insights, trends):
    global _sent_this_run
    cfg = config.settings()["email"]

    if not cfg.get("enabled", False):
        log.info("Email output disabled — skipping")
        return

    # Fix #10 — never send twice in the same pipeline run
    if _sent_this_run:
        log.warning("Email already sent this run — skipping duplicate call")
        return

    min_items = config.watchlist().get("alerts", {}).get("min_items_for_email", 5)
    if len(articles) < min_items:
        log.info(f"Only {len(articles)} articles — below minimum {min_items}, skipping email")
        return

    today   = _local_now().date().isoformat()
    subject = cfg.get("subject_template", "📰 News Digest — {date}").format(date=today)

    # Display limits (tunable in settings.yaml under email.display)
    cfg_display = cfg.get("display", {
        "max_clusters_per_category": 3,
        "max_articles_per_category": 5,
    })

    try:
        html_body = _build_html(articles, clusters, insights, trends, today, cfg_display)
        provider  = cfg.get("provider", "smtp")
        if provider == "sendgrid":
            _send_sendgrid(html_body, subject)
        else:
            _send_smtp(html_body, subject)
        _sent_this_run = True
    except Exception as e:
        log.error(f"Email send failed: {e}")
        raise
