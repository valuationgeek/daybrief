# Architecture

daybrief is a linear 6-stage pipeline orchestrated by `main.py:run_pipeline()`. Each stage is a module under `agent/` that transforms a list of **article dicts** and passes it on:

| Stage | Module | What it does |
|---|---|---|
| 1 | `collector.py` | Fetch RSS feeds + NYT API + NewsData API in one pass. Enriches short bodies via full-page fetch (`trafilatura`). |
| 2 | `preprocessor.py` | Clean boilerplate, dedupe (content hash), drop short/empty, normalize dates. |
| 3 | `analyzer.py` | Per-article LLM summary + "read angle" (audience/impact sentence), KeyBERT keywords, VADER sentiment. Parallelized via `ThreadPoolExecutor`. |
| 4 | `fusion.py` | Embed all articles (`sentence-transformers`), greedy single-linkage clustering across **all** categories, LLM-generated unified cross-source summary per multi-article cluster. |
| 5 | `scorer.py` | Composite score from weighted components, assign `BREAKING`/`PRIORITY`/`CRISIS` flags, split articles by per-category **score barrier**. |
| 6 | `decision.py` | Keyword trend detection (spike ratio vs. DB history), actionable insights, alert selection. |

Outputs live in `agent/outputs/` (`obsidian.py`, `email_digest.py`, `telegram_bot.py`), each rendering shown articles/clusters/insights/trends. Templates are Jinja2 in `templates/`. All LLM calls go through `agent/llm.py`.

## Critical flow details (not obvious from any single file)

- **Scoring runs twice.** `main.py` calls `score_all(articles, [])` *before* fusion (so fusion can use scores), then `score_all(articles, clusters)` again after, so cluster size can factor into the final score. Don't remove either call.
- **The DB is a complete record; output is filtered.** Every scored article/cluster is saved to SQLite *before* the score barrier is applied. `scorer.filter_by_barrier` / `filter_clusters_by_barrier` decide what reaches the outputs — filtered-out items stay in the DB only.
- **The article dict shape is a contract.** `collector.py:_blank_article()` defines the canonical shape every downstream stage expects. Add new fields there and thread them through — don't invent fields mid-pipeline.
- **`config.feeds()` aliases `rss_feeds` → `categories`.** `feeds.yaml` uses the `rss_feeds:` key, but downstream code reads `config.feeds()["categories"]`. The alias is injected in `config.py`.
- **Non-English articles bypass the LLM.** In `analyzer.py`, articles with `language != "en"` keep their original text (no translation) and get a blank read angle.
- **The Ollama client bypasses the system proxy.** Always create it via `agent/ollama_client.py:make_client()` (`trust_env=False`) — this fixes HTTP 502 errors behind a VPN/corporate proxy. Don't instantiate `ollama.Client` directly.
- **The LLM provider is switchable.** `settings.yaml:llm.provider` is `"ollama"` (local, default) or `"openai"` (any OpenAI-compatible endpoint via `llm.openai.base_url`). The single branch point is `agent/llm.py:generate()`, which returns `""` on failure — each caller has its own degradation path (analyzer falls back to lead sentences, fusion to joined summaries). Keep both provider paths working when editing prompts.

## Categories

Categories are defined once, in `feeds.yaml:rss_feeds` — the key order there sets display order in every output. Everything else falls back gracefully:

- `sources.yaml:scoring.score_barriers` and `max_solo_per_category` use per-category entries with a `default:` fallback, so a brand-new category works without touching them.
- `config.validate()` (called at pipeline start) cross-checks the category keys across the two files and warns on gaps or typos — it never fails the run.
- Insight generation reads its target categories from `sources.yaml:insights.market_signal_categories`.
- The API collectors map their results into category keys configurable in `feeds.yaml` (`apis.nytimes.world_category`, `apis.newsdata.category`).

## Trend detection

`decision.py:detect_trends` uses a smoothed **spike ratio**, not a z-score: `today_count / (baseline + smoothing)`, where `baseline` is the keyword's average daily frequency over `lookback_days` (absent days counted as zero). This is deliberate — KeyBERT emits diverse 1–2 word phrases that almost always appear once per day, so per-day variance is ~0 and a z-score can never fire. Tuning lives in `sources.yaml:trends`. Trends need a few days of history in the DB before they can fire.

## Data

SQLite at `db/news.sqlite` (schema in `agent/db.py:init_db`): `articles`, `clusters`, `keyword_freq` (powers trend detection), `run_log`. `purge_old_articles` trims to `agent.history_days`. The directory and file are created automatically on first run.

Because dedupe and trends depend on this history, the DB matters for deployment: on ephemeral runners (e.g. GitHub Actions) it must be persisted between runs — the shipped `digest.yml` workflow does this with `actions/cache`.

## Configuration

All YAML in `config/`, loaded and cached in `agent/config.py`:

- **settings.yaml** (personal, gitignored — copy from `settings.example.yaml`) — outputs, LLM provider, timezone, logging.
- **feeds.yaml** — RSS feeds grouped by category, API toggles, fetch limits.
- **sources.yaml** — scoring: credibility table, composite weights (**must sum to 1.0**), score barriers, flag thresholds, insight categories, trend params.
- **watchlist.yaml** (personal, gitignored — copy from `watchlist.example.yaml`) — topics/companies/regions that boost scores and drive alerts.

Secrets are never stored in YAML: files hold `${VAR}` placeholders; real values come from environment variables auto-loaded from a gitignored `.env` (see `.env.example`). The loader/substitution lives in `agent/config.py` (`_load_dotenv`, `_expand_env`).

To add a scoring component: implement a `0.0–1.0` function in `scorer.py:score_article`, add its weight to `sources.yaml:scoring.weights`, and rebalance so the weights still sum to 1.0.
