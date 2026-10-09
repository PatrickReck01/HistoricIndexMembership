"""Settings: the SEC contact string, the cache location and EDGAR's access limits.

EDGAR rejects requests that do not name who is making them, so every run needs a
User-Agent with a name and an email address. It is looked up in this order:

1. the ``user_agent`` argument (CLI: ``--user-agent``);
2. the ``SEC_USER_AGENT`` environment variable;
3. ``SEC_USER_AGENT`` in a ``.env`` file in the working directory or one of its parents.

The cache directory follows the same order with ``cache_dir`` / ``--cache-dir`` /
``INDEX_MEMBERSHIP_CACHE_DIR``, and defaults to ``cache`` in the working directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

USER_AGENT_VARIABLE = "SEC_USER_AGENT"
CACHE_DIR_VARIABLE = "INDEX_MEMBERSHIP_CACHE_DIR"
DEFAULT_CACHE_DIR = Path("cache")

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL_TEMPLATE = "https://data.sec.gov/submissions/CIK{cik}.json"
# The filing index page is the only EDGAR document that names each document's type.
FILING_INDEX_URL_TEMPLATE = (
    "https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/{accession}-index.htm"
)
# EDGAR's fair-access policy allows at most 10 requests per second. Running right at the
# limit still draws occasional 503s, so a little headroom is kept.
REQUESTS_PER_SECOND = 8.0
MAX_RETRIES = 5


@dataclass(frozen=True)
class Settings:
    """Everything a run needs to talk to EDGAR.

    Attributes:
        user_agent: Contact string sent with every request, e.g. ``"Jane Doe jane@example.com"``.
        cache_dir: Directory holding cached EDGAR responses.
        requests_per_second: Maximum sustained request rate.
        max_retries: Attempts per URL before giving up.
    """

    user_agent: str
    cache_dir: Path = DEFAULT_CACHE_DIR
    requests_per_second: float = REQUESTS_PER_SECOND
    max_retries: int = MAX_RETRIES


def load_settings(user_agent: str | None = None, cache_dir: str | Path | None = None) -> Settings:
    """Resolve the settings from arguments, environment variables and a ``.env`` file.

    Args:
        user_agent: SEC contact string; read from ``SEC_USER_AGENT`` when omitted.
        cache_dir: Cache directory; read from ``INDEX_MEMBERSHIP_CACHE_DIR`` when omitted,
            else ``cache`` in the working directory.

    Returns:
        The settings.

    Raises:
        RuntimeError: If no usable SEC contact string is found.
    """
    # Values already in the environment win over the .env file.
    load_dotenv(find_dotenv(usecwd=True))
    user_agent = user_agent or os.environ.get(USER_AGENT_VARIABLE)
    if not user_agent or "@" not in user_agent:
        raise RuntimeError(
            "The SEC requires a User-Agent with your name and email address. Pass "
            '--user-agent "Your Name you@example.com" (CLI), user_agent=... (Python), or '
            f"set {USER_AGENT_VARIABLE} in the environment or in a .env file."
        )
    resolved_cache = cache_dir or os.environ.get(CACHE_DIR_VARIABLE) or DEFAULT_CACHE_DIR
    return Settings(user_agent=user_agent.strip(), cache_dir=Path(resolved_cache))
