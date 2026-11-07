"""
outputs/safe_text.py — Make untrusted text safe for the format it lands in.

Titles, summaries, sources and links come from feeds or the LLM, so anyone
who controls a feed (or slips instructions into an article) chooses them.
Written raw into an Obsidian note they can add a remote image that reveals
the reader's IP, a javascript: or file: link, raw HTML, or an inline
DataviewJS query (`$= …`) that runs code if Dataview JS is enabled.
"""

import re

_WS_RE = re.compile(r"\s+")
_SAFE_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)

# Markdown/Obsidian syntax that can create links, embeds, code, HTML or break
# a table cell. Emphasis characters are left alone: they only change styling.
_MD_ESCAPES = {
    "\\": "\\\\",
    "[": "\\[",
    "]": "\\]",
    "`": "\\`",
    "|": "\\|",
    "<": "&lt;",
    ">": "&gt;",
}
_MD_RE = re.compile("|".join(re.escape(c) for c in _MD_ESCAPES))


def md_text(value) -> str:
    """Untrusted text as one line of inert Markdown."""
    text = _WS_RE.sub(" ", "" if value is None else str(value)).strip()
    return _MD_RE.sub(lambda m: _MD_ESCAPES[m.group()], text)


def md_code(value) -> str:
    """Text for inside a `code span`: no backticks, and no leading = or $=,
    which Dataview treats as an inline query or inline JS."""
    text = _WS_RE.sub(" ", "" if value is None else str(value)).replace("`", "")
    return re.sub(r"^[\s$=]+", "", text).strip()


def safe_url(value) -> str:
    """The URL if it's http(s), else "". Whitespace is percent-encoded."""
    url = "" if value is None else str(value).strip()
    if not _SAFE_SCHEME_RE.match(url):
        return ""
    return _WS_RE.sub(lambda m: "".join(f"%{ord(c):02X}" for c in m.group()), url)


def md_url(value) -> str:
    """A safe_url that can't end a Markdown link target early."""
    url = safe_url(value)
    for char, code in (("(", "%28"), (")", "%29"), ("<", "%3C"), (">", "%3E"), ("|", "%7C")):
        url = url.replace(char, code)
    return url


def md_link(text, url) -> str:
    """[text](url) when the URL is http(s); otherwise just the escaped text."""
    label = md_text(text)
    target = md_url(url)
    return f"[{label}]({target})" if target else label


def register(env) -> None:
    """Add the Markdown filters and helpers to a Jinja environment."""
    env.filters["md"] = md_text
    env.filters["md_code"] = md_code
    env.filters["safe_url"] = safe_url
    env.globals["md_link"] = md_link
