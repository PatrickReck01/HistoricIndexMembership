"""Shared HTTP layer: rate limiting, retries and an on-disk response cache.

Every network call goes through this module, and EDGAR's rules set its defaults: a
``User-Agent`` carrying a real name and email, at most 10 requests per second, and
exponential backoff on failure. Responses are cached to disk keyed by URL, so a rebuild
only fetches what changed (filing lists) or is new.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from historic_index_membership.config import Settings

log = structlog.get_logger(__name__)

RETRYABLE_STATUS = frozenset({403, 429, 500, 502, 503, 504})


class RetryableStatusError(Exception):
    """Raised for HTTP statuses that are worth retrying."""


@dataclass(frozen=True)
class FetchResult:
    """One fetched (or cached) response body plus its provenance."""

    url: str
    text: str
    fetched_at: datetime
    from_cache: bool


class RateLimiter:
    """A token bucket that spaces requests to a fixed maximum rate."""

    def __init__(self, requests_per_second: float) -> None:
        """Initialise the limiter.

        Args:
            requests_per_second: Maximum sustained request rate.
        """
        self._min_interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        """Block until the next request is permitted."""
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self._min_interval


class CachedHttpClient:
    """An HTTP client with a disk cache, a rate limiter and retries."""

    def __init__(
        self,
        cache_dir: Path,
        user_agent: str,
        requests_per_second: float = 10.0,
        max_retries: int = 5,
        timeout: float = 30.0,
    ) -> None:
        """Initialise the client.

        Args:
            cache_dir: Directory holding cached response bodies.
            user_agent: Contact string sent with every request.
            requests_per_second: Maximum sustained request rate.
            max_retries: Attempts before giving up on a URL.
            timeout: Per-request timeout in seconds.
        """
        self.cache_dir = cache_dir
        self.user_agent = user_agent
        self.max_retries = max_retries
        self._limiter = RateLimiter(requests_per_second)
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._client.close()

    def __enter__(self) -> CachedHttpClient:
        """Enter the context manager.

        Returns:
            This client.
        """
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the client on context exit."""
        self.close()

    def cache_paths(self, url: str) -> tuple[Path, Path]:
        """Return the body and metadata paths for a URL.

        Args:
            url: The URL being cached.

        Returns:
            A ``(body_path, meta_path)`` pair.
        """
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        bucket = self.cache_dir / digest[:2]
        return bucket / f"{digest}.body", bucket / f"{digest}.meta.json"

    def fetch(self, url: str, force: bool = False) -> FetchResult:
        """Fetch a URL, using the disk cache when possible.

        Args:
            url: The URL to fetch.
            force: Bypass the cache and refetch.

        Returns:
            The response body with its provenance.
        """
        body_path, meta_path = self.cache_paths(url)
        if not force and body_path.exists() and meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return FetchResult(
                url=url,
                text=body_path.read_text(encoding="utf-8"),
                fetched_at=datetime.fromisoformat(meta["fetched_at"]),
                from_cache=True,
            )

        text = self._get_with_retry(url)
        fetched_at = datetime.now(UTC)
        body_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_text(text, encoding="utf-8")
        meta_path.write_text(
            json.dumps({"url": url, "fetched_at": fetched_at.isoformat()}, indent=2),
            encoding="utf-8",
        )
        return FetchResult(url=url, text=text, fetched_at=fetched_at, from_cache=False)

    def fetch_json(self, url: str, force: bool = False) -> Any:
        """Fetch a URL and parse it as JSON.

        Args:
            url: The URL to fetch.
            force: Bypass the cache and refetch.

        Returns:
            The parsed JSON payload.
        """
        return json.loads(self.fetch(url, force=force).text)

    def _get_with_retry(self, url: str) -> str:
        """Perform the actual request, retrying transient failures.

        Args:
            url: The URL to fetch.

        Returns:
            The response body as text.
        """

        @retry(
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=30),
            retry=retry_if_exception_type((RetryableStatusError, httpx.TransportError)),
            reraise=True,
        )
        def _attempt() -> str:
            self._limiter.acquire()
            response = self._client.get(url)
            if response.status_code in RETRYABLE_STATUS:
                log.warning("http.retryable", url=url, status=response.status_code)
                raise RetryableStatusError(f"{response.status_code} for {url}")
            response.raise_for_status()
            return response.text

        log.debug("http.fetch", url=url)
        return _attempt()


def edgar_client(settings: Settings) -> CachedHttpClient:
    """Build a client configured for EDGAR.

    Args:
        settings: Contact string, cache directory and rate limits.

    Returns:
        A caching, rate-limited client whose User-Agent satisfies EDGAR's contact rule.
    """
    return CachedHttpClient(
        cache_dir=settings.cache_dir,
        user_agent=settings.user_agent,
        requests_per_second=settings.requests_per_second,
        max_retries=settings.max_retries,
    )
