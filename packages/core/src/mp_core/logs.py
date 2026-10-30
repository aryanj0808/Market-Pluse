"""Logging setup shared by every process: credentials never reach a log line.

httpx logs every request URL at INFO, and the Telegram bot token is part of its URL; tracebacks can
carry Authorization headers or database URLs. Every formatted record (message and traceback) passes
through `redact`, and the HTTP client loggers are lowered to WARNING.
"""

from __future__ import annotations

import logging
import re

FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"
REDACTED = "<redacted>"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(/bot)\d+:[A-Za-z0-9_-]+"), rf"\1{REDACTED}"),  # Telegram bot token in a URL
    (re.compile(r"\b\d{6,12}:AA[A-Za-z0-9_-]{30,}"), REDACTED),  # bare Telegram bot token
    (re.compile(r"sk-ant-[A-Za-z0-9_-]+"), REDACTED),  # Anthropic API key
    (re.compile(r"(\btoken\s+)[^\s:'\"]+:[^\s'\",}]+", re.I), rf"\1{REDACTED}"),  # Kite "token key:access"
    (re.compile(r"(\bBearer\s+)[A-Za-z0-9._~+/=-]+", re.I), rf"\1{REDACTED}"),  # Upstox / OAuth
    (
        re.compile(r"([?&](?:request_token|access_token|api_secret|checksum|code|token)=)[^&\s'\"]+", re.I),
        rf"\1{REDACTED}",
    ),
    (re.compile(r"(://[^:/@\s]+:)[^@\s]+@"), rf"\1{REDACTED}@"),  # password in postgresql:// or redis://
]


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging(level: str = "INFO", fmt: str = FORMAT) -> None:
    logging.basicConfig(level=level, format=fmt)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(RedactingFormatter(fmt))
    for name in ("httpx", "httpcore"):  # request lines carry URLs (and the Telegram token)
        logging.getLogger(name).setLevel(logging.WARNING)
