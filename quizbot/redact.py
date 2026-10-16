"""Secret redaction for every log record and error message."""

from __future__ import annotations

import logging
import os
import re

_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),                 # Anthropic API / OAuth tokens
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),              # GitHub tokens
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?i)(authorization:\s*(?:bearer|token|basic)\s+)[^\s'\"]+"),
    re.compile(r"(?i)(x-api-key:\s*)[^\s'\"]+"),
    re.compile(r"(https?://)[^/@\s:]+:[^/@\s]+@"),            # credentials embedded in URLs
]
_SECRET_ENV = ("GITHUB_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "GH_TOKEN")


def redact(text: str) -> str:
    for name in _SECRET_ENV:
        val = os.environ.get(name)
        if val and len(val) >= 8:
            text = text.replace(val, "[REDACTED]")
    for pat in _PATTERNS:
        text = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "[REDACTED]", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def install() -> None:
    f = RedactingFilter()
    root = logging.getLogger()
    for h in root.handlers:
        if not any(isinstance(x, RedactingFilter) for x in h.filters):
            h.addFilter(f)
