"""Listing a fund's holdings reports on EDGAR."""

from __future__ import annotations

from datetime import date

from fakes import fake_client
from historic_index_membership.filings import (
    list_filings,
    parse_filing_index,
    parse_filing_list,
    parse_submissions,
)
from historic_index_membership.funds import load_index_funds

IJH = load_index_funds()["sp400"][0]


def test_filing_list_and_index_page_are_parsed() -> None:
    feed = """<feed><entry><filing-date>2013-08-29</filing-date>
    <filing-href>https://www.sec.gov/Archives/edgar/data/1100663/x-index.htm</filing-href>
    <filing-type>N-Q</filing-type></entry>
    <entry><filing-date>2013-09-01</filing-date><filing-href>y</filing-href>
    <filing-type>N-Q/A</filing-type></entry></feed>"""
    filings = parse_filing_list(feed, IJH)
    assert [(f.form, f.filing_date) for f in filings] == [("N-Q", date(2013, 8, 29))]

    page = """<div class="infoHead">Period of Report</div>
    <div class="info">2013-06-30</div>
    <td><a href="/Archives/edgar/data/1100663/x/d587788dnq.htm">d587788dnq.htm</a></td>
    <td>N-Q</td>"""
    period, documents = parse_filing_index(page)
    assert period == date(2013, 6, 30)
    assert documents == [
        ("https://www.sec.gov/Archives/edgar/data/1100663/x/d587788dnq.htm", "N-Q")
    ]


def test_a_trusts_reports_are_read_from_its_submissions_document() -> None:
    table = {
        "form": ["N-30D", "497", "NPORT-P/A", "NPORT-P"],
        "filingDate": ["2010-06-28", "2010-06-01", "2020-01-02", "2019-12-20"],
        "accessionNumber": ["0001-10-1", "0001-10-2", "0001-20-3", "0001-19-4"],
    }
    filings = parse_submissions([table], IJH, "1041130")
    assert [(f.form, f.filing_date) for f in filings] == [
        ("N-30D", date(2010, 6, 28)),
        ("NPORT-P", date(2019, 12, 20)),
    ]
    assert filings[0].index_url == (
        "https://www.sec.gov/Archives/edgar/data/1041130/0001101/0001-10-1-index.htm"
    )


def test_a_listing_that_cannot_be_read_is_reported_not_skipped() -> None:
    client = fake_client({"type=NPORT-P": "<feed></feed>"})  # every other form fails
    errors: list[str] = []
    assert list_filings(client, IJH, date(2013, 1, 1), date(2013, 12, 31), errors) == []
    assert len(errors) == 4
    assert all("iShares Core S&P Mid-Cap ETF (IJH)" in error for error in errors)
