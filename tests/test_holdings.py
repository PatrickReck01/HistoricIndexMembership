"""Parsing fund holdings out of N-Q / N-CSR (HTML, text) and NPORT-P (XML) reports.

Every layout rule in ``holdings`` exists because a real filing needed it; the snippets below
are cut down from those filings.
"""

from __future__ import annotations

from datetime import date

from fakes import fake_client
from historic_index_membership.filings import Filing
from historic_index_membership.funds import load_index_funds
from historic_index_membership.holdings import (
    document_lines,
    fund_section,
    parse_holding_line,
    parse_nport_xml,
    parse_schedule,
)

IJH, IVOO, DREYFUS_MID = load_index_funds()["sp400"]


def _row(name: str, shares: str, value: str) -> str:
    """Build one schedule row the way iShares lays it out: name, blank, shares, $, value.

    Args:
        name: The name cell's inner HTML.
        shares: Share count.
        value: Market value.

    Returns:
        A ``<tr>``.
    """
    return (
        f"<tr><td><p><font>{name}</font></p></td><td>&nbsp;</td><td>{shares}</td>"
        f"<td>$</td><td>{value}</td></tr>"
    )


# A name split over two source lines, footnotes as a superscript.
SIGNATURE_BANK = "Signature\nBank<sup>a,b</sup>"
ISHARES_2013 = f"""
<html><body>
<p>Table of Contents</p>
<table><tr><td>iShares Core S&amp;P Mid-Cap ETF</td><td>33</td></tr></table>
<p>Schedule of Investments (Unaudited)</p>
<p>iSHARES<sup>&reg;</sup> CORE S&amp;P MID-CAP ETF</p>
<p>June 30, 2013</p>
<table>
<tr><td>Security</td><td>Shares</td><td>Value</td></tr>
<tr><td>COMMON STOCKS&mdash;99.9%</td></tr>
{_row(SIGNATURE_BANK, "101,761", "9,316,220")}
{_row("Equinix Inc.<sup>a</sup>", "108,213", "19,408,002")}
{_row("Lamar Advertising Co. Class&nbsp;A(a)(b)", "384,745", "11,961,722")}
<tr><td>TOTAL COMMON STOCKS</td><td>40,685,944</td></tr>
{_row("BlackRock Cash Funds: Institutional", "1,000", "1,000")}
</table>
</body></html>
"""


def test_ishares_schedule_skips_the_contents_and_drops_superscript_footnotes() -> None:
    lines = document_lines(ISHARES_2013, is_text=False)
    rows = fund_section(lines, IJH.heading_pattern)
    assert rows == [
        ("Signature Bank", 101761, 9316220),
        ("Equinix Inc.", 108213, 19408002),
        ("Lamar Advertising Co. Class A", 384745, 11961722),
    ]


def test_a_name_split_over_two_paragraphs_of_one_cell_keeps_its_space() -> None:
    html = f"""<html><body><p>iSHARES S&amp;P MIDCAP 400 INDEX FUND</p><table>
    {_row("<p>Alliance Data Systems</p><p>Corp.</p>", "606,873", "128,335,433")}
    </table></body></html>"""
    rows = fund_section(document_lines(html, is_text=False), IJH.heading_pattern)
    assert rows == [("Alliance Data Systems Corp.", 606873, 128335433)]


def test_a_heading_that_wraps_onto_two_lines_still_starts_the_section() -> None:
    html = f"""<html><body>
    <p>Schedule of Investments</p><p>iSHARES S&amp;P MIDCAP</p><p>400 INDEX FUND</p>
    <table>{_row("Harte-Hanks Inc.", "274,395", "2,957,978")}</table></body></html>"""
    assert fund_section(document_lines(html, is_text=False), IJH.heading_pattern) == [
        ("Harte-Hanks Inc.", 274395, 2957978)
    ]


def test_the_next_funds_heading_ends_the_section() -> None:
    html = f"""<html><body>
    <p>iSHARES S&amp;P MIDCAP 400 INDEX FUND</p>
    <table>{_row("Harte-Hanks Inc.", "274,395", "2,957,978")}</table>
    <p>iSHARES S&amp;P MIDCAP 400 GROWTH INDEX FUND</p>
    <table>{_row("Growth Only Inc.", "1,000", "2,000")}</table></body></html>"""
    rows = fund_section(document_lines(html, is_text=False), IJH.heading_pattern)
    assert [name for name, _, _ in rows] == ["Harte-Hanks Inc."]


def test_vanguard_star_column_is_dropped_and_values_are_scaled_from_thousands() -> None:
    html = """<html><body><p>Vanguard S&amp;P Mid-Cap 400 Index Fund</p>
    <table>
    <tr><td></td><td>Shares</td><td>Market Value ($000)</td></tr>
    <tr><td>*</td><td>LKQ Corp.</td><td>77,417</td><td>1,895</td></tr>
    <tr><td></td><td>Tractor Supply Co.</td><td>12,000</td><td>1,300</td></tr>
    <tr><td>Total Common Stocks (Cost $301,247)</td><td>350,725</td></tr>
    </table></body></html>"""
    filing = Filing(IVOO, "N-Q", date(2013, 7, 31), "index", date(2013, 5, 31))
    filing.documents = [("https://www.sec.gov/Archives/vanguard.htm", "N-Q")]
    holdings = parse_schedule(fake_client({"vanguard.htm": html}), filing)
    assert [(h.name, h.shares, h.value_usd) for h in holdings] == [
        ("LKQ Corp.", 77417, 1_895_000),
        ("Tractor Supply Co.", 12000, 1_300_000),
    ]


def test_dreyfus_footnote_cells_and_wrapped_names() -> None:
    lines = [
        "Dreyfus Midcap Index Fund, Inc.",
        "Common Stocks--97.9% | Shares | Value ($)",
        "Signature Bank | 101,761 | a | 9,316,220",
        "Charles River Laboratories",
        "International | 107,672 | a | 4,903,383",
        "Health Management Associates, Cl.",
        "A | 568,355 | a | 7,661,425",
        "Aaron's | 113,036 a,b | 3,149,183",
    ]
    assert fund_section(lines, DREYFUS_MID.heading_pattern) == [
        ("Signature Bank", 101761, 9316220),
        ("Charles River Laboratories International", 107672, 4903383),
        ("Health Management Associates, Cl. A", 568355, 7661425),
        ("Aaron's", 113036, 3149183),
    ]


def test_long_wrapped_names_join_but_capitalised_industry_headers_do_not() -> None:
    lines = [
        "Vanguard S&P Mid-Cap 400 Index Fund",
        "Lender Processing",
        "Services Inc. Class B | 20,000 | 600",
        "IT SERVICES",
        "Alliance Data Systems Corp. | 606,873 | 128,335",
    ]
    assert [name for name, _, _ in fund_section(lines, IVOO.heading_pattern)] == [
        "Lender Processing Services Inc. Class B",
        "Alliance Data Systems Corp.",
    ]


def test_holding_lines_reject_totals_rights_and_debt() -> None:
    assert parse_holding_line("Legg Mason Inc. (1) | 1,000 | 50,000") == (
        "Legg Mason Inc.",
        1000,
        50000,
    )
    assert parse_holding_line("Total Common Stocks | 1,000 | 50,000") is None
    assert parse_holding_line("Contingent Value Rights | 1,000 | 50") is None
    assert parse_holding_line("J. Crew Group Inc. Escrow | 1,000 | 50") is None
    assert parse_holding_line("United States Treasury Bill | 0.125% | 2/10/11 | 1,400") is None


NPORT = """<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/nport">
  <formData><invstOrSecs>
    <invstOrSec>
      <name>Teradyne Inc</name><title>Teradyne Inc</title><cusip>880770102</cusip>
      <identifiers><isin value="US8807701029"/></identifiers>
      <balance>100</balance><units>NS</units><valUSD>8450.5</valUSD>
      <assetCat>EC</assetCat><issuerCat>CORP</issuerCat>
    </invstOrSec>
    <invstOrSec>
      <name>iShares Trust</name><title>iShares Core S&amp;P Small-Cap ETF</title>
      <balance>10</balance><units>NS</units><valUSD>900</valUSD>
      <assetCat>EC</assetCat><issuerCat>RF</issuerCat>
    </invstOrSec>
    <invstOrSec>
      <name>BlackRock Cash Funds</name><balance>5</balance><units>NS</units>
      <valUSD>5</valUSD><assetCat>STIV</assetCat><issuerCat>RF</issuerCat>
    </invstOrSec>
  </invstOrSecs></formData>
</edgarSubmission>"""


def test_nport_keeps_common_stocks_and_drops_funds_held_for_cash() -> None:
    holdings = parse_nport_xml(NPORT)
    assert [(h.name, h.shares, h.value_usd, h.cusip, h.isin) for h in holdings] == [
        ("Teradyne Inc", 100, 8450, "880770102", "US8807701029")
    ]


def test_dot_leaders_of_plain_text_reports_are_not_part_of_the_name() -> None:
    assert parse_holding_line("Alcoa, Inc. ...... | 1,000 | 50,000") == ("Alcoa, Inc.", 1000, 50000)
    assert parse_holding_line("3M Co. . . . . . | 1,000 | 50,000") == ("3M Co.", 1000, 50000)


def test_fund_profile_rows_under_the_heading_are_not_holdings() -> None:
    assert parse_holding_line("Number of Stocks | 507 | 500") is None
    assert parse_holding_line("Consumer Discretionary | 14 | 14") is None
    assert parse_holding_line("Energy Transfer Equity | 1,000 | 50,000") is not None


def test_a_heading_with_its_words_run_together_still_starts_the_section() -> None:
    lines = ["DreyfusSmallcapStockIndexFund", "Aaron's | 113,036 | 3,149,183"]
    assert fund_section(lines, "SMALL ?CAP STOCK INDEX FUND") == [("Aaron's", 113036, 3149183)]


def test_invesco_rows_put_the_share_count_first() -> None:
    assert parse_holding_line("39,413,497 | Apple, Inc. | 6,074,408,158") == (
        "Apple, Inc.",
        39413497,
        6074408158,
    )


def test_a_next_fund_heading_with_its_words_run_together_ends_the_section() -> None:
    lines = [
        "Dreyfus S & P 500 Index Fund",
        "Apple Inc. | 1,000 | 50,000",
        "DreyfusSmallcapStockIndexFund",
        "Aaron's Inc.+ | 2,000 | 60,000",
    ]
    assert fund_section(lines, "S ?& ?P 500 INDEX FUND") == [("Apple Inc.", 1000, 50000)]
    assert parse_holding_line("Interwoven Inc.+ | 2,000 | 60,000") == ("Interwoven Inc.", 2000, 60000)


def test_a_cost_line_is_not_a_holding() -> None:
    assert parse_holding_line("(cost$38,399,347) | 1,000 | 50,000") is None
