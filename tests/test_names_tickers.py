"""Matching fund-report names to CIKs, and finding the ticker a company had at the time."""

from __future__ import annotations

from datetime import date
from typing import Any

from fakes import fake_client
from historic_index_membership.names import CikMatcher, name_keys
from historic_index_membership.tickers import (
    EXCHANGE_TAG,
    HistoricalTickers,
    ticker_from_index_page,
    tickers_in_press_release,
)


def test_name_keys_see_through_edgar_spelling() -> None:
    assert name_keys("Warnaco Group Inc. (The)")[0] == name_keys("WARNACO GROUP INC /DE/")[0]
    curly_apostrophe = chr(0x2019)
    assert name_keys(f"Aaron{curly_apostrophe}s")[0] == name_keys("AARON'S INC")[0]
    assert name_keys("VERTEX PHARMACEUTICALS INC / MA")[0] == "VERTEX PHARMACEUTICALS"
    # EDGAR inverts names and spaces out initials; only the loose key sees through that.
    assert (
        name_keys("J.B. Hunt Transport Services Inc.")[1]
        == name_keys("HUNT J B TRANSPORT SERVICES INC")[1]
    )
    assert name_keys("Korn/Ferry International")[1] == name_keys("KORN FERRY /DE/")[1]


def _submissions(first: str, last: str) -> dict[str, Any]:
    """Build a submissions document with one 10-Q per quarter between two dates.

    Args:
        first: Earliest filing date.
        last: Latest filing date.

    Returns:
        The JSON payload.
    """
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    dates = [
        date(year, month, 15).isoformat()
        for year in range(start.year, end.year + 1)
        for month in (2, 5, 8, 11)
        if start <= date(year, month, 15) <= end
    ]
    return {
        "filings": {"recent": {"form": ["10-Q"] * len(dates), "filingDate": dates}, "files": []}
    }


def test_a_shared_name_resolves_to_the_cik_filing_at_the_time() -> None:
    client = fake_client(
        {
            "CIK0000001001.json": _submissions("1999-01-05", "2015-06-30"),  # old Energizer
            "CIK0000001002.json": _submissions("2015-06-01", "2026-08-01"),  # the spin-off
        }
    )
    matcher = CikMatcher.from_names(
        [("ENERGIZER HOLDINGS INC", "1001"), ("Energizer Holdings, Inc.", "1002")],
        ticker_today={"1002": "ENR"},
        client=client
    )
    assert matcher.match("Energizer Holdings Inc.", date(2013, 6, 30)).cik == "1001"
    assert matcher.match("Energizer Holdings Inc.", date(2020, 6, 30)).cik == "1002"
    assert matcher.match("Nobody Corp.", date(2013, 6, 30)).quality == "none"


INDEX_PAGE_2013 = (
    '<a href="/Archives/edgar/data/1101215/000110121513000009/ads-20121231.xml">'
    "ads-20121231.xml</a></td><td>EX-101.INS</td>"
    '<a href="/Archives/edgar/data/1101215/000110121513000009/ads-20121231_cal.xml">'
)
INDEX_PAGE_INLINE = '<a href="/Archives/edgar/data/875320/x/vrtx-20201231_htm.xml">'


def test_ticker_is_read_from_the_xbrl_instance_file_name() -> None:
    assert ticker_from_index_page(INDEX_PAGE_2013) == "ADS"
    assert ticker_from_index_page(INDEX_PAGE_INLINE) == "VRTX"
    assert ticker_from_index_page("<a href='/Archives/x/d123.htm'>") is None


def test_historical_ticker_prefers_the_10k_of_the_time_over_todays_ticker() -> None:
    payload = {
        "filings": {
            "recent": {
                "form": ["10-K", "8-K", "10-K"],
                "filingDate": ["2024-02-20", "2013-05-01", "2013-02-27"],
                "accessionNumber": ["0001101215-24-000001", "x", "0001101215-13-000009"],
            },
            "files": [],
        }
    }
    client = fake_client(
        {
            "CIK0001101215.json": payload,
            "000110121513000009": INDEX_PAGE_2013,
            "000110121524000001": '<a href="/Archives/x/bfh-20231231_htm.xml">',
        }
    )
    tickers = HistoricalTickers(client, {"1101215": "BFH", "42": "NOW"})
    assert tickers.lookup("1101215", date(2013, 6, 30)) == ("ADS", "xbrl")
    assert tickers.lookup("1101215", date(2024, 6, 30)) == ("BFH", "xbrl")
    # No annual report on file at all: today's ticker is the fallback, then nothing.
    empty = fake_client({"CIK00000000": {"filings": {"recent": {}, "files": []}}})
    fallback = HistoricalTickers(empty, {"42": "NOW"})
    assert fallback.lookup("42", date(2013, 6, 30)) == ("NOW", "today")
    assert fallback.lookup("43", date(2013, 6, 30)) == (None, "none")


def test_press_releases_name_their_issuers_ticker() -> None:
    release = (
        "<p>NEW YORK &ndash; March 1, 2010 - Dress Barn, Inc. (NASDAQ - DBRN) today reported "
        "sales. Dress Barn, Inc. (NASDAQ - DBRN), is a leading retailer. It competes with "
        "Chico's FAS (NYSE: CHS).</p>"
    )
    assert tickers_in_press_release(release, EXCHANGE_TAG).most_common(1) == [("DBRN", 2)]
    assert tickers_in_press_release("(Nasdaq Global Select Market: SYBS)", EXCHANGE_TAG) == {
        "SYBS": 1
    }


def test_a_lone_namesake_that_never_filed_as_a_listed_company_is_passed_over() -> None:
    # The fund writes "du Pont", EDGAR "DUPONT": only the compact key finds the real company.
    # The loose key's single candidate filed nothing but a no-action letter.
    client = fake_client(
        {
            "CIK0000030554.json": {"name": "EIDP, INC.", **_submissions("2005-01-01", "2026-08-01")},
            "CIK0001137191.json": {"name": "EI DU PONT DE NEMOURS & CO", "filings": {"recent": {}, "files": []}},
        }
    )
    matcher = CikMatcher.from_names(
        [("DUPONT E I DE NEMOURS & CO", "30554"), ("EI DU PONT DE NEMOURS & CO", "1137191")],
        ticker_today={},
        client=client,
    )
    match = matcher.match("E. I. du Pont de Nemours & Co.", date(2012, 6, 30))
    assert (match.cik, match.quality) == ("30554", "compact")


def test_of_two_active_namesakes_the_one_still_called_that_wins() -> None:
    client = fake_client(
        {
            "CIK0000086312.json": {"name": "TRAVELERS COMPANIES, INC.", **_submissions("2000-01-01", "2026-08-01")},
            "CIK0000831001.json": {"name": "CITIGROUP INC", **_submissions("1990-01-01", "2026-08-01")},
        }
    )
    matcher = CikMatcher.from_names(
        [("TRAVELERS COMPANIES, INC.", "86312"), ("TRAVELERS INC", "831001")],
        ticker_today={"86312": "TRV", "831001": "C"},
        client=client,
    )
    assert matcher.match("The Travelers Cos., Inc.", date(2012, 6, 30)).cik == "86312"
