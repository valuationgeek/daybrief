"""Fail if a commit in a range matches the repository's blocked identities.

    python tools/check_commits.py BASE HEAD

The patterns come from the BLOCKED_COMMIT_IDENTITIES environment variable (in
CI, the repository variable of the same name): one regular expression, matched
case-insensitively against each commit's author and committer ("Name <email>")
and against every line of its message, such as a "Co-authored-by:" trailer.
Keeping the list in a repository variable means this public file names no one.

With no patterns set (the variable isn't defined, or a pull request from a
fork can't read it) the check passes with a note.
"""

import os
import re
import subprocess
import sys

FIELD_SEP, RECORD_SEP = "\x1f", "\x1e"


def commits(base, head):
    """(sha, author, committer, message) for each commit in base..head."""
    fmt = FIELD_SEP.join(["%H", "%an <%ae>", "%cn <%ce>", "%B"]) + RECORD_SEP
    out = subprocess.run(["git", "log", f"--format={fmt}", f"{base}..{head}"],
                         capture_output=True, text=True, encoding="utf-8", check=True).stdout
    for record in out.split(RECORD_SEP):
        record = record.lstrip("\n")
        if record:
            yield tuple(record.split(FIELD_SEP, 3))


def problems(entries, pattern):
    """(short sha, field) for every match. The matched text itself isn't returned,
    so the CI log doesn't repeat what the repository wants kept out."""
    found = []
    for sha, author, committer, message in entries:
        for field, text in (("author", author), ("committer", committer)):
            if pattern.search(text):
                found.append((sha[:7], field))
        for number, line in enumerate(message.splitlines(), 1):
            if pattern.search(line):
                found.append((sha[:7], f"message line {number}"))
    return found


def main(argv):
    if len(argv) != 3:
        sys.exit(__doc__)
    raw = os.environ.get("BLOCKED_COMMIT_IDENTITIES", "").strip()
    if not raw:
        print("No blocked identities are configured for this run; skipping the check.")
        return 0
    pattern = re.compile(raw, re.IGNORECASE)
    found = problems(commits(argv[1], argv[2]), pattern)
    for sha, field in found:
        print(f"{sha}: {field} matches a blocked identity")
    if found:
        print("Rewrite these commits (e.g. git rebase -i, then edit the message or --reset-author) and push again.")
        return 1
    print("No commit in this range matches a blocked identity.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
