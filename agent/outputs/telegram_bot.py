"""
outputs/telegram_bot.py — Send digest and breaking alerts to Telegram.
Uses python-telegram-bot (sync wrapper for simplicity in cron context).
"""

import logging
import asyncio
from datetime import datetime

from .. import config

log = logging.getLogger("daybrief.telegram")

MAX_MSG_LEN = 4096  # Telegram limit


def _flag_icon(flags):
    if "BREAKING" in flags:
        return "🔴"
    if "PRIORITY" in flags:
        return "🟡"
    if "CRISIS" in flags:
        return "⚠️"
    return "🟢"


def _format_article(article: dict) -> str:
    icon = _flag_icon(article.get("flags", []))
    title = article.get("title", "No title")
    source = article.get("source", "?")
    score = article.get("score", 0)
    summary = article.get("summary", "")[:200]
    url = article.get("url", "")
    sentiment = article.get("sentiment_label", "neutral")
    sentiment_icon = {"positive": "😊", "negative": "😟", "neutral": "😐"}.get(sentiment, "😐")

    return (
        f"{icon} *{title}*\n"
        f"_{source}_ · Score: {score:.2f} · {sentiment_icon}\n"
        f"{summary}\n"
        f"[Read more]({url})"
    )


def _format_digest(articles, clusters, insights, trends, today) -> str:
    feed_cfg = config.feeds()["categories"]

    lines = [f"📰 *Daily News Digest — {today}*\n"]

    # Insights first
    if insights:
        lines.append("*Key Insights*")
        lines.extend(f"• {i}" for i in insights[:4])
        lines.append("")

    # Top clusters
    top_clusters = sorted(clusters, key=lambda c: c.get("score", 0), reverse=True)[:3]
    if top_clusters:
        lines.append("*Top Stories (multi-source)*")
        for c in top_clusters:
            icon = _flag_icon(c.get("flags", []))
            kw = c.get("top_keywords", ["?"])[0] if c.get("top_keywords") else "?"
            n = c.get("source_count", 1)
            summary = c.get("unified_summary", "")[:150]
            lines.append(f"{icon} *{kw}* ({n} sources)\n_{summary}_\n")

    # Per-category top items
    for cat_key, cat_meta in feed_cfg.items():
        cat_articles = [a for a in articles if a["category"] == cat_key]
        cat_articles = sorted(cat_articles, key=lambda a: a.get("score", 0), reverse=True)
        top = cat_articles[:2]
        if not top:
            continue
        label = cat_meta["label"]
        lines.append(f"*{label}*")
        for a in top:
            icon = _flag_icon(a.get("flags", []))
            title = a.get("title", "?")[:80]
            source = a.get("source", "?")
            url = a.get("url", "")
            lines.append(f"{icon} [{title}]({url}) _{source}_")
        lines.append("")

    # Trends
    if trends:
        lines.append("*Trending Keywords*")
        for t in trends[:4]:
            lines.append(f"📈 `{t['keyword']}` — {t['spike_factor']}× above average")

    msg = "\n".join(lines)
    return msg[:MAX_MSG_LEN]


async def _send_async(text: str, bot_token: str, chat_id: str, parse_mode: str = "Markdown"):
    from telegram import Bot
    from telegram.constants import ParseMode
    bot = Bot(token=bot_token)
    pm = ParseMode.MARKDOWN_V2 if parse_mode == "MarkdownV2" else ParseMode.MARKDOWN
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=pm,
        disable_web_page_preview=False,
    )


def _send(text: str):
    cfg = config.settings()["telegram"]
    token = cfg["bot_token"]
    chat_id = str(cfg["chat_id"])
    asyncio.run(_send_async(text, token, chat_id))


def send_digest(articles, clusters, insights, trends):
    cfg = config.settings()["telegram"]
    if not cfg.get("enabled", False):
        log.info("Telegram output disabled — skipping")
        return

    today = datetime.utcnow().date().isoformat()
    try:
        text = _format_digest(articles, clusters, insights, trends, today)
        _send(text)
        log.info("Telegram digest sent")
    except Exception as e:
        log.error(f"Telegram digest failed: {e}")


def send_breaking_alert(article: dict):
    """Send an immediate Telegram message for a breaking/high-score article."""
    cfg = config.settings()["telegram"]
    if not cfg.get("enabled", False):
        return
    if not cfg.get("send_breaking_immediately", True):
        return

    try:
        text = f"🚨 *BREAKING ALERT*\n\n{_format_article(article)}"
        _send(text[:MAX_MSG_LEN])
        log.info(f"Breaking alert sent: {article.get('title', '')[:60]}")
    except Exception as e:
        log.error(f"Telegram alert failed: {e}")
