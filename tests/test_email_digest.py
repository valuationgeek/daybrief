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


# ── _send_smtp: only provably-undelivered failures are retried ──

@pytest.fixture
def smtp_settings(monkeypatch):
    settings = {"email": {"smtp": {"host": "smtp.example.com", "port": 587, "username": "u",
                                   "password": "p", "timeout": 120},
                          "from_address": "a@example.com", "to_addresses": ["b@example.com"]}}
    monkeypatch.setattr(email_digest.config, "settings", lambda: settings)


class FakeSMTP:
    """One scripted behaviour per connection: None, or an exception to raise
    at 'login' or 'sendmail'."""
    script = []
    connections = []

    def __init__(self, host, port, timeout=None):
        self.step = FakeSMTP.script.pop(0) if FakeSMTP.script else None
        self.timeout = timeout
        self.sent = False
        FakeSMTP.connections.append(self)

    def ehlo(self):
        pass

    def starttls(self):
        pass

    def login(self, user, password):
        if self.step and self.step[0] == "login":
            raise self.step[1]

    def sendmail(self, from_addr, to_addrs, msg):
        if self.step and self.step[0] == "sendmail":
            raise self.step[1]
        self.sent = True

    def quit(self):
        raise TimeoutError("QUIT timed out")   # must not turn a delivered message into a failure

    def close(self):
        pass


@pytest.fixture
def fake_smtp(monkeypatch, smtp_settings):
    FakeSMTP.script, FakeSMTP.connections = [], []
    monkeypatch.setattr(email_digest.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def test_disconnect_during_sendmail_is_delivery_unknown_and_not_retried(fake_smtp, sleeps):
    fake_smtp.script = [("sendmail", smtplib.SMTPServerDisconnected("lost"))]
    with pytest.raises(email_digest.DeliveryUnknown):
        _send_with_retry(email_digest._send_smtp, "<html/>", "subject")
    assert len(fake_smtp.connections) == 1
    assert sleeps == []


def test_timeout_during_sendmail_is_delivery_unknown(fake_smtp, sleeps):
    fake_smtp.script = [("sendmail", TimeoutError("timed out waiting for 250"))]
    with pytest.raises(email_digest.DeliveryUnknown):
        _send_with_retry(email_digest._send_smtp, "<html/>", "subject")
    assert sleeps == []


def test_disconnect_during_login_is_retried(fake_smtp, sleeps):
    fake_smtp.script = [("login", smtplib.SMTPServerDisconnected("lost")), None]
    _send_with_retry(email_digest._send_smtp, "<html/>", "subject")
    assert [c.sent for c in fake_smtp.connections] == [False, True]
    assert sleeps == [30]


def test_explicit_421_at_data_is_retried(fake_smtp, sleeps):
    fake_smtp.script = [("sendmail", smtplib.SMTPDataError(421, b"Temporary System Problem")), None]
    _send_with_retry(email_digest._send_smtp, "<html/>", "subject")
    assert [c.sent for c in fake_smtp.connections] == [False, True]


def test_quit_timeout_after_delivery_is_ignored(fake_smtp, sleeps):
    email_digest._send_smtp("<html/>", "subject")
    assert fake_smtp.connections[0].sent and fake_smtp.connections[0].timeout == 120


# ── SendGrid ─────────────────────────────────────────────────

class _StatusError(Exception):
    def __init__(self, status_code):
        super().__init__(status_code)
        self.status_code = status_code


@pytest.mark.parametrize("status, expected", [(503, True), (429, True), (400, False), (401, False)])
def test_sendgrid_status_codes(status, expected):
    assert _is_transient(_StatusError(status)) is expected


def test_delivery_unknown_is_never_transient():
    assert _is_transient(email_digest.DeliveryUnknown("x")) is False


def test_sendgrid_timeout_is_delivery_unknown_and_client_has_a_timeout(monkeypatch):
    sendgrid = pytest.importorskip("sendgrid")
    import urllib.error

    settings = {"email": {"sendgrid": {"api_key": "k", "timeout": 45},
                          "from_address": "a@example.com", "to_addresses": ["b@example.com"]}}
    monkeypatch.setattr(email_digest.config, "settings", lambda: settings)
    seen = {}

    def send(self, mail):
        seen["timeout"] = self.client.timeout
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(sendgrid.SendGridAPIClient, "send", send)
    with pytest.raises(email_digest.DeliveryUnknown):
        email_digest._send_sendgrid("<html/>", "subject")
    assert seen["timeout"] == 45
