"""
net.py — Shared network resilience helpers.

A scheduled run on a laptop or home network meets two failure modes that a
single unguarded request turns into a lost digest:

  1. A brief outage (Wi-Fi drop, adapter reset, DNS hiccup, flaky upstream)
     aborts every in-flight connection. Fixed by with_retry(), whose default
     backoff schedule (2s + 4s + 8s) is sized to outlast a ~10-15s blip.

  2. Worse: a half-open socket blocks a worker *forever*. The run then hangs
     silently — main.py's `finally` never executes, so no run_log row is
     written and the failure is invisible. Fixed by making sure every
     outbound call has an explicit timeout (see resolve_timeout and the
     callers in collector.py).

Every outbound network call in the collector should go through with_retry().
"""

import logging
import random
import socket
import time
import urllib.error

import httpx

from . import config

log = logging.getLogger("daybrief.net")

# Errors worth retrying: the connection failed or died in transit.
# Deliberately EXCLUDES HTTP status errors (httpx.HTTPStatusError) — a 404 or
# a 401 will still be a 404 or a 401 on the third attempt, so retrying them
# just wastes the outage budget.
#
# OSError is the broad catch that matters on Windows, where a dropped adapter
# surfaces as WinError 10050/10051 ("network is down" / "unreachable").
# socket.gaierror (DNS failure) and ConnectionError are OSError subclasses and
# are listed only to document the intent.
TRANSIENT_ERRORS = (
    httpx.TransportError,       # connect/read/write timeout, connection reset
    urllib.error.URLError,      # urllib layer (OSError subclass)
    socket.gaierror,            # DNS failure — typical while the network is down
    ConnectionError,
    OSError,
)

# Defaults, overridable via feeds.yaml → limits.
_DEFAULT_ATTEMPTS   = 4
_DEFAULT_BASE_DELAY = 2.0
_DEFAULT_TIMEOUT    = 20


def _limits():
    try:
        return config.feeds().get("limits", {}) or {}
    except Exception:
        return {}


def resolve_timeout(explicit=None):
    """Timeout in seconds for a single network call. Never returns None —
    an unbounded call is the hang described in this module's docstring."""
    if explicit:
        return explicit
    return _limits().get("fetch_timeout_seconds", _DEFAULT_TIMEOUT)


def with_retry(fn, *, attempts=None, base_delay=None, what="request"):
    """
    Call fn(), retrying transient network failures with exponential backoff.

    Returns fn()'s result. If every attempt fails the last exception is
    re-raised, so existing per-feed try/except blocks still decide whether a
    given source is fatal or merely skipped.

    Backoff is 2s, 4s, 8s by default (~14s of waiting across 4 attempts),
    chosen to ride out a short network outage rather than to be polite to
    the remote host.
    """
    lim        = _limits()
    attempts   = attempts   or lim.get("retry_attempts",   _DEFAULT_ATTEMPTS)
    base_delay = base_delay or lim.get("retry_base_delay", _DEFAULT_BASE_DELAY)

    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except TRANSIENT_ERRORS as e:
            last_error = e
            if attempt == attempts:
                break
            delay = base_delay * (2 ** (attempt - 1))
            delay += random.uniform(0, delay * 0.1)   # jitter
            log.warning(
                f"{what}: {type(e).__name__}: {e} — "
                f"retrying in {delay:.1f}s ({attempt}/{attempts - 1})"
            )
            time.sleep(delay)

    log.warning(f"{what}: giving up after {attempts} attempts — {last_error}")
    raise last_error
