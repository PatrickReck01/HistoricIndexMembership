"""Shared test setup."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import structlog

from historic_index_membership import filings
from historic_index_membership.net import RateLimiter


@pytest.fixture(autouse=True)
def reset_logging() -> Iterator[None]:
    """Undo any logging setup a test (e.g. the CLI) made, so later tests log normally.

    Yields:
        Nothing; the reset runs after the test.
    """
    yield
    structlog.reset_defaults()


@pytest.fixture(autouse=True)
def no_browser_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the one-request-per-second pacing of EDGAR's company browser; tests are offline.

    Args:
        monkeypatch: Pytest's monkeypatch.
    """
    monkeypatch.setattr(filings, "_BROWSER_PACE", RateLimiter(requests_per_second=0))


@pytest.fixture(autouse=True)
def no_listing_retry_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry failed filing lists without the half-minute waits a real EDGAR outage needs.

    Args:
        monkeypatch: Pytest's monkeypatch.
    """
    monkeypatch.setattr(filings, "LISTING_RETRY_WAITS", ())
