"""
outputs/telegram_bot.py — Send digest and breaking alerts to Telegram.
Uses python-telegram-bot (sync wrapper for simplicity in cron context).

Messages use Telegram's HTML parse mode with every dynamic field escaped.
Legacy Markdown has no reliable escaping, so a headline containing "_", "*"
or "[" made Telegram reject the whole message ("can't parse entities").
"""

import html
import logging
import asyncio
from datetime import datetime

from .. import config

log = logging.getLogger("daybrief.telegram")

MAX_MSG_LEN = 4096  # Telegram limit


def _esc(value) -> str:
    """Escape any field for HTML parse mode; None becomes ""."""
    return html.escape(str(value or ""), quote=True)


def _flag_icon(flags):
    flags = flags or []
    if "BREAKING" in flags:
        return "🔴"
    if "PRIORITY" in flags:
        return "🟡"
    if "CRISIS" in flags:
        return "⚠️"
    return "🟢"


def _join_within_limit(lines, limit=MAX_MSG_LEN) -> str:
    """Join lines, dropping whole lines once the limit would be exceeded.

    Each line carries balanced tags, so stopping between lines never leaves
    an unclosed <b>/<i>/<a> — slicing the finished string could cut one in
    half and Telegram would reject the message.
    """
    out, total = [], 0
    for line in lines:
        extra = len(line) + (1 if out else 0)
        if total + extra > limit:
            if total + 2 <= limit:
                out.append("…")
            break
        out.append(line)
        total += extra
    return "\n".join(out)


def _format_article(article: dict) -> str:
    icon = _flag_icon(article.get("flags"))
    title = _esc((article.get("title") or "No title")[:300])
    source = _esc(article.get("source") or "?")
    score = article.get("score") or 0
    summary = _esc((article.get("summary") or "")[:200])
    url = _esc(article.get("url"))
    sentiment = article.get("sentiment_label") or "neutral"
    sentiment_icon = {"positive": "😊", "negative": "😟", "neutral": "😐"}.get(sentiment, "😐")

    return (
        f"{icon} <b>{title}</b>\n"
        f"<i>{source}</i> · Score: {score:.2f} · {sentiment_icon}\n"
        f"{summary}\n"
        f'<a href="{url}">Read more</a>'
    )


def _format_digest(articles, clusters, insights, trends, today) -> str:
    feed_cfg = config.feeds()["categories"]

    lines = [f"📰 <b>Daily News Digest — {_esc(today)}</b>\n"]

    # Insights first
    if insights:
        lines.append("<b>Key Insights</b>")
        lines.extend(f"• {_esc(i)}" for i in insights[:4])
        lines.append("")

    # Top clusters
    top_clusters = sorted(clusters, key=lambda c: c.get("score") or 0, reverse=True)[:3]
    if top_clusters:
        lines.append("<b>Top Stories (multi-source)</b>")
        for c in top_clusters:
            icon = _flag_icon(c.get("flags"))
            kw = _esc(c["top_keywords"][0] if c.get("top_keywords") else "?")
            n = c.get("source_count", 1)
            summary = _esc((c.get("unified_summary") or "")[:150])
            lines.append(f"{icon} <b>{kw}</b> ({n} sources)\n<i>{summary}</i>\n")

    # Per-category top items
    for cat_key, cat_meta in feed_cfg.items():
        cat_articles = [a for a in articles if a["category"] == cat_key]
        cat_articles = sorted(cat_articles, key=lambda a: a.get("score") or 0, reverse=True)
        top = cat_articles[:2]
        if not top:
            continue
        label = _esc(cat_meta["label"])
        lines.append(f"<b>{label}</b>")
        for a in top:
            icon = _flag_icon(a.get("flags"))
            title = _esc((a.get("title") or "?")[:80])
            source = _esc(a.get("source") or "?")
            url = _esc(a.get("url"))
            lines.append(f'{icon} <a href="{url}">{title}</a> <i>{source}</i>')
        lines.append("")

    # Trends
    if trends:
        lines.append("<b>Trending Keywords</b>")
        for t in trends[:4]:
            lines.append(f"📈 <code>{_esc(t['keyword'])}</code> — {t['spike_factor']}× above average")

    return _join_within_limit(lines)


async def _send_async(text: str, bot_token: str, chat_id: str):
    from telegram import Bot
    from telegram.constants import ParseMode
    bot = Bot(token=bot_token)
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=False,
    )


def _send(text: str):
    cfg = config.settings()["telegram"]
    token = cfg["bot_token"]
    chat_id = str(cfg["chat_id"])
    asyncio.run(_send_async(text, token, chat_id))


def send_digest(articles, clusters, insights, trends):
    """Send the daily digest. Failures are logged and re-raised so main.py
    records them in run_log instead of the digest vanishing silently."""
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
        raise


def send_breaking_alert(article: dict):
    """Send an immediate Telegram message for a breaking/high-score article."""
    cfg = config.settings()["telegram"]
    if not cfg.get("enabled", False):
        return
    if not cfg.get("send_breaking_immediately", True):
        return

    try:
        _send(f"🚨 <b>BREAKING ALERT</b>\n\n{_format_article(article)}")
        log.info(f"Breaking alert sent: {(article.get('title') or '')[:60]}")
    except Exception as e:
        log.error(f"Telegram alert failed: {e}")
        raise
