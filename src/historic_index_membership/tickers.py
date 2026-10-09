"""The ticker a company traded under at the time, from its own filings.

Fund reports carry no tickers (NPORT-P carries CUSIPs, which EDGAR cannot map), and EDGAR's
``company_tickers.json`` only knows today's: of the S&P 400 / 600 members of mid-2013 about
half no longer trade, and some that do trade
under a later symbol (Alliance Data is BFH today, ADS then). Three places are tried, in
order:

1. **XBRL.** Since 2009-2011 every 10-K carries an XBRL instance whose file name EDGAR's
   filer manual asks to start with the ticker -- ``ads-20121231.xml``,
   ``cbst-20121231.xml``; inline XBRL keeps the convention (``vrtx-20201231_htm.xml``). The
   10-Ks filed closest to the company's time in the index are read for that prefix.
2. **Today's ticker** for the same CIK, for a company that filed no XBRL 10-K but still
   trades.
3. **Press releases.** A company acquired before it ever filed XBRL still announced its
   earnings in 8-K exhibits, and a press release names its issuer with an exchange tag:
   "(NASDAQ: SYBS)", or "(NASDAQ - DBRN)". The most frequent
   exchange tag across the press releases closest to the date wins.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from datetime import date
from typing import Any

import structlog

from historic_index_membership.config import FILING_INDEX_URL_TEMPLATE, SUBMISSIONS_URL_TEMPLATE
from historic_index_membership.filings import parse_filing_index
from historic_index_membership.net import CachedHttpClient
from historic_index_membership.sec import CompanyProfile

log = structlog.get_logger(__name__)

ANNUAL_FORMS = frozenset({"10-K", "10-K405", "10-KT", "20-F", "40-F"})
PRESS_RELEASE_FORMS = frozenset({"8-K"})
# XBRL became mandatory for the last filers in June 2011; nothing earlier than the first
# phase is worth probing.
XBRL_START = date(2009, 6, 15)
MAX_ANNUAL_REPORTS_PROBED = 3
# Press releases read, and 8-Ks looked at to find them (many 8-Ks carry no exhibit).
MAX_PRESS_RELEASES_READ = 4
MAX_8KS_PROBED = 12
_INSTANCE = re.compile(r'/([A-Za-z]{1,5})-\d{8}(?:_htm)?\.xml"')
# TickerMatcher's exchange tag ("(NYSE: ABC)"), also accepting a dash as the separator and the
# long Nasdaq market names that 2000s press releases used.
EXCHANGE_TAG = re.compile(
    r"\((?:NYSE|NASDAQ|Nasdaq|NasdaqGS|NasdaqGM|NasdaqCM|AMEX|NYSE Amex|NYSE American)"
    r"(?:\s+Global(?:\s+Select)?\s+Market)?\s*[:\-\u2013]\s*(?P<ticker>[A-Za-z][A-Za-z.]{0,5})\)"
)
_TAG = re.compile(r"<[^>]+>")


def ticker_from_index_page(page: str) -> str | None:
    """Read the ticker out of the XBRL instance file name on a filing index page.

    Args:
        page: A filing's ``...-index.htm`` page.

    Returns:
        The upper-cased prefix of the instance file, or None if the filing has none.
    """
    match = _INSTANCE.search(page)
    return match.group(1).upper() if match else None


def tickers_in_press_release(document: str, exchange_pattern: re.Pattern[str]) -> Counter[str]:
    """Count the exchange tags ("(NYSE: ABC)") in a press release.

    Args:
        document: The exhibit, HTML or plain text.
        exchange_pattern: Regex with a named group ``ticker``.

    Returns:
        Upper-cased tickers with their number of mentions.
    """
    text = html.unescape(_TAG.sub(" ", document))
    text = re.sub(r"\s+", " ", text)
    return Counter(match.group("ticker").upper() for match in exchange_pattern.finditer(text))


def _filings_of(payload: dict[str, Any], forms: frozenset[str]) -> list[tuple[date, str]]:
    """List the filings of some forms in one submissions document or page.

    Args:
        payload: The submissions JSON, or one of its older pages.
        forms: Form types to keep.

    Returns:
        ``(filing date, accession number)`` pairs.
    """
    table = payload.get("filings", {}).get("recent", payload)
    return [
        (date.fromisoformat(str(filed)), str(accession))
        for form, filed, accession in zip(
            table.get("form", []),
            table.get("filingDate", []),
            table.get("accessionNumber", []),
            strict=False,
        )
        if str(form) in forms and filed
    ]


class HistoricalTickers:
    """Looks up the ticker each CIK traded under around a date.

    Args:
        client: EDGAR client.
        ticker_today: CIK -> ticker today.
    """

    def __init__(self, client: CachedHttpClient, ticker_today: dict[str, str]) -> None:
        """Store the client and the fallback tickers.

        Args:
            client: EDGAR client.
            ticker_today: CIK -> ticker today.
        """
        self.client = client
        self.ticker_today = ticker_today
        self._tables: dict[str, list[dict[str, Any]]] = {}

    def _submission_tables(self, cik: str) -> list[dict[str, Any]]:
        """Fetch a CIK's submissions document and all its older pages, once per CIK.

        Args:
            cik: CIK without leading zeros.

        Returns:
            The documents, the main one first; empty if EDGAR has none.
        """
        if cik not in self._tables:
            self._tables[cik] = self._fetch_submission_tables(cik)
        return self._tables[cik]

    def _fetch_submission_tables(self, cik: str) -> list[dict[str, Any]]:
        """Fetch a CIK's submissions document and all its older pages.

        Args:
            cik: CIK without leading zeros.

        Returns:
            The documents, the main one first; empty if EDGAR has none.
        """
        template = SUBMISSIONS_URL_TEMPLATE
        try:
            payload: dict[str, Any] = self.client.fetch_json(template.format(cik=cik.zfill(10)))
        except Exception as error:  # a single missing filer must not stop a build
            log.warning("tickers.submissions_failed", cik=cik, error=str(error))
            return []
        tables = [payload]
        base = template.rsplit("/", 1)[0]
        for page in payload.get("filings", {}).get("files", []):
            try:
                tables.append(self.client.fetch_json(f"{base}/{page['name']}"))
            except Exception as error:  # an unreadable page only narrows the search
                log.warning("tickers.submissions_page_failed", cik=cik, error=str(error))
        return tables

    def _index_page(self, cik: str, accession: str) -> str | None:
        """Fetch a filing's index page.

        Args:
            cik: CIK without leading zeros.
            accession: Accession number with dashes.

        Returns:
            The page, or None if it could not be read.
        """
        url = FILING_INDEX_URL_TEMPLATE.format(
            cik=cik, accession_nodash=accession.replace("-", ""), accession=accession
        )
        try:
            return self.client.fetch(url).text
        except Exception as error:  # try the next filing
            log.warning("tickers.filing_index_failed", url=url, error=str(error))
            return None

    def _from_press_releases(self, cik: str, filings: list[tuple[date, str]]) -> str | None:
        """Read the issuer's ticker from the exchange tags in its 8-K press releases.

        Args:
            cik: CIK without leading zeros.
            filings: 8-Ks, closest to the date of interest first.

        Returns:
            The most frequent ticker, or None if no press release names one.
        """
        counts: Counter[str] = Counter()
        read = 0
        for _, accession in filings[:MAX_8KS_PROBED]:
            page = self._index_page(cik, accession)
            if page is None:
                continue
            _, documents = parse_filing_index(page)
            exhibits = [url for url, kind in documents if kind.startswith("EX-99")]
            for url in exhibits[:1]:
                try:
                    counts += tickers_in_press_release(self.client.fetch(url).text, EXCHANGE_TAG)
                except Exception as error:  # an unreadable exhibit only narrows the search
                    log.warning("tickers.exhibit_failed", url=url, error=str(error))
            read += bool(exhibits)
            if read == MAX_PRESS_RELEASES_READ:
                break
        return counts.most_common(1)[0][0] if counts else None

    def profile(self, cik: str) -> CompanyProfile:
        """Return what EDGAR's submissions document says about a CIK today.

        Reads the document :meth:`lookup` already fetched, so it costs no request.

        Args:
            cik: CIK without leading zeros.

        Returns:
            The profile; empty if EDGAR has no submissions for the CIK.
        """
        tables = self._submission_tables(cik)
        return CompanyProfile.from_submissions(tables[0]) if tables else CompanyProfile()

    def lookup(self, cik: str, around: date) -> tuple[str | None, str]:
        """Find the ticker a CIK traded under around a date.

        Args:
            cik: CIK without leading zeros.
            around: A date inside the company's time in the index.

        Returns:
            ``(ticker, how)`` where ``how`` is ``xbrl``, ``today``, ``press_release`` or
            ``none``.
        """
        tables = self._submission_tables(cik)

        def closest(forms: frozenset[str], since: date = date.min) -> list[tuple[date, str]]:
            found = [filing for table in tables for filing in _filings_of(table, forms)]
            found = [filing for filing in found if filing[0] >= since]
            return sorted(found, key=lambda filing: abs((filing[0] - around).days))

        for _, accession in closest(ANNUAL_FORMS, XBRL_START)[:MAX_ANNUAL_REPORTS_PROBED]:
            page = self._index_page(cik, accession)
            ticker = ticker_from_index_page(page) if page else None
            if ticker:
                return ticker, "xbrl"
        if cik in self.ticker_today:
            return self.ticker_today[cik], "today"
        ticker = self._from_press_releases(cik, closest(PRESS_RELEASE_FORMS))
        if ticker:
            return ticker, "press_release"
        return None, "none"
