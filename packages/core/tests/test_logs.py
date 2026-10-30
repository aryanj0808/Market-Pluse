"""Credentials never reach a log line, and untrusted news links are web links only."""

import io
import logging
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from mp_core.logs import REDACTED, RedactingFormatter, configure_logging, redact
from mp_core.models import Source
from mp_core.news.models import RawNewsItem

TG = "123456789:AAH" + "x" * 32


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (f"HTTP Request: POST https://api.telegram.org/bot{TG}/sendMessage", TG),
        (f"token is {TG}", TG),
        ("key sk-ant-api03-AbC_dEf-123 leaked", "sk-ant-api03-AbC_dEf-123"),
        ("headers {'Authorization': 'token emzkey:Acc3ssT0ken'}", "Acc3ssT0ken"),
        ("Authorization: Bearer eyJhbGciOi.payload.sig", "eyJhbGciOi.payload.sig"),
        ('"GET /?action=login&status=success&request_token=Oq3zuRqrHowzDq30 HTTP/1.1" 200', "Oq3zuRqrHowzDq30"),
        ("GET /callback?code=Up5t0xC0de HTTP/1.1", "Up5t0xC0de"),
        ("connect postgresql://pulse:s3cretPw@timescale:5432/market_pulse failed", "s3cretPw"),
        ("redis://default:r3dispw@redis:6379/0", "r3dispw"),
    ],
)
def test_redact(text, secret):
    out = redact(text)
    assert secret not in out and REDACTED in out


def test_ordinary_lines_untouched():
    for line in ("ingesting NSE:BANKNIFTY from replay", "stream NSE:BANKNIFTY closed after 12", "close code=4404"):
        assert redact(line) == line


def test_formatter_redacts_message_args_and_traceback():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(RedactingFormatter("%(message)s"))
    log = logging.getLogger("test.redact")
    log.addHandler(handler)
    log.propagate = False
    try:
        log.warning("sending to %s", f"https://api.telegram.org/bot{TG}/sendMessage")
        try:
            raise RuntimeError("postgresql://pulse:s3cretPw@timescale/db unreachable")
        except RuntimeError:
            log.exception("db down")
    finally:
        log.removeHandler(handler)
    out = buf.getvalue()
    assert TG not in out and "s3cretPw" not in out and "RuntimeError" in out


def test_configure_logging_quiets_http_client_request_lines():
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        root.handlers = []
        configure_logging("INFO")
        assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
        assert all(isinstance(h.formatter, RedactingFormatter) for h in root.handlers)
    finally:
        root.handlers, _ = saved
        root.setLevel(saved[1])


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(document.cookie)",
        "data:text/html,<script>x</script>",
        "JaVaScRiPt:alert(1)",
        "//no-scheme.example/x",
        "ftp://x.example/a",
    ],
)
def test_news_url_must_be_a_web_link(url):
    now = datetime(2026, 10, 30, tzinfo=UTC)
    with pytest.raises(ValidationError, match="http"):
        RawNewsItem(feed="f", url=url, title="t", published_at=now, fetched_at=now, source=Source.NEWS_FEED)
    ok = RawNewsItem(
        feed="f", url="https://x.example/a", title="t", published_at=now, fetched_at=now, source=Source.NEWS_FEED
    )
    assert ok.url.startswith("https://")
