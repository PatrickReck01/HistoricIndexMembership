"""Listing a fund's holdings reports on EDGAR, with their report dates and documents.

Reports read:

* N-Q (2004-2019): schedule of investments for fiscal Q1 and Q3, HTML or plain text;
* N-CSR / N-CSRS: annual / semi-annual report, including the schedule for Q4 and Q2;
* N-30D / N-30B-2: annual / semi-annual report of a unit investment trust (SPY, DIA, QQQ);
* NPORT-P (2019-): structured XML holdings, public for each fiscal quarter end.

A fund's filings are listed from EDGAR's company browser, which accepts a series id as the
filer. A trust's filings (a unit investment trust, or any trust before series ids existed)
are listed from its submissions document on ``data.sec.gov`` instead: one request covers
a thousand filings, where the browser needs ten pages, and the browser is the endpoint EDGAR
throttles first. Filing lists grow as new reports are filed, so they are always fetched
fresh; filing index pages and documents never change once filed and are served from the
cache.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import structlog

from historic_index_membership.config import SUBMISSIONS_URL_TEMPLATE
from historic_index_membership.funds import FundSource
from historic_index_membership.net import CachedHttpClient, RateLimiter

log = structlog.get_logger(__name__)

HOLDINGS_FORMS = ("N-Q", "N-CSR", "N-CSRS", "N-30D", "N-30B-2", "NPORT-P")
# The browser matches form types by prefix, so "N-CSR" also returns N-CSRS.
_SEARCHED_FORMS = ("N-Q", "N-CSR", "N-30D", "N-30B-2", "NPORT-P")
PAGE_SIZE = 100
# The company browser is throttled much sooner than the rest of EDGAR: at the general
# 8-10 requests per second it starts answering 503 within minutes. It is needed only a
# handful of times per fund, so it gets its own, slower pace.
_BROWSER_PACE = RateLimiter(requests_per_second=1.0)
# EDGAR's listings have short spells of answering 503 to everything. Seconds to wait before
# each extra attempt, on top of the HTTP client's own quick retries.
LISTING_RETRY_WAITS = (30, 60)
BROWSE_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={filer}&type={form}"
    "&dateb={before:%Y%m%d}&owner=include&start={start}&count={count}&output=atom"
)
ARCHIVES = "https://www.sec.gov"
INDEX_URL = ARCHIVES + "/Archives/edgar/data/{cik}/{accession_nodash}/{accession}-index.htm"
# EDGAR has tagged every fund filing with its series id only since 2006. Earlier filings are
# listed under the trust, whose filings cover several funds each.
SERIES_IDS_ON_ALL_FILINGS = date(2006, 7, 1)

_ENTRY = re.compile(r"<entry>(.*?)</entry>", re.S)
_PERIOD = re.compile(r'Period of Report</div>\s*<div class="info">(\d{4}-\d{2}-\d{2})<')
_DOCUMENT = re.compile(
    r'<a href="(/Archives/edgar/data/[^"]+)">[^<]*</a></td>\s*<td[^>]*>([^<]*)</td>'
)


@dataclass
class Filing:
    """One holdings report of a fund.

    Attributes:
        fund: The fund the report belongs to.
        form: EDGAR form type, one of ``HOLDINGS_FORMS``.
        filing_date: Date the report was filed.
        index_url: URL of the filing's EDGAR index page.
        period: Report date of the holdings, read from the index page.
        documents: ``(url, document type)`` of every document in the filing.
    """

    fund: FundSource
    form: str
    filing_date: date
    index_url: str
    period: date | None = None
    documents: list[tuple[str, str]] = field(default_factory=list)


def _tag(entry: str, name: str) -> str:
    """Read one tag's text out of an Atom entry.

    Args:
        entry: The ``<entry>`` body.
        name: Tag name, e.g. ``filing-type``.

    Returns:
        The tag's text, or an empty string if it is missing.
    """
    match = re.search(rf"<{name}>(.*?)</{name}>", entry, re.S)
    return match.group(1).strip() if match else ""


def parse_filing_list(feed: str, fund: FundSource) -> list[Filing]:
    """Read the holdings reports out of an EDGAR company-browser Atom feed.

    Args:
        feed: The Atom feed.
        fund: The fund the feed was requested for.

    Returns:
        Filings of the forms in ``HOLDINGS_FORMS``; amendments are skipped.
    """
    filings = []
    for entry in _ENTRY.findall(feed):
        form = _tag(entry, "filing-type")
        filing_date = _tag(entry, "filing-date")
        index_url = _tag(entry, "filing-href")
        if form in HOLDINGS_FORMS and filing_date and index_url:
            filings.append(Filing(fund, form, date.fromisoformat(filing_date), index_url))
    return filings


def parse_submissions(
    tables: list[dict[str, Any]], fund: FundSource, cik: str
) -> list[Filing]:
    """Read the holdings reports out of a trust's submissions document and its older pages.

    Args:
        tables: The ``filings.recent`` table and the older pages, each with parallel
            ``form`` / ``filingDate`` / ``accessionNumber`` lists.
        fund: The fund the reports are wanted for.
        cik: The trust's CIK.

    Returns:
        Filings of the forms in ``HOLDINGS_FORMS``; amendments are skipped.
    """
    filings = []
    for table in tables:
        for form, filed, accession in zip(
            table.get("form", []),
            table.get("filingDate", []),
            table.get("accessionNumber", []),
            strict=False,
        ):
            if form in HOLDINGS_FORMS and filed and accession:
                url = INDEX_URL.format(
                    cik=cik, accession_nodash=accession.replace("-", ""), accession=accession
                )
                filings.append(Filing(fund, form, date.fromisoformat(filed), url))
    return filings


def _fetch_listing(client: CachedHttpClient, url: str) -> str:
    """Fetch a filing list fresh, waiting out a spell of EDGAR errors if needed.

    Args:
        client: EDGAR client.
        url: The listing URL.

    Returns:
        The response body.

    Raises:
        Exception: The last error, if every attempt failed.
    """
    for wait in (0, *LISTING_RETRY_WAITS):
        time.sleep(wait)
        try:
            return client.fetch(url, force=True).text
        except Exception as error:
            last_error = error
            log.warning("filings.listing_retry", url=url, error=str(error))
    raise last_error


def _list_by_trust(
    client: CachedHttpClient,
    fund: FundSource,
    filed_after: date,
    filed_before: date,
    errors: list[str],
) -> list[Filing]:
    """List the holdings reports a fund's trust filed, from its submissions document.

    Args:
        client: EDGAR client.
        fund: The fund; ``registrant_cik`` must be set.
        filed_after: Earliest filing date to include.
        filed_before: Latest filing date to include.
        errors: A message is appended here if the list cannot be read.

    Returns:
        The trust's holdings reports in the range, for every fund it runs.
    """
    cik = str(fund.registrant_cik)
    url = SUBMISSIONS_URL_TEMPLATE.format(cik=cik.zfill(10))
    try:
        payload: dict[str, Any] = json.loads(_fetch_listing(client, url))
        tables = [payload.get("filings", {}).get("recent", {})]
        base = url.rsplit("/", 1)[0]
        for page in payload.get("filings", {}).get("files", []):
            # Older pages never change; only those overlapping the range are read.
            if page.get("filingTo", "9999") < filed_after.isoformat():
                continue
            if page.get("filingFrom", "0000") > filed_before.isoformat():
                continue
            tables.append(client.fetch_json(f"{base}/{page['name']}"))
    except Exception as error:  # one failed listing must not stop a build
        log.warning("filings.listing_failed", url=url, error=str(error))
        errors.append(f"could not list the filings of {fund.label} ({url}): {error}")
        return []
    return [
        filing
        for filing in parse_submissions(tables, fund, cik)
        if filed_after <= filing.filing_date <= filed_before
    ]


def _list_by_series(
    client: CachedHttpClient,
    fund: FundSource,
    filed_after: date,
    filed_before: date,
    errors: list[str],
) -> list[Filing]:
    """List a fund's holdings reports from EDGAR's company browser, by series id.

    Args:
        client: EDGAR client.
        fund: The fund; ``series_id`` must be set.
        filed_after: Earliest filing date to include.
        filed_before: Latest filing date to include.
        errors: A message is appended for each list that cannot be read.

    Returns:
        The fund's holdings reports in the range.
    """
    filings = []
    for form in _SEARCHED_FORMS:
        start = 0
        while True:
            url = BROWSE_URL.format(
                filer=fund.series_id, form=form, before=filed_before, start=start, count=PAGE_SIZE
            )
            _BROWSER_PACE.acquire()
            try:
                feed = _fetch_listing(client, url)
            except Exception as error:  # one failed listing must not stop a build
                log.warning("filings.listing_failed", url=url, error=str(error))
                errors.append(f"could not list the {form} filings of {fund.label}: {error}")
                break
            page = parse_filing_list(feed, fund)
            filings += [filing for filing in page if filing.filing_date >= filed_after]
            # Pages run newest first: stop at the last page or once past the range.
            full_page = len(_ENTRY.findall(feed)) == PAGE_SIZE
            if not full_page or (page and page[-1].filing_date < filed_after):
                break
            start += PAGE_SIZE
    return filings


def list_filings(
    client: CachedHttpClient,
    fund: FundSource,
    filed_after: date,
    filed_before: date,
    errors: list[str] | None = None,
) -> list[Filing]:
    """List a fund's holdings reports filed within a date range, newest first.

    A fund with a series id is listed by it. Its trust's own list covers every fund the
    trust runs, so it is only read for a unit investment trust (no series id) or for the
    years before series ids were tagged.

    Args:
        client: EDGAR client.
        fund: The fund.
        filed_after: Earliest filing date to include.
        filed_before: Latest filing date to include.
        errors: Messages about lists that could not be read are appended here; a build
            reports them as warnings, since a missing list can mean missing quarters.

    Returns:
        The filings, deduplicated by index page, report dates not yet read.
    """
    errors = errors if errors is not None else []
    found: list[Filing] = []
    if fund.series_id is None:
        found += _list_by_trust(client, fund, filed_after, filed_before, errors)
    else:
        found += _list_by_series(client, fund, filed_after, filed_before, errors)
        if fund.registrant_cik and filed_after < SERIES_IDS_ON_ALL_FILINGS:
            last_untagged = min(filed_before, SERIES_IDS_ON_ALL_FILINGS - timedelta(days=1))
            found += _list_by_trust(client, fund, filed_after, last_untagged, errors)
    filings: dict[str, Filing] = {}
    for filing in found:
        filings.setdefault(filing.index_url, filing)
    return sorted(filings.values(), key=lambda filing: filing.filing_date, reverse=True)


def parse_filing_index(page: str) -> tuple[date | None, list[tuple[str, str]]]:
    """Read the report date and the documents from a filing index page.

    Args:
        page: The ``...-index.htm`` page.

    Returns:
        ``(period of report, [(document url, document type), ...])``.
    """
    period = _PERIOD.search(page)
    documents = [(ARCHIVES + href, kind.strip()) for href, kind in _DOCUMENT.findall(page)]
    return (date.fromisoformat(period.group(1)) if period else None), documents


def read_filing_index(client: CachedHttpClient, filing: Filing) -> None:
    """Fill in a filing's report date and document list from its index page.

    Args:
        client: EDGAR client.
        filing: The filing; ``period`` and ``documents`` are set in place.
    """
    try:
        page = client.fetch(filing.index_url).text
    except Exception as error:  # an unreadable filing is skipped, not fatal
        log.warning("filings.index_failed", url=filing.index_url, error=str(error))
        return
    filing.period, filing.documents = parse_filing_index(page)
