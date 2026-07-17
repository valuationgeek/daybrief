# Contributing to daybrief

Thanks for considering a contribution! This is a small, focused project — the bar for a good PR is that it keeps the pipeline simple and both LLM provider paths working.

## Dev setup

```bash
git clone https://github.com/YOURNAME/daybrief
cd daybrief
bash setup.sh          # or setup.bat on Windows
pip install ruff pytest
```

The setup script copies `config/*.example.yaml` to your personal (gitignored) `settings.yaml` / `watchlist.yaml`. You don't need any API keys for development — RSS feeds and the local Ollama path work without them.

## Checks

CI runs on every PR and must pass:

```bash
ruff check .           # lint
pytest                 # offline unit tests (no network, no LLM needed)
```

For live end-to-end testing there's a diagnostic harness that exercises each pipeline stage against real feeds and your local Ollama:

```bash
python quick_test.py             # all stages
python quick_test.py --stage 3   # just the AI analysis stage
```

## Guidelines

- **Read [ARCHITECTURE.md](ARCHITECTURE.md) first** — a few things (double scoring, the DB-complete/output-filtered rule, the article-dict contract) are deliberate and easy to "fix" by accident.
- **The article dict is a contract.** New fields start in `collector.py:_blank_article()` and get threaded through; don't invent fields mid-pipeline.
- **Keep both LLM providers working.** All generation goes through `agent/llm.py:generate()`. If you touch prompts, test with Ollama at minimum (`quick_test.py --stage 3`).
- **Config changes need example files.** If you add a setting, add it to the relevant `*.example.yaml` with a comment, and keep `scoring.weights` summing to 1.0.
- **No secrets in tracked files.** Secrets go in `.env` via `${VAR}` placeholders — CI and reviewers will reject literal credentials.
- Match the existing code style (readable, commented where non-obvious); don't add dependencies without a strong reason.

## Good first issues

Check the [good first issue](../../labels/good%20first%20issue) label. A known, well-scoped one: **add an OpenAI-compatible provider** (vLLM / LM Studio / OpenRouter / OpenAI) to `agent/llm.py` — the module was designed to make this a small, self-contained PR.

## Reporting bugs

Open an issue with: your OS, Python version, `llm.provider`, the relevant snippet from `logs/agent.log`, and (if config-related) your `feeds.yaml`/`sources.yaml` category keys. `python quick_test.py` output is usually the fastest way to show where things break.
