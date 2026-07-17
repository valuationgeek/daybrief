"""
quick_test.py — Diagnostic test runner for daybrief.
Tests each pipeline stage independently so you can spot issues fast.

Usage:
    python quick_test.py              # run all tests
    python quick_test.py --stage 1    # test only collection
    python quick_test.py --stage 3    # test only AI analysis
"""

import sys
import argparse
import traceback
from datetime import datetime
from pathlib import Path

# ── Colour helpers (Windows 10+ supports ANSI in terminals) ──────────────────
def green(s):  return f"\033[92m{s}\033[0m"
def red(s):    return f"\033[91m{s}\033[0m"
def yellow(s): return f"\033[93m{s}\033[0m"
def bold(s):   return f"\033[1m{s}\033[0m"
def cyan(s):   return f"\033[96m{s}\033[0m"

PASS = green("  ✓ PASS")
FAIL = red("  ✗ FAIL")
SKIP = yellow("  ─ SKIP")

results = []

def section(title):
    print(f"\n{bold(cyan('═' * 55))}")
    print(f"  {bold(title)}")
    print(f"{bold(cyan('═' * 55))}")

def ok(msg):
    print(f"{PASS}  {msg}")
    results.append(("pass", msg))

def fail(msg, err=None):
    print(f"{FAIL}  {msg}")
    if err:
        print(f"       {red(str(err))}")
    results.append(("fail", msg))

def skip(msg):
    print(f"{SKIP}  {msg}")
    results.append(("skip", msg))

def info(msg):
    print(f"       {msg}")


# ════════════════════════════════════════════════════════════
# STAGE 0 — Environment & Config
# ════════════════════════════════════════════════════════════
def test_environment():
    section("Stage 0 — Environment & Configuration")

    # Python version
    v = sys.version_info
    if v >= (3, 10):
        ok(f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        fail(f"Python {v.major}.{v.minor} — need 3.10+. Download from python.org")
        return False

    # Config files
    config_dir = Path("config")
    for fname in ["settings.yaml", "feeds.yaml", "sources.yaml", "watchlist.yaml"]:
        p = config_dir / fname
        if p.exists():
            ok(f"Config found: {p}")
        else:
            fail(f"Missing config: {p}")

    # Load config
    try:
        from agent import config
        s = config.settings()
        ok("settings.yaml loaded OK")
        info(f"Timezone : {s['agent'].get('timezone', 'UTC')}")
        info(f"Obsidian : {s['obsidian']['vault_path']}")
        info(f"LLM      : {s['llm']['provider']} / {s['llm']['ollama']['model']}")
    except Exception as e:
        fail("settings.yaml failed to load", e)

    try:
        from agent import config
        f = config.feeds()
        cats = list(f["categories"].keys())
        total_feeds = sum(len(f["categories"][c].get("feeds", [])) for c in cats)
        ok(f"feeds.yaml loaded — {len(cats)} categories, {total_feeds} RSS feeds")
    except Exception as e:
        fail("feeds.yaml failed to load", e)

    try:
        from agent import config
        wl = config.watchlist()
        ok(f"watchlist.yaml loaded — {len(wl.get('topics', []))} topics, {len(wl.get('companies', []))} companies")
    except Exception as e:
        fail("watchlist.yaml failed to load", e)

    # Database
    try:
        from agent.db import init_db, get_connection
        init_db()
        conn = get_connection()
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        conn.close()
        ok(f"SQLite DB ready — tables: {', '.join(t[0] for t in tables)}")
    except Exception as e:
        fail("Database init failed", e)

    # Output dirs
    out = Path("output/obsidian/Daily")
    out.mkdir(parents=True, exist_ok=True)
    ok(f"Output directory ready: {out.resolve()}")
    Path("logs").mkdir(exist_ok=True)
    ok("Logs directory ready: logs/")

    # API key checks
    try:
        from agent import config
        api_cfg = config.feeds().get("apis", {})

        nyt = api_cfg.get("nytimes", {})
        if nyt.get("enabled") and nyt.get("key", "").startswith("YOUR_"):
            fail("NYT API key not set — set NYT_API_KEY in .env (see .env.example)")
        elif nyt.get("enabled"):
            ok("NYT API key looks set")
        else:
            skip("NYT API disabled")

        nd = api_cfg.get("newsdata", {})
        if nd.get("enabled") and nd.get("key", "").startswith("YOUR_"):
            fail("NewsData API key not set — set NEWSDATA_API_KEY in .env (see .env.example)")
        elif nd.get("enabled"):
            ok("NewsData API key looks set")
        else:
            skip("NewsData API disabled")
    except Exception as e:
        fail("API key check failed", e)

    return True


# ════════════════════════════════════════════════════════════
# STAGE 1 — Collection
# ════════════════════════════════════════════════════════════
def test_collection():
    section("Stage 1 — News Collection (RSS Feeds)")
    from agent import config

    feed_cfg = config.feeds()
    categories = feed_cfg["categories"]

    import feedparser

    total_fetched = 0
    sample_articles = []

    for cat_key, cat_meta in categories.items():
        feeds = cat_meta["feeds"]
        cat_ok = 0
        for feed in feeds[:2]:  # Test first 2 feeds per category
            try:
                parsed = feedparser.parse(
                    feed["url"],
                    request_headers={"User-Agent": "daybrief/1.0"}
                )
                n = len(parsed.entries)
                if n > 0:
                    ok(f"[{cat_key}] {feed['source']}: {n} entries")
                    cat_ok += 1
                    total_fetched += n
                    if not sample_articles and parsed.entries:
                        e = parsed.entries[0]
                        sample_articles.append({
                            "title": getattr(e, "title", "?"),
                            "url": getattr(e, "link", ""),
                            "source": feed["source"],
                            "category": cat_key,
                        })
                else:
                    fail(f"[{cat_key}] {feed['source']}: 0 entries returned")
            except Exception as e:
                fail(f"[{cat_key}] {feed['source']}: fetch error", e)

    info(f"Total entries across tested feeds: {total_fetched}")
    if sample_articles:
        info(f"Sample title: \"{sample_articles[0]['title'][:70]}\"")

    # Run mini collect (1 feed per category, max 3 articles)
    info("\nRunning mini collect (capped at 3 articles per feed)...")
    try:
        import feedparser
        from agent.preprocessor import process

        mini_articles = []
        for cat_key, cat_meta in list(categories.items())[:2]:
            feed = cat_meta["feeds"][0]
            parsed = feedparser.parse(feed["url"])
            for entry in parsed.entries[:3]:
                link = getattr(entry, "link", "")
                title = getattr(entry, "title", "").strip()
                if link and title:
                    mini_articles.append({
                        "id": link[-16:],
                        "title": title,
                        "url": link,
                        "source": feed["source"],
                        "category": cat_key,
                        "published": datetime.utcnow().isoformat(),
                        "fetched_at": datetime.utcnow().isoformat(),
                        "body": "Test body text for stage 1 validation. " * 5,
                        "credibility": feed.get("credibility", 0.8),
                        "summary": None, "keywords": [], "sentiment": None,
                        "sentiment_label": None, "score": None, "flags": [], "cluster_id": None,
                    })

        cleaned = process(mini_articles)
        ok(f"Mini collect + preprocess: {len(mini_articles)} raw → {len(cleaned)} clean")
        return cleaned

    except Exception as e:
        fail("Mini collect failed", e)
        traceback.print_exc()
        return []


# ════════════════════════════════════════════════════════════
# STAGE 2 — Pre-processing
# ════════════════════════════════════════════════════════════
def test_preprocessing(articles=None):
    section("Stage 2 — Pre-processing")

    if not articles:
        # Create synthetic test articles
        articles = [
            {
                "id": "test001",
                "title": "Federal Reserve Raises Interest Rates Again",
                "url": "https://example.com/fed-rates",
                "source": "Reuters",
                "category": "business",
                "published": datetime.utcnow().isoformat(),
                "fetched_at": datetime.utcnow().isoformat(),
                "body": (
                    "The Federal Reserve raised interest rates by 25 basis points on Wednesday, "
                    "marking the tenth consecutive hike since March 2022. Fed Chair Jerome Powell "
                    "indicated that further increases may be necessary to bring inflation down to "
                    "the central bank's 2% target. Markets reacted with a sharp selloff in equities, "
                    "while bond yields rose to their highest levels in 16 years. Economists warned "
                    "the moves could increase the risk of recession in early 2025."
                ),
                "credibility": 0.98,
                "summary": None, "keywords": [], "sentiment": None,
                "sentiment_label": None, "score": None, "flags": [], "cluster_id": None,
            },
            {
                "id": "test002",
                "title": "OpenAI Releases GPT-5 with Multimodal Capabilities",
                "url": "https://example.com/gpt5",
                "source": "TechCrunch",
                "category": "tech",
                "published": datetime.utcnow().isoformat(),
                "fetched_at": datetime.utcnow().isoformat(),
                "body": (
                    "OpenAI unveiled GPT-5, its most capable language model to date, featuring "
                    "advanced multimodal reasoning across text, images, audio, and video. The model "
                    "demonstrates significant improvements in coding, mathematics, and scientific "
                    "reasoning benchmarks. GPT-5 will initially be available to ChatGPT Plus and "
                    "Enterprise subscribers, with API access to follow in coming weeks."
                ),
                "credibility": 0.88,
                "summary": None, "keywords": [], "sentiment": None,
                "sentiment_label": None, "score": None, "flags": [], "cluster_id": None,
            },
            {
                "id": "test003",
                "title": "Climate Summit Reaches Landmark Carbon Agreement",
                "url": "https://example.com/climate",
                "source": "BBC World",
                "category": "world",
                "published": datetime.utcnow().isoformat(),
                "fetched_at": datetime.utcnow().isoformat(),
                "body": (
                    "World leaders at COP32 signed a landmark agreement committing 190 nations to "
                    "reach net-zero carbon emissions by 2045, five years ahead of the previous target. "
                    "The deal includes a $500 billion climate finance package for developing nations "
                    "and binding annual emissions review mechanisms. Environmental groups called it "
                    "the most significant climate agreement in a decade."
                ),
                "credibility": 0.95,
                "summary": None, "keywords": [], "sentiment": None,
                "sentiment_label": None, "score": None, "flags": [], "cluster_id": None,
            },
            {
                "id": "test004",
                "title": "NVIDIA Reports Record Quarterly Revenue on AI Chip Demand",
                "url": "https://example.com/nvidia",
                "source": "Bloomberg",
                "category": "business",
                "published": datetime.utcnow().isoformat(),
                "fetched_at": datetime.utcnow().isoformat(),
                "body": (
                    "NVIDIA reported record quarterly revenue of $35 billion, beating analyst expectations "
                    "by a wide margin, driven by insatiable demand for its H100 and upcoming H200 AI chips. "
                    "The company raised its forward guidance and announced a new data center partnership with "
                    "Microsoft Azure and Google Cloud. NVIDIA shares rose 8% in after-hours trading."
                ),
                "credibility": 0.97,
                "summary": None, "keywords": [], "sentiment": None,
                "sentiment_label": None, "score": None, "flags": [], "cluster_id": None,
            },
            {
                "id": "test001_dup",
                "title": "Federal Reserve Raises Interest Rates Again",  # duplicate title
                "url": "https://example.com/fed-rates",  # duplicate URL
                "source": "Reuters",
                "category": "business",
                "published": datetime.utcnow().isoformat(),
                "fetched_at": datetime.utcnow().isoformat(),
                "body": "The Federal Reserve raised interest rates by 25 basis points on Wednesday.",
                "credibility": 0.98,
                "summary": None, "keywords": [], "sentiment": None,
                "sentiment_label": None, "score": None, "flags": [], "cluster_id": None,
            },
        ]
        info("Using 5 synthetic test articles (including 1 deliberate duplicate)")

    try:
        from agent.preprocessor import process
        cleaned = process(articles)
        ok(f"Pre-processing: {len(articles)} in → {len(cleaned)} out")
        if len(cleaned) < len(articles):
            ok(f"Deduplication removed {len(articles) - len(cleaned)} duplicate(s)")
        for a in cleaned[:2]:
            info(f"  [{a['category']}] \"{a['title'][:60]}\" — {a.get('word_count', '?')} words")
        return cleaned
    except Exception as e:
        fail("Pre-processing failed", e)
        traceback.print_exc()
        return articles[:4]  # return unprocessed for next stages


# ════════════════════════════════════════════════════════════
# STAGE 3 — AI Analysis
# ════════════════════════════════════════════════════════════
def test_analysis(articles):
    section("Stage 3 — AI Analysis (Summarize + Keywords + Sentiment)")

    if not articles:
        fail("No articles to analyze")
        return articles

    # Test VADER sentiment (always works, no external deps)
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        vader = SentimentIntensityAnalyzer()
        score = vader.polarity_scores("The economy is growing strongly despite global challenges")
        ok(f"VADER sentiment: compound={score['compound']:.3f}")
    except Exception as e:
        fail("VADER failed", e)

    # Test KeyBERT keywords
    try:
        from keybert import KeyBERT
        info("Loading KeyBERT (may take ~10s first time)…")
        kb = KeyBERT(model="all-MiniLM-L6-v2")
        sample_text = articles[0]["title"] + ". " + articles[0]["body"]
        kws = kb.extract_keywords(sample_text, top_n=5)
        ok(f"KeyBERT keywords: {[k for k,_ in kws]}")
    except Exception as e:
        fail("KeyBERT failed", e)
        traceback.print_exc()

    # Test Ollama LLM
    from agent import config
    llm_cfg = config.settings()["llm"]
    provider = llm_cfg.get("provider", "ollama")

    if provider == "ollama":
        import ollama as ollama_lib
        ollama_cfg = llm_cfg["ollama"]
        base_url   = ollama_cfg.get("base_url", "http://localhost:11434")
        target     = ollama_cfg["model"]

        ollama_running  = False
        model_ok        = False

        # ── Step 1: Is Ollama running? List installed models ──
        try:
            client      = ollama_lib.Client(host=base_url)
            model_list  = client.list()
            model_names = [m.model for m in model_list.models if m.model is not None]
            ollama_running = True
            ok(f"Ollama is running at {base_url}")
            if model_names:
                ok(f"Models installed: {', '.join(model_names)}")
            else:
                fail(f"No models installed. Open a terminal and run:  ollama pull {target}")

        except ollama_lib.ResponseError as e:
            fail(f"Ollama list error — status={e.status_code}  message='{e.error}'")
        except Exception as e:
            fail("Ollama is NOT running or not reachable")
            info(f"  Detail: {type(e).__name__}: {e}")
            info("  → On Windows: look for the Ollama icon in the system tray (bottom-right).")
            info("    If not there, open Start menu → launch Ollama, then re-run.")

        if not ollama_running:
            info("  Filling mock summaries so other test stages can still run.")
            for a in articles:
                a["summary"]         = f"[MOCK] {a['title'][:80]}"
                a["keywords"]        = ["test", "keyword"]
                a["sentiment"]       = 0.1
                a["sentiment_label"] = "neutral"
            return articles

        # ── Step 2: Is the target model installed? ────────────
        if model_names:
            base_target = target.split(":")[0]   # "llama3.2" from "llama3.2:latest"
            matched = [m for m in model_names if base_target in m]
            if matched:
                ok(f"Target model '{target}' found as: {matched[0]} ✓")
                model_ok = True
            else:
                fail(f"Model '{target}' is not installed.")
                info(f"  Installed models: {model_names}")
                info(f"  Fix: open a terminal and run:  ollama pull {target}")
                info(f"  OR change 'llm.ollama.model' in config/settings.yaml to one of: {model_names}")

        # ── Step 3: Smoke test — send a real prompt ───────────
        if model_ok:
            try:
                info("  Sending a test prompt to the model (may take 10-30s on first call)…")
                actual_model = matched[0]   # use the exact installed name
                resp  = client.generate(model=actual_model, prompt="Reply with just the word READY.")
                reply = (resp.response or "").strip()
                if reply:
                    ok(f"LLM smoke test passed ✓  Model replied: \"{reply[:60]}\"")
                else:
                    fail("LLM returned an empty response — model may be corrupted")
                    info(f"  Try:  ollama rm {actual_model}  then  ollama pull {actual_model}")
            except ollama_lib.ResponseError as e:
                fail(f"LLM generate error — status={e.status_code}  message='{e.error}'")
                info(f"  The model name in settings.yaml is '{target}'")
                info(f"  Installed names are: {model_names}")
                info("  Make sure they match — update config/settings.yaml if needed.")
            except Exception as e:
                fail(f"LLM smoke test failed: {type(e).__name__}: {e}")
    elif provider == "openai":
        import importlib.util
        if importlib.util.find_spec("openai") is None:
            fail("openai package not installed — run: pip install openai")
        else:
            key = llm_cfg["openai"].get("api_key", "")
            if key:
                ok("OpenAI-compatible API key found")
            else:
                fail("OpenAI-compatible API key missing — set OPENAI_API_KEY in .env")

    # Run analysis on first 2 articles only (save time in tests)
    info(f"\nRunning full analysis on {min(2, len(articles))} article(s) (capped for speed)…")
    try:
        from agent.analyzer import analyze_article
        test_subset = articles[:2]
        for a in test_subset:
            analyzed = analyze_article(a)
            ok(f"Analyzed: \"{analyzed['title'][:50]}\"")
            info(f"  Summary   : {(analyzed.get('summary') or '')[:100]}…")
            info(f"  Keywords  : {analyzed.get('keywords', [])[:5]}")
            info(f"  Sentiment : {analyzed.get('sentiment_label')} ({analyzed.get('sentiment', 0):.3f})")

        # Fill remaining articles with mock data for downstream tests
        for a in articles[2:]:
            if not a.get("summary"):
                a["summary"] = f"Summary of: {a['title']}"
                a["keywords"] = ["news", "update", a["category"]]
                a["sentiment"] = 0.0
                a["sentiment_label"] = "neutral"

        return articles
    except Exception as e:
        fail("Analysis failed", e)
        traceback.print_exc()
        return articles


# ════════════════════════════════════════════════════════════
# STAGE 4 — Fusion
# ════════════════════════════════════════════════════════════
def test_fusion(articles):
    section("Stage 4 — Cross-Source Fusion (Semantic Clustering)")

    if not articles:
        fail("No articles for fusion")
        return articles, []

    # Add a second similar article to force a cluster
    similar = dict(articles[0])
    similar["id"] = similar["id"] + "_b"
    similar["url"] = similar["url"] + "_b"
    similar["source"] = "Bloomberg"
    similar["title"] = "Fed Hikes Rates Again Amid Inflation Fight"
    all_articles = articles + [similar]

    try:
        from agent.fusion import fuse_all
        info(f"Running fusion on {len(all_articles)} articles…")
        updated, clusters = fuse_all(all_articles)
        ok(f"Fusion complete: {len(clusters)} cluster(s) formed")
        for c in clusters:
            info(f"  Cluster: {c['source_count']} sources — \"{c.get('unified_summary', '')[:80]}\"")
            info(f"  Keywords: {c.get('top_keywords', [])[:4]}")
        return updated, clusters
    except Exception as e:
        fail("Fusion failed", e)
        traceback.print_exc()
        return articles, []


# ════════════════════════════════════════════════════════════
# STAGE 5 — Scoring
# ════════════════════════════════════════════════════════════
def test_scoring(articles, clusters):
    section("Stage 5 — Scoring & Flagging")

    if not articles:
        fail("No articles to score")
        return articles

    try:
        from agent.scorer import score_all
        scored = score_all(articles, clusters)
        ok(f"Scored {len(scored)} articles")
        for a in scored[:5]:
            flags = a.get("flags", [])
            flag_str = " ".join(flags) if flags else "none"
            info(f"  [{a['source']:15}] score={a.get('score', 0):.3f}  flags={flag_str}  \"{a['title'][:50]}\"")
        return scored
    except Exception as e:
        fail("Scoring failed", e)
        traceback.print_exc()
        return articles


# ════════════════════════════════════════════════════════════
# STAGE 6 — Decision Support
# ════════════════════════════════════════════════════════════
def test_decision(articles, clusters):
    section("Stage 6 — Decision Support (Trends & Insights)")

    try:
        from agent.decision import detect_trends, generate_insights, get_alerts
        trends = detect_trends(articles)
        ok(f"Trend detection complete — {len(trends)} trends found")
        if trends:
            for t in trends[:3]:
                info(f"  Trending: '{t['keyword']}' ({t['spike_factor']}× above avg)")
        else:
            info("  No trends yet (need 3+ days of history — expected on first run)")

        insights = generate_insights(articles, clusters, trends)
        ok(f"Generated {len(insights)} insight(s)")
        for i in insights[:4]:
            info(f"  {i}")

        alerts = get_alerts(articles)
        ok(f"Alert check: {len(alerts)} article(s) above alert threshold")

        return trends, insights
    except Exception as e:
        fail("Decision support failed", e)
        traceback.print_exc()
        return [], []


# ════════════════════════════════════════════════════════════
# OUTPUTS — Obsidian write test
# ════════════════════════════════════════════════════════════
def test_obsidian_output(articles, clusters, insights, trends):
    section("Output — Obsidian Markdown Write Test")

    try:
        from agent.outputs.obsidian import write
        from agent import config
        vault = config.settings()["obsidian"]["vault_path"]
        info(f"Writing to: {vault}")
        write(articles, clusters, insights, trends, run_date="TEST-" + datetime.utcnow().date().isoformat())

        # Find the written file
        from pathlib import Path
        out_dir = Path(vault) / config.settings()["obsidian"]["subfolder"]
        written = list(out_dir.glob("TEST-*.md"))
        if written:
            ok(f"Obsidian note written: {written[0]}")
            info(f"  Size: {written[0].stat().st_size} bytes")
            info("  Open in Obsidian or any text editor to verify")
        else:
            fail("Obsidian file not found after write — check vault_path in settings.yaml")
    except Exception as e:
        fail("Obsidian write failed", e)
        traceback.print_exc()


# ════════════════════════════════════════════════════════════
# SUMMARY
# ════════════════════════════════════════════════════════════
def print_summary():
    section("Test Summary")
    passed = sum(1 for r in results if r[0] == "pass")
    failed = sum(1 for r in results if r[0] == "fail")
    skipped = sum(1 for r in results if r[0] == "skip")
    total = len(results)

    print(f"\n  {green(f'{passed} passed')}  {red(f'{failed} failed')}  {yellow(f'{skipped} skipped')}  of {total} checks")

    if failed > 0:
        print(f"\n  {bold('Failed checks:')}")
        for r in results:
            if r[0] == "fail":
                print(f"  {red('✗')} {r[1]}")
        print(f"\n  {yellow('Fix the items above, then re-run:  python quick_test.py')}")
    else:
        print(f"\n  {green(bold('All checks passed! Run the full pipeline with:'))}")
        print(f"  {cyan('  python main.py')}")
        print(f"  {cyan('  (or double-click run.bat)')}")
    print()


# ════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════
if __name__ == "__main__":
    # Enable ANSI colours on Windows
    import os
    os.system("")

    parser = argparse.ArgumentParser(description="daybrief diagnostic test")
    parser.add_argument("--stage", type=int, choices=[0,1,2,3,4,5,6], default=None,
                        help="Run only a specific stage (0–6). Default: all stages.")
    args = parser.parse_args()

    print(bold(cyan("\n  daybrief — Diagnostic Test")))
    print(f"  Path: {Path('.').resolve()}")
    print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    stage = args.stage
    articles = []
    clusters = []
    insights = []
    trends = []

    if stage is None or stage == 0:
        env_ok = test_environment()
        if not env_ok and stage == 0:
            print_summary()
            sys.exit(1)

    if stage is None or stage == 1:
        live_articles = test_collection()
        if live_articles:
            articles = live_articles

    if stage is None or stage == 2:
        articles = test_preprocessing(articles if articles else None)

    if stage is None or stage == 3:
        articles = test_analysis(articles)

    if stage is None or stage == 4:
        articles, clusters = test_fusion(articles)

    if stage is None or stage == 5:
        articles = test_scoring(articles, clusters)

    if stage is None or stage == 6:
        trends, insights = test_decision(articles, clusters)

    if stage is None:
        test_obsidian_output(articles, clusters, insights, trends)

    print_summary()
