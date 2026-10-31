"""Offline tests for the email retry logic in agent/outputs/email_digest.py."""

import smtplib

import pytest

from agent.outputs import email_digest
from agent.outputs.email_digest import _is_transient, _send_with_retry


@pytest.mark.parametrize(
    "exc, expected",
    [
        (smtplib.SMTPDataError(421, b"4.3.0 Temporary System Problem"), True),
        (smtplib.SMTPDataError(550, b"5.7.1 Message rejected"), False),
        (smtplib.SMTPAuthenticationError(454, b"4.7.0 Too many login attempts"), False),
        (smtplib.SMTPAuthenticationError(535, b"5.7.8 Bad credentials"), False),
        (smtplib.SMTPServerDisconnected("Connection unexpectedly closed"), True),
        (smtplib.SMTPRecipientsRefused({"x@example.com": (550, b"no such user")}), False),
        (TimeoutError("timed out"), True),
        (ConnectionResetError("reset by peer"), True),
        (ValueError("bad template"), False),
    ],
)
def test_is_transient(exc, expected):
    assert _is_transient(exc) is expected


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr(email_digest.time, "sleep", recorded.append)
    return recorded


def _sender(errors):
    calls = []

    def sender(html_body, subject):
        calls.append(subject)
        if errors:
            raise errors.pop(0)

    return sender, calls


def test_send_with_retry_recovers_from_transient_421(sleeps):
    sender, calls = _sender([smtplib.SMTPDataError(421, b"try again later")])
    _send_with_retry(sender, "<html/>", "subject")
    assert len(calls) == 2
    assert sleeps == [30]


def test_send_with_retry_raises_permanent_error_immediately(sleeps):
    sender, calls = _sender([smtplib.SMTPAuthenticationError(535, b"bad credentials")])
    with pytest.raises(smtplib.SMTPAuthenticationError):
        _send_with_retry(sender, "<html/>", "subject")
    assert len(calls) == 1
    assert sleeps == []


def test_send_with_retry_gives_up_after_all_attempts(sleeps):
    sender, calls = _sender([smtplib.SMTPServerDisconnected("x") for _ in range(5)])
    with pytest.raises(smtplib.SMTPServerDisconnected):
        _send_with_retry(sender, "<html/>", "subject")
    assert len(calls) == 3
    assert sleeps == [30, 120]
