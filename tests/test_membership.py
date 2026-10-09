"""Turning quarterly snapshots into membership spans, end to end against a fake EDGAR."""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import pytest

from fakes import fake_client
from historic_index_membership.funds import FundSource
from historic_index_membership.membership import (
    MembershipBuilder,
    clean_name,
    extend_spans,
    intervals_from_snapshots,
    quarter_ends,
)


def test_quarters_start_at_the_last_quarter_end_before_the_window() -> None:
    assert quarter_ends(date(2010, 1, 1), date(2010, 7, 1)) == [
        date(2009, 12, 31),
        date(2010, 3, 31),
        date(2010, 6, 30),
    ]
    assert quarter_ends(date(2010, 3, 31), date(2010, 3, 31)) == [date(2010, 3, 31)]


def test_snapshots_are_diffed_into_spans_and_a_returning_member_gets_two() -> None:
    q1, q2, q3, q4 = date(2013, 3, 31), date(2013, 6, 30), date(2013, 9, 30), date(2013, 12, 31)
    snapshots = [(q1, {"1", "2"}), (q2, {"1"}), (q3, {"1", "2"}), (q4, {"2"})]
    assert intervals_from_snapshots(snapshots) == [
        ("1", q1, q3),
        ("2", q1, q1),
        ("2", q3, None),
    ]


def test_spans_are_stretched_to_the_day_before_the_next_snapshot() -> None:
    dates = [date(2013, 3, 31), date(2013, 6, 30), date(2013, 9, 30)]
    intervals = [("1", date(2013, 3, 31), date(2013, 6, 30)), ("2", date(2013, 3, 31), None)]
    assert extend_spans(intervals, dates) == [
        ("1", date(2013, 3, 31), date(2013, 9, 29)),
        ("2", date(2013, 3, 31), None),
    ]


def test_share_class_suffixes_are_not_part_of_the_name() -> None:
    assert clean_name("Under Armour, Cl. A") == "Under Armour"
    assert clean_name("Lamar Advertising Co. Class A") == "Lamar Advertising Co."
    assert clean_name("Eaton Vance Corp. NVS") == "Eaton Vance Corp."
    assert clean_name("Class Act Inc.") == "Class Act Inc."


TEST_FUND = FundSource(
    index_id="test",
    index_name="Test Index",
    priority=1,
    fund_ticker="TST",
    fund_name="Test Index Fund",
    series_id="S000000001",
    registrant_cik=None,
    heading_pattern="TEST INDEX FUND",
    values_in_thousands=False,
    min_holdings=1,
    max_holdings=10,
)


def _feed(*entries: tuple[str, str]) -> str:
    """Build a company-browser Atom feed of NPORT-P filings.

    Args:
        entries: ``(filing date, index page url)`` pairs.

    Returns:
        The feed.
    """
    body = "".join(
        f"<entry><filing-date>{filed}</filing-date><filing-href>{href}</filing-href>"
        "<filing-type>NPORT-P</filing-type></entry>"
        for filed, href in entries
    )
    return f"<feed>{body}</feed>"


def _index_page(period: str, folder: str) -> str:
    """Build a filing index page with a report date and one NPORT-P XML document.

    Args:
        period: Report date.
        folder: Accession folder name.

    Returns:
        The page.
    """
    return (
        f'<div class="infoHead">Period of Report</div><div class="info">{period}</div>'
        f'<td><a href="/Archives/edgar/data/1/{folder}/primary_doc.xml">primary_doc.xml</a></td>'
        "<td>NPORT-P</td>"
    )


def _nport(*names: str) -> str:
    """Build an NPORT-P document holding one share of each named stock.

    Args:
        names: Security names.

    Returns:
        The XML.
    """
    securities = "".join(
        f"<invstOrSec><name>{name}</name><title>{name}</title><cusip>C{position}</cusip>"
        "<balance>1</balance><units>NS</units><valUSD>10</valUSD>"
        "<assetCat>EC</assetCat><issuerCat>CORP</issuerCat></invstOrSec>"
        for position, name in enumerate(names)
    )
    return (
        '<edgarSubmission xmlns="http://www.sec.gov/edgar/nport"><formData><invstOrSecs>'
        f"{securities}</invstOrSecs></formData></edgarSubmission>"
    )


EDGAR = {
    "type=NPORT-P": _feed(
        ("2013-08-20", "https://www.sec.gov/Archives/edgar/data/1/q2-index.htm"),
        ("2013-05-20", "https://www.sec.gov/Archives/edgar/data/1/q1-index.htm"),
    ),
    "browse-edgar": "<feed></feed>",
    "q1-index.htm": _index_page("2013-03-31", "q1"),
    "q2-index.htm": _index_page("2013-06-30", "q2"),
    "q1/primary_doc.xml": _nport("Alpha Corp", "Beta Inc", "Mystery Holdings"),
    "q2/primary_doc.xml": _nport("Alpha Corp", "Mystery Holdings"),
    "company_tickers.json": {"0": {"cik_str": 1001, "ticker": "ALP", "title": "Alpha Corp"}},
    "cik-lookup-data.txt": "ALPHA CORP:0000001001:\nBETA INC:0000001002:\n",
    "CIK0000001001.json": {
        "name": "ALPHA CORP",
        "sic": "3571",
        "sicDescription": "Electronic Computers",
        "stateOfIncorporation": "DE",
        "tickers": ["ALP"],
        "exchanges": ["NYSE"],
        "filings": {
            "recent": {
                "form": ["10-K"],
                "filingDate": ["2013-02-27"],
                "accessionNumber": ["0000001001-13-000001"],
            },
            "files": [],
        },
    },
    "CIK0000001002.json": {"name": "BETA INC", "filings": {"recent": {}, "files": []}},
    "000000100113000001": '<a href="/Archives/edgar/data/1001/x/alp-20121231.xml">',
}


def test_a_build_keeps_every_holding_and_fills_in_what_edgar_knows(tmp_path: Path) -> None:
    builder = MembershipBuilder(
        [TEST_FUND], date(2013, 3, 31), date(2013, 6, 30), fake_client(EDGAR)
    )
    membership = builder.build()
    by_name = {member.name: member for member in membership.members}
    assert list(by_name) == ["Alpha Corp", "Beta Inc", "Mystery Holdings"]

    alpha = by_name["Alpha Corp"]
    assert (alpha.cik, alpha.ticker, alpha.ticker_source) == ("1001", "ALP", "xbrl")
    assert (alpha.valid_from, alpha.valid_to, alpha.last_seen) == (
        date(2013, 3, 31),
        None,
        date(2013, 6, 30),
    )
    assert (alpha.sec_name, alpha.sic, alpha.exchanges_today) == ("ALPHA CORP", "3571", "NYSE")
    assert alpha.cusip == "C0"

    beta = by_name["Beta Inc"]  # dropped out in Q2: its span ends the day before
    assert (beta.cik, beta.ticker, beta.ticker_source) == ("1002", None, "none")
    assert (beta.valid_to, beta.last_seen) == (date(2013, 6, 29), date(2013, 3, 31))

    mystery = by_name["Mystery Holdings"]  # no CIK: kept, but without CIK or ticker
    assert (mystery.cik, mystery.ticker, mystery.name_match) == (None, None, "none")

    assert [member.name for member in membership.on("2013-06-30")] == [
        "Alpha Corp",
        "Mystery Holdings",
    ]
    assert [report.period for report in membership.reports] == [
        date(2013, 3, 31),
        date(2013, 6, 30),
    ]
    assert any("Mystery Holdings" in warning for warning in membership.warnings)

    path = membership.to_csv(tmp_path / "out" / "members.csv")
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["ticker"] for row in rows] == ["ALP", "", ""]
    assert rows[1]["valid_to"] == "2013-06-29"


def test_a_window_that_ends_before_it_starts_is_rejected() -> None:
    with pytest.raises(ValueError, match="after its end"):
        MembershipBuilder([TEST_FUND], date(2014, 1, 1), date(2013, 1, 1), fake_client({}))
