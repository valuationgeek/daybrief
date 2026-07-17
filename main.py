"""
main.py — Pipeline orchestrator and the sole entry point.
Runs all 6 stages in sequence and dispatches outputs, then exits.
Schedule it with your OS scheduler (cron / systemd / launchd /
Task Scheduler) — see docs/scheduling.md for copy-paste recipes.
"""

import uuid
from datetime import datetime

from agent import config, db
from agent.collector import collect_all
from agent.preprocessor import process
from agent.analyzer import analyze_all
from agent.fusion import fuse_all
from agent.scorer import score_all, filter_by_barrier, filter_clusters_by_barrier
from agent.decision import detect_trends, generate_insights, get_alerts
from agent.outputs import obsidian, email_digest, telegram_bot

log = config.setup_logging()


def run_pipeline():
    run_id = str(uuid.uuid4())[:8]
    started_at = datetime.utcnow().isoformat()
    log.info(f"═══ Pipeline run {run_id} started ═══")
    config.validate()   # warn about category/weight mismatches across config files

    stats = {
        "run_id": run_id,
        "started_at": started_at,
        "articles_fetched": 0,
        "articles_new": 0,
        "clusters_formed": 0,
        "status": "ok",
        "errors": "",
    }

    try:
        # ── Stage 1: Collect ──────────────────────────
        log.info("Stage 1 — Collecting articles…")
        raw_articles = collect_all()
        stats["articles_fetched"] = len(raw_articles)

        # ── Stage 2: Pre-process ──────────────────────
        log.info("Stage 2 — Pre-processing…")
        articles = process(raw_articles)
        stats["articles_new"] = len(articles)

        if not articles:
            log.warning("No new articles after pre-processing — pipeline complete (nothing to do)")
            stats["status"] = "empty"
            db.log_run(stats)
            return

        # ── Stage 3: AI Analysis ──────────────────────
        log.info("Stage 3 — AI analysis…")
        articles = analyze_all(articles, max_workers=4)

        # Preliminary scoring so fusion can use scores
        log.info("Stage 5a — Preliminary scoring for fusion input…")
        articles = score_all(articles, [])

        # ── Stage 4: Cross-source Fusion ─────────────
        log.info("Stage 4 — Cross-source fusion…")
        articles, clusters = fuse_all(articles)
        stats["clusters_formed"] = len(clusters)

        # ── Stage 5: Final Scoring & Flagging ─────────
        log.info("Stage 5b — Final scoring with cluster data…")
        articles = score_all(articles, clusters)

        # ── Persist ALL articles to DB (before barrier) ──
        # We save everything so the DB is a complete historical record,
        # even for articles that won't appear in today's output.
        log.info("Saving to database…")
        for article in articles:
            db.save_article(article)
        for cluster in clusters:
            db.save_cluster(cluster)

        # ── Apply score barriers ──────────────────────
        # This splits articles into shown / filtered_out.
        # Only 'shown' articles go to outputs — filtered_out are in DB only.
        log.info("Applying score barriers…")
        shown_articles, filtered_out = filter_by_barrier(articles)
        shown_clusters = filter_clusters_by_barrier(clusters)
        stats["articles_shown"]   = len(shown_articles)
        stats["articles_filtered"] = len(filtered_out)

        # ── Stage 6: Decision Support ─────────────────
        log.info("Stage 6 — Decision support…")
        trends   = detect_trends(shown_articles)
        insights = generate_insights(shown_articles, shown_clusters, trends)
        alerts   = get_alerts(shown_articles)

        log.info(
            f"Pipeline complete: {len(articles)} fetched, "
            f"{len(shown_articles)} shown ({len(filtered_out)} below barrier), "
            f"{len(shown_clusters)} clusters, {len(trends)} trends, {len(alerts)} alerts"
        )

        # ── Outputs ───────────────────────────────────
        log.info("Writing outputs…")

        obsidian.write(shown_articles, shown_clusters, insights, trends)
        email_digest.send(shown_articles, shown_clusters, insights, trends)
        telegram_bot.send_digest(shown_articles, shown_clusters, insights, trends)

        for alert_article in alerts[:3]:
            telegram_bot.send_breaking_alert(alert_article)

        # ── Maintenance ───────────────────────────────
        settings = config.settings()
        db.purge_old_articles(days=settings["agent"].get("history_days", 90))

    except Exception as e:
        log.exception(f"Pipeline failed: {e}")
        stats["status"] = "error"
        stats["errors"] = str(e)
        raise
    finally:
        stats["finished_at"] = datetime.utcnow().isoformat()
        db.log_run(stats)
        log.info(f"═══ Pipeline run {run_id} finished ({stats['status']}) ═══")


if __name__ == "__main__":
    db.init_db()
    run_pipeline()
