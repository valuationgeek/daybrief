"""Tests for tools/check_commits.py, against a throwaway git repository.

The blocked identity here is a made-up bot, so this file names no one either.
"""

import importlib.util
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "check_commits", Path(__file__).parent.parent / "tools" / "check_commits.py")
check_commits = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_commits)

BLOCKED = r"blocked\.example|^\s*co-authored-by:.*\bbot\b"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.run(["git", "-c", "user.name=Dev", "-c", "user.email=dev@example.com",
                               "-c", "core.hooksPath=/dev/null", *args],
                              cwd=tmp_path, check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    git("commit", "-q", "--allow-empty", "-m", "Base")
    monkeypatch.chdir(tmp_path)
    return git


def _run(repo, monkeypatch, pattern=BLOCKED):
    monkeypatch.setenv("BLOCKED_COMMIT_IDENTITIES", pattern)
    return check_commits.main(["check_commits.py", "HEAD~1", "HEAD"])


def test_clean_commit_passes(repo, monkeypatch):
    repo("commit", "-q", "--allow-empty", "-m", "Fix the parser\n\nCo-authored-by: Jane <jane@example.com>")
    assert _run(repo, monkeypatch) == 0


def test_blocked_co_author_line_fails(repo, monkeypatch, capsys):
    repo("commit", "-q", "--allow-empty", "-m", "Fix the parser\n\nCo-authored-by: Helper Bot <bot@blocked.example>")
    assert _run(repo, monkeypatch) == 1
    out = capsys.readouterr().out
    assert "message line 3" in out and "blocked.example" not in out   # names the field, not the text


def test_blocked_author_fails(repo, monkeypatch, capsys):
    repo("commit", "-q", "--allow-empty", "--author", "Helper <helper@blocked.example>", "-m", "Tidy")
    assert _run(repo, monkeypatch) == 1
    assert "author matches" in capsys.readouterr().out


def test_prose_mentioning_a_word_is_not_a_trailer(repo, monkeypatch):
    repo("commit", "-q", "--allow-empty", "-m", "Teach the bot parser about dates")
    assert _run(repo, monkeypatch) == 0


def test_no_patterns_configured_skips(repo, monkeypatch, capsys):
    repo("commit", "-q", "--allow-empty", "--author", "Helper <helper@blocked.example>", "-m", "Tidy")
    assert _run(repo, monkeypatch, pattern="") == 0
    assert "skipping" in capsys.readouterr().out
