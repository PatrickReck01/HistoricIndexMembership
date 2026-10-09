"""A fake EDGAR client, so tests run offline against canned pages."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

from historic_index_membership.net import CachedHttpClient, FetchResult


class FakeClient:
    """Answers ``fetch`` / ``fetch_json`` from canned bodies keyed by a URL substring."""

    def __init__(self, responses: dict[str, Any]) -> None:
        """Store the canned responses.

        Args:
            responses: URL substring -> body (str) or JSON payload (anything else). The
                first key found in a URL wins, so list specific keys before general ones.
        """
        self.responses = responses

    def _lookup(self, url: str) -> Any:
        """Return the first canned response whose key occurs in the URL.

        Args:
            url: The requested URL.

        Returns:
            The canned response.

        Raises:
            RuntimeError: If no key matches, as a real 404 would.
        """
        for key, response in self.responses.items():
            if key in url:
                return response
        raise RuntimeError(f"no canned response for {url}")

    def fetch(self, url: str, force: bool = False) -> FetchResult:
        """Return a canned body.

        Args:
            url: The requested URL.
            force: Ignored.

        Returns:
            The body wrapped like a real fetch.
        """
        body = self._lookup(url)
        text = body if isinstance(body, str) else json.dumps(body)
        return FetchResult(url=url, text=text, fetched_at=datetime.now(UTC), from_cache=True)

    def fetch_json(self, url: str, force: bool = False) -> Any:
        """Return a canned JSON payload.

        Args:
            url: The requested URL.
            force: Ignored.

        Returns:
            The payload.
        """
        return self._lookup(url)


def fake_client(responses: dict[str, Any]) -> CachedHttpClient:
    """Wrap canned responses as an EDGAR client.

    Args:
        responses: URL substring -> body or payload.

    Returns:
        The fake, typed as the real client.
    """
    return cast(CachedHttpClient, FakeClient(responses))
