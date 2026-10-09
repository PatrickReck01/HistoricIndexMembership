"""SEC EDGAR company data: today's ticker/CIK/name list and per-company profiles.

``company_tickers.json`` is EDGAR's mapping from today's tickers to CIKs. A company's
submissions document (``CIK##########.json``), which the ticker lookup reads anyway, also
carries its current name, industry and state of incorporation; ``CompanyProfile`` keeps
those so the output can show them without another request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from historic_index_membership.config import COMPANY_TICKERS_URL
from historic_index_membership.net import CachedHttpClient

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SecCompany:
    """One registrant as listed in ``company_tickers.json``."""

    cik: str
    ticker: str
    title: str


def load_company_tickers(client: CachedHttpClient) -> list[SecCompany]:
    """Load the full ticker/CIK/title mapping from EDGAR.

    Args:
        client: HTTP client configured with an EDGAR contact string.

    Returns:
        One entry per ticker EDGAR knows about. A company with several share classes
        appears once per ticker.
    """
    payload: dict[str, Any] = client.fetch_json(COMPANY_TICKERS_URL)
    companies: list[SecCompany] = []
    for record in payload.values():
        companies.append(
            SecCompany(
                cik=str(record["cik_str"]),
                ticker=str(record["ticker"]).upper(),
                title=str(record["title"]),
            )
        )
    log.info("sec.company_tickers", count=len(companies))
    return companies


@dataclass(frozen=True)
class CompanyProfile:
    """What a company's EDGAR submissions document says about it today.

    Attributes:
        name: Current registrant name.
        sic: Standard Industrial Classification code.
        sic_description: The SIC code's industry name.
        state_of_incorporation: Two-letter state or country code.
        tickers: Tickers EDGAR lists for the company today, if any.
        exchanges: Exchanges those tickers trade on.
    """

    name: str = ""
    sic: str = ""
    sic_description: str = ""
    state_of_incorporation: str = ""
    tickers: tuple[str, ...] = ()
    exchanges: tuple[str, ...] = ()

    @classmethod
    def from_submissions(cls, payload: dict[str, Any]) -> CompanyProfile:
        """Read the profile out of a submissions document.

        Args:
            payload: The parsed ``CIK##########.json``.

        Returns:
            The profile; missing fields stay empty.
        """
        return cls(
            name=str(payload.get("name") or ""),
            sic=str(payload.get("sic") or ""),
            sic_description=str(payload.get("sicDescription") or ""),
            state_of_incorporation=str(payload.get("stateOfIncorporation") or ""),
            tickers=tuple(str(ticker) for ticker in payload.get("tickers") or ()),
            exchanges=tuple(str(item) for item in payload.get("exchanges") or () if item),
        )
