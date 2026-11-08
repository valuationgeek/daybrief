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
from datetime import datetime
from pathlib import Path
from functools import lru_cache
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"

# ${VAR} or ${VAR:-default}  — default is used when VAR is unset/empty
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")

# NYT and NewsData take their keys as URL parameters, and Telegram puts the bot
# token in the URL path, so any logged URL or HTTP error can carry a secret.
# `&amp;` covers a URL that was HTML-escaped before it reached the log.
_SECRET_NAME = r"(?:api[-_]?key|access[-_]?token|auth[-_]?token|token|key|secret|password|passwd|pwd)"
_SECRET_URL_PARAM_RE = re.compile(
    rf"((?:[?&]|&amp;){_SECRET_NAME}=)[^&\s\"'<>)]+", re.IGNORECASE
)
# 'apikey': 'X', "token": "X", api_key=X in a repr or dict dump. The name must
# be the whole key, so "keywords: x" or "monkey = y" are left alone.
_SECRET_PAIR_RE = re.compile(
    rf"""(?<![\w-])(["']?{_SECRET_NAME}["']?\s*[:=]\s*["']?)(?!\*\*\*)[^"'\s,&)}}\]]{{6,}}""",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(\b(?:Bearer|Basic|Token)\s+)[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
_BOT_TOKEN_RE = re.compile(r"(/bot\d+:)[\w-]+")
_SECRET_MIN_LENGTH = 8   # shorter values could mask ordinary words in a log line
_secret_values = set()   # every secret value this process has loaded

# A variable or config key holds a secret when its name ENDS in one of these
# words: NYT_API_KEY, api_key, password, bot_token. Matching anywhere in the
# name would also catch "keywords" or "author" and mask watchlist terms.
_SECRET_KEY_RE = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|apikey|key|token|secret|password|passwd|pwd|pass|credentials?|auth)$",
    re.IGNORECASE,
)
_PLACEHOLDER_RE = re.compile(r"^(?:YOUR_|CHANGE_?ME|<)|\$\{", re.IGNORECASE)


def _remember_secret(value) -> None:
    """Mask this value in every later log line, stored error and traceback."""
    if isinstance(value, str):
        value = value.strip()
        if len(value) >= _SECRET_MIN_LENGTH and not _PLACEHOLDER_RE.search(value):
            _secret_values.add(value)


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
        if _SECRET_KEY_RE.search(key):
            _remember_secret(val)
            _remember_secret(os.environ[key])


def redact(text) -> str:
    """Mask API keys and tokens in text that is about to be logged or stored."""
    text = str(text)
    for secret in sorted(_secret_values, key=len, reverse=True):
        text = text.replace(secret, "***")
    text = _SECRET_URL_PARAM_RE.sub(r"\1***", text)
    text = _SECRET_PAIR_RE.sub(r"\1***", text)
    text = _BEARER_RE.sub(r"\1***", text)
    text = _BOT_TOKEN_RE.sub(r"\1***", text)
    return text


def _expand_env(value, key=""):
    """Recursively replace ${VAR} / ${VAR:-default} in strings within a config.

    Also records secrets wherever they come from: a substituted variable with a
    secret-looking name, whether it was set in .env, the OS or a scheduler, and
    any value, literal or substituted, under a secret-looking config key.
    """
    if isinstance(value, str):
        def repl(m):
            var, default = m.group(1), m.group(2)
            env_val = os.environ.get(var)
            if env_val:
                if _SECRET_KEY_RE.search(var):
                    _remember_secret(env_val)
                return env_val
            return default if default is not None else m.group(0)
        expanded = _ENV_RE.sub(repl, value)
        if key and _SECRET_KEY_RE.search(key):
            _remember_secret(expanded)
        return expanded
    if isinstance(value, dict):
        return {k: _expand_env(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v, key) for v in value]
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


def local_now() -> datetime:
    """Now in settings.yaml → agent.timezone (UTC if unset or unknown).

    Every output dates the digest with this, so the email subject, the
    Obsidian note and the Telegram header agree on the day.
    """
    try:
        tz = ZoneInfo(settings()["agent"].get("timezone") or "UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz)


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

    try:
        llm_cfg = settings().get("llm", {})
    except Exception:
        llm_cfg = {}   # no settings.yaml yet; load() reports that on its own
    ollama_timeout = (llm_cfg.get("ollama") or {}).get("timeout")
    if llm_cfg.get("provider", "ollama") == "ollama" and isinstance(ollama_timeout, (int, float)) \
            and ollama_timeout < 120:
        warnings.append(
            f"settings.yaml: llm.ollama.timeout is {ollama_timeout}s. Older example configs shipped 60, "
            f"which used to be ignored and is now enforced per call — 180 is the default; a long "
            f"article on a CPU-only model can take more than 60s"
        )

    for w in warnings:
        log.warning(f"Config check: {w}")
    return warnings


class _RedactingFormatter(logging.Formatter):
    """Redacts the finished line, so arguments and tracebacks are covered too.

    On a handler rather than a logging.Filter: logger filters don't run for
    records propagated from child loggers such as httpx, and a filter on the
    message wouldn't see the traceback text the formatter appends.
    """

    def format(self, record):
        return redact(super().format(record))


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
    formatter = _RedactingFormatter(fmt)
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=handlers)
    # At INFO, httpx logs every request's full URL, and the NYT and NewsData
    # keys are URL parameters.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _install_redacting_excepthooks()
    return logging.getLogger("daybrief")


def _install_redacting_excepthooks():
    """Mask secrets in tracebacks printed outside logging.

    An exception that escapes main.py is printed by sys.excepthook straight to
    the console (and the scheduler's captured output), bypassing the log
    formatter.
    """
    import threading
    import traceback

    def hook(exc_type, exc, tb):
        sys.stderr.write(redact("".join(traceback.format_exception(exc_type, exc, tb))))

    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        name = getattr(args.thread, "name", "thread")
        text = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        sys.stderr.write(redact(f"Exception in thread {name}:\n{text}"))

    sys.excepthook = hook
    threading.excepthook = thread_hook
