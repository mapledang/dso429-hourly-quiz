"""HTTP GET with timeouts, status validation and bounded exponential backoff with jitter."""

from __future__ import annotations

import logging
import random
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class FetchError(Exception):
    pass


@dataclass
class FetchResult:
    url: str
    status: int
    body: bytes
    headers: dict


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:  # python.org macOS builds ship without a CA bundle; fall back to the system one
        if not ctx.get_ca_certs():
            ctx.load_verify_locations("/etc/ssl/cert.pem")
    except (OSError, ssl.SSLError):
        pass
    return ctx


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    """Full-jitter exponential backoff."""
    return random.uniform(0, min(cap, base * 2 ** attempt))


def get(url: str, *, headers: dict | None = None, timeout: float = 20.0, attempts: int = 5,
        sleep=time.sleep, opener=None) -> FetchResult:
    opener = opener or (lambda req, t: urllib.request.urlopen(req, timeout=t, context=_ssl_context()))
    last_error = "unknown"
    for attempt in range(attempts):
        req = urllib.request.Request(url, headers={"User-Agent": "dso429-quizbot", **(headers or {})})
        try:
            with opener(req, timeout) as resp:
                body = resp.read()
                status = getattr(resp, "status", 200)
                return FetchResult(url, status, body, dict(resp.headers.items()))
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            if exc.code not in RETRYABLE_STATUS:
                raise FetchError(f"{url}: {last_error} (not retryable)") from exc
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            delay = min(float(retry_after), 60.0) if retry_after and retry_after.isdigit() else backoff_delay(attempt)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last_error = type(exc).__name__ + (f": {exc.reason}" if hasattr(exc, "reason") else "")
            delay = backoff_delay(attempt)
        if attempt < attempts - 1:
            log.warning("fetch %s failed (%s); retry %d/%d in %.1fs", url, last_error, attempt + 1, attempts - 1, delay)
            sleep(delay)
    raise FetchError(f"{url}: gave up after {attempts} attempts ({last_error})")
