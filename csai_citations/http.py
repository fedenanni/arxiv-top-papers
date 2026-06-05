"""Shared HTTP session with retry/backoff and polite rate limiting.

All outbound calls go through this module so rate limiting and 429 handling
live in one place. arXiv gets a fixed inter-request delay; the JSON APIs back
off exponentially and respect `Retry-After`.
"""

from __future__ import annotations

import time
from typing import Any

import requests

USER_AGENT = "csai-citations/0.1 (mailto:none@example.com)"

# Max attempts for a single request before giving up. Set high enough to ride
# out arXiv's intermittent 429 bursts (backoff caps at BACKOFF_MAX per attempt).
MAX_RETRIES = 8
# Base for exponential backoff, in seconds: 1, 2, 4, 8, ...
BACKOFF_BASE = 1.0
# Cap on any single backoff sleep.
BACKOFF_MAX = 60.0


class HTTPError(Exception):
    """Raised when a request fails after exhausting retries."""


def make_session(headers: dict[str, str] | None = None) -> requests.Session:
    """Build a session preconfigured with our User-Agent."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    if headers:
        session.headers.update(headers)
    return session


def _retry_after_seconds(response: requests.Response) -> float | None:
    """Parse a `Retry-After` header (delta-seconds form only)."""
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    timeout: float = 120.0,
    **kwargs: Any,
) -> requests.Response:
    """Perform a request with retry/backoff on 429, 5xx, and network errors.

    Honors `Retry-After` on 429. Raises HTTPError after MAX_RETRIES attempts or
    on a non-retryable 4xx response.
    """
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        backoff = min(BACKOFF_BASE * (2**attempt), BACKOFF_MAX)
        try:
            response = session.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(backoff)
            continue

        if response.status_code == 429:
            wait = _retry_after_seconds(response) or backoff
            time.sleep(wait)
            last_exc = HTTPError(f"429 Too Many Requests for {url}")
            continue
        if 500 <= response.status_code < 600:
            last_exc = HTTPError(f"{response.status_code} server error for {url}")
            time.sleep(backoff)
            continue
        if not response.ok:
            raise HTTPError(
                f"{response.status_code} {response.reason} for {url}: "
                f"{response.text[:200]}"
            )
        return response

    raise HTTPError(f"Request to {url} failed after {MAX_RETRIES} attempts") from last_exc
