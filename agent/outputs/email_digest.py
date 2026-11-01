"""
outputs/email_digest.py — Generate and send the HTML email digest.

Fix #2:  Summary: / Impact: labels added
Fix #9:  total_articles = only articles actually shown (not total fetched)
Fix #10: send() is guarded by a run-level flag so it fires exactly once
"""

import smtplib
import logging
import time
import urllib.error
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


class DeliveryUnknown(Exception):
    """The connection failed after the message may already have been accepted.

    Raised when a send dies without an explicit server reply: a drop or
    timeout while the message is being sent, or SendGrid timing out after
    the request went out. Never retried. Resending could deliver the digest
    twice, so the failure goes to run_log for a person to check instead.
    Deliberately not an OSError, so _is_transient never treats it as one.
    """


_UNKNOWN_MSG = "may or may not have been delivered — check the inbox before resending"


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

    # smtplib defaults to the global socket timeout, which is normally None —
    # a stalled DATA upload would then hang the run indefinitely. Generous,
    # because a large digest is a slow upload, but bounded so a wedged
    # socket raises and _send_with_retry can try again.
    timeout = smtp.get("timeout", 120)

    # Connect, EHLO, STARTTLS and login failures are safe to retry, because
    # nothing has been sent yet. During sendmail only an explicit reply code
    # proves the message was refused. A bare disconnect or timeout there may
    # come after the server accepted it (the 250 reply lost in transit), so
    # it becomes DeliveryUnknown rather than a retry that duplicates the
    # digest. Not a `with` block: its __exit__ sends QUIT and lets a QUIT
    # timeout escape, which would turn a delivered message into a "transient"
    # failure and get it resent.
    server = smtplib.SMTP(smtp["host"], smtp["port"], timeout=timeout)
    try:
        server.ehlo()
        server.starttls()
        server.login(smtp["username"], smtp["password"])
        try:
            server.sendmail(from_addr, to_addrs, msg.as_string())
        except (smtplib.SMTPResponseException, smtplib.SMTPRecipientsRefused):
            raise   # the server answered with a refusal — nothing was accepted
        except OSError as e:
            raise DeliveryUnknown(f"SMTP connection lost mid-send ({e!r}) — {_UNKNOWN_MSG}") from e
        try:
            server.quit()
        except Exception:
            pass    # already accepted; a failed QUIT changes nothing
    finally:
        server.close()

    log.info(f"Email sent to: {to_addrs}")


def _send_sendgrid(html_body, subject):
    import sendgrid
    from sendgrid.helpers.mail import Mail
    cfg      = config.settings()["email"]
    sg       = sendgrid.SendGridAPIClient(api_key=cfg["sendgrid"]["api_key"])
    # python_http_client calls urlopen with timeout=None unless one is set
    # here (it propagates to the per-endpoint child clients), so a half-open
    # socket to api.sendgrid.com used to block the run forever.
    sg.client.timeout = cfg["sendgrid"].get("timeout", 60)
    mail     = Mail(
        from_email   = cfg["from_address"],
        to_emails    = cfg["to_addresses"],
        subject      = subject,
        html_content = html_body,
    )
    try:
        resp = sg.send(mail)
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            raise DeliveryUnknown(f"SendGrid request timed out — {_UNKNOWN_MSG}") from e
        raise   # refused / DNS failure — nothing was sent, safe to retry
    except TimeoutError as e:   # response read timed out after the request went out
        raise DeliveryUnknown(f"SendGrid response timed out — {_UNKNOWN_MSG}") from e
    log.info(f"SendGrid response: {resp.status_code}")


# Gmail answers a slow or busy DATA phase with "421 4.3.0 Temporary System
# Problem. Try again later." — a transient that a single-shot send treats as
# fatal, losing a whole digest. Two retries, backing off, cover it.
_RETRY_DELAYS = (30, 120)


def _is_transient(exc) -> bool:
    """True for SMTP failures worth retrying — 4xx and connection faults.

    Never retries auth failures: a bad app password fails identically on every
    attempt, so retrying only delays the error by 150s. Checked by type first
    because Gmail reports throttled auth as 454 (a 4xx that is not transient).

    The SMTPException arm has to come before the OSError arm: smtplib's
    exceptions all subclass OSError, so a bare OSError check would classify
    every permanent refusal (SMTPRecipientsRefused and friends, which carry
    no smtp_code) as retryable.
    """
    if isinstance(exc, (DeliveryUnknown, smtplib.SMTPAuthenticationError)):
        return False
    code = getattr(exc, "smtp_code", None)
    if isinstance(code, int):
        return 400 <= code < 500
    status = getattr(exc, "status_code", None)   # SendGrid (python_http_client)
    if isinstance(status, int):
        return status == 429 or status >= 500
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return True
    if isinstance(exc, smtplib.SMTPException):
        return False
    return isinstance(exc, OSError)   # raw socket faults: timeout, reset


def _send_with_retry(sender, html_body, subject):
    """Call `sender`, retrying transient failures with backoff.

    Only failures that prove nothing was accepted get here as transient: an
    explicit 4xx reply (a 421 at DATA is the refusal itself), or a fault
    before the message was sent. Ambiguous mid-send failures arrive as
    DeliveryUnknown and are never retried.
    """
    attempts = len(_RETRY_DELAYS) + 1
    for attempt in range(attempts):
        try:
            sender(html_body, subject)
            return
        except Exception as e:
            if attempt == attempts - 1 or not _is_transient(e):
                raise
            delay = _RETRY_DELAYS[attempt]
            log.warning(
                f"Email send failed ({e}) — transient, retrying in {delay}s "
                f"(attempt {attempt + 2}/{attempts})"
            )
            time.sleep(delay)


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
        sender    = _send_sendgrid if provider == "sendgrid" else _send_smtp
        _send_with_retry(sender, html_body, subject)
        _sent_this_run = True
    except Exception as e:
        log.error(f"Email send failed: {e}")
        raise
