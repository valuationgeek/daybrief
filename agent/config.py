"""
config.py — Load all YAML configs and set up logging.

Secrets (SMTP password, API keys) are NOT stored in the YAML files. Instead the
YAML holds ${VAR} placeholders that are filled from environment variables at load
time. Real values live in a gitignored `.env` file at the project root (loaded
automatically below) or in the OS environment. See `.env.example` for the list.
"""

import os
import re
import yaml
import logging
import sys
from pathlib import Path
from functools import lru_cache

PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"

# ${VAR} or ${VAR:-default}  — default is used when VAR is unset/empty
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")


def _load_dotenv():
    """Load KEY=VALUE lines from project-root .env into os.environ.

    Minimal parser (no external dependency). Existing environment variables
    always win, so you can still override .env from the OS/Task Scheduler.
    """
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def _expand_env(value):
    """Recursively replace ${VAR} / ${VAR:-default} in strings within a config."""
    if isinstance(value, str):
        def repl(m):
            var, default = m.group(1), m.group(2)
            env_val = os.environ.get(var)
            if env_val:
                return env_val
            return default if default is not None else m.group(0)
        return _ENV_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


_load_dotenv()


@lru_cache(maxsize=None)
def load(name: str) -> dict:
    path = CONFIG_DIR / f"{name}.yaml"
    if not path.exists():
        example = CONFIG_DIR / f"{name}.example.yaml"
        if example.exists():
            raise FileNotFoundError(
                f"{path} not found. Copy {example.name} to {path.name} "
                f"(in the config/ folder) and fill in your values."
            )
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return _expand_env(data)


def feeds() -> dict:
    """
    Return the feeds config.
    Also injects a 'categories' key that mirrors 'rss_feeds',
    so all downstream code can use config.feeds()['categories']
    without needing to know about the rename.
    """
    data = load("feeds")
    # Provide 'categories' as an alias for 'rss_feeds' so the
    # rest of the codebase works without any changes.
    if "categories" not in data and "rss_feeds" in data:
        data = dict(data)
        data["categories"] = data["rss_feeds"]
    return data

def sources() -> dict:
    return load("sources")

def watchlist() -> dict:
    return load("watchlist")

def settings() -> dict:
    return load("settings")


def validate() -> list:
    """Cross-check category keys and scoring weights across the config files.

    Never raises — categories missing from sources.yaml fall back to the
    'default' entries at runtime, so problems are reported as warnings.
    Returns the list of warning strings (also logged) so callers/tests can
    inspect them.
    """
    log = logging.getLogger("daybrief.config")
    warnings = []
    feed_cats = set(feeds().get("categories", {}))
    scoring = sources().get("scoring", {})

    for key in ("score_barriers", "max_solo_per_category"):
        table = scoring.get(key, {})
        if "default" not in table:
            warnings.append(
                f"sources.yaml: {key} has no 'default' entry — "
                f"unlisted categories fall back to a hardcoded default"
            )
        for cat in feed_cats - set(table):
            warnings.append(
                f"category '{cat}' (feeds.yaml) has no {key} entry in "
                f"sources.yaml — using the 'default' value"
            )
        for cat in set(table) - feed_cats - {"default"}:
            warnings.append(
                f"sources.yaml: {key} lists '{cat}' which is not a category "
                f"in feeds.yaml — possible typo, entry is ignored"
            )

    weights = scoring.get("weights", {})
    if weights and abs(sum(weights.values()) - 1.0) > 0.001:
        warnings.append(
            f"sources.yaml: scoring.weights sum to {sum(weights.values()):.3f}, "
            f"expected 1.0 — scores will be skewed"
        )

    for w in warnings:
        log.warning(f"Config check: {w}")
    return warnings


def setup_logging():
    s = settings()
    level = getattr(logging, s["agent"].get("log_level", "INFO"))
    project_root = Path(__file__).parent.parent
    log_file = project_root / s["agent"].get("log_file", "logs/agent.log")
    log_file.parent.mkdir(parents=True, exist_ok=True)

    fmt = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(log_file), encoding="utf-8"),
    ]
    logging.basicConfig(level=level, format=fmt, handlers=handlers)
    return logging.getLogger("daybrief")
