"""
outputs/labels.py — How each sentiment label is shown, in one place.

Obsidian, email and Telegram all read this table, so a new label or icon is
one edit here. A None label means "not measured" (non-English articles,
which VADER can't read, or an article whose analysis failed) and is shown
as nothing, never as neutral.
"""

# label: (emoji for Obsidian and Telegram, CSS class for the email badge)
SENTIMENT = {
    "positive": ("😊", "pos"),
    "negative": ("😟", "neg"),
    "neutral":  ("😐", "neu"),
}


def sentiment_emoji(label) -> str:
    """The label's emoji, or "" for None or an unknown label."""
    return SENTIMENT.get(label, ("", ""))[0]


def sentiment_class(label) -> str:
    """The email badge class. Templates show a badge only when there is a label."""
    return SENTIMENT.get(label, ("", "neu"))[1]
