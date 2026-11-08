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

Every outbound network call in the collector should go through with_retry(),
and every URL that came from a feed or an API response through safe_get().
"""

import ipaddress
import logging
import random
import socket
import time
import urllib.error
from dataclasses import dataclass
from urllib.parse import urlsplit

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
    # Clamped: retry_attempts: 0 used to skip the loop entirely and then
    # `raise None` (TypeError) without ever making the request.
    attempts   = max(1, int(attempts or lim.get("retry_attempts", _DEFAULT_ATTEMPTS)))
    base_delay = max(0.0, float(base_delay or lim.get("retry_base_delay", _DEFAULT_BASE_DELAY)))

    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except httpx.UnsupportedProtocol:
            # A TransportError subclass, but a malformed or relative URL —
            # it fails identically on every attempt.
            raise
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


# ─────────────────────────────────────────────────────────────
# Fetching URLs that came from untrusted data
# ─────────────────────────────────────────────────────────────
#
# Feed links, and the redirects they lead to, are chosen by whoever controls
# the feed. Without checks, a link to http://192.168.1.1/ or
# http://localhost:11434/api/ps makes daybrief request it from inside your
# network, and the response can end up in the digest. A huge or endless
# response would also be read into memory whole.

MAX_REDIRECTS  = 5
MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_FEED_BYTES = 10 * 1024 * 1024


class FetchRefused(Exception):
    """The fetch was stopped on purpose: unsafe address, too large or too slow.

    Deliberately not an OSError, so with_retry never retries it and the
    enrichment breaker never counts it as the network being down.
    """


class HTTPStatus(Exception):
    """safe_get's equivalent of httpx.HTTPStatusError. Not retried."""


@dataclass
class SafeResponse:
    status_code: int
    headers: httpx.Headers
    content: bytes
    url: httpx.URL

    def raise_for_status(self):
        if self.status_code >= 400:
            raise HTTPStatus(f"HTTP {self.status_code} for {self.url}")
        return self


def check_public_url(url) -> None:
    """Raise FetchRefused unless url is http(s) and its host is publicly routable.

    Every address the name resolves to must be public, so a name with one
    public and one private address is refused. A DNS failure raises
    socket.gaierror, which callers treat as a network error.

    The check runs before connecting, and httpx looks the name up again, so a
    host that changes its DNS answer in between isn't caught. Through a proxy,
    the proxy does its own lookup.
    """
    parts = urlsplit(str(url))
    if parts.scheme not in ("http", "https"):
        raise FetchRefused(f"scheme {parts.scheme or '(none)'!r} not allowed")
    host = parts.hostname
    if not host:
        raise FetchRefused("URL has no host")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as e:
        raise FetchRefused(f"bad port: {e}") from None
    for *_, sockaddr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        ip = ipaddress.ip_address(sockaddr[0].split("%", 1)[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise FetchRefused(f"{host} resolves to a non-public address")


def safe_get(url, *, headers=None, timeout=None, max_bytes=MAX_PAGE_BYTES,
             accept=None, client=None, transport=None):
    """GET a URL from untrusted data, checking every redirect hop.

    Follows up to MAX_REDIRECTS redirects itself, checking each target with
    check_public_url, streams the body with a byte cap, and stops if the whole
    download takes much longer than the per-read timeout (httpx's timeout is
    per read, so a server trickling a byte at a time never trips it).

    accept:    optional predicate on the Content-Type. When it says no to a
               successful response, the body isn't downloaded and the
               response comes back with empty content.
    client:    an httpx.Client to reuse, so a pool of workers shares
               connections. Its own redirect setting is overridden.
    transport: for tests, when no client is given.

    Uses the same proxy settings as httpx.get.
    """
    timeout  = resolve_timeout(timeout)
    deadline = time.monotonic() + max(3 * timeout, 30)
    own      = client is None
    if own:
        client = httpx.Client(timeout=timeout, follow_redirects=False, transport=transport)
    try:
        for _ in range(MAX_REDIRECTS + 1):
            check_public_url(url)
            with client.stream("GET", url, headers=headers, timeout=timeout, follow_redirects=False) as resp:
                if resp.is_redirect:
                    url = resp.url.join(resp.headers["location"])
                    continue
                ctype = resp.headers.get("content-type", "")
                if accept is not None and resp.status_code < 400 and not accept(ctype):
                    return SafeResponse(resp.status_code, resp.headers, b"", resp.url)
                declared = resp.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    raise FetchRefused(f"response over {max_bytes // (1024 * 1024)} MB")
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        raise FetchRefused(f"response over {max_bytes // (1024 * 1024)} MB")
                    if time.monotonic() > deadline:
                        raise FetchRefused("download too slow")
                return SafeResponse(resp.status_code, resp.headers, bytes(body), resp.url)
        raise FetchRefused(f"more than {MAX_REDIRECTS} redirects")
    finally:
        if own:
            client.close()
