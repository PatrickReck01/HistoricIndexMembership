"""Parsing a fund's common-stock holdings out of its SEC reports.

N-Q, N-CSR and N-30D documents are HTML (plain text in some early filings) and often cover
every fund of a trust in one file; iShares puts 50 funds into one 16 MB document. The document is
flattened into lines, one per table row with the cells joined by `` | ``, the fund's section
is found by its heading, and ``name | shares | value`` rows are read until the common stocks
end. NPORT-P is structured XML and needs none of that.

The layouts differ in small ways that each cost a few dozen names when missed; every rule
below is there because a real filing needed it:

* footnote markers glued to names as superscripts (iShares from 2013: ``Equinix Inc.a,b``),
  in their own cells (Vanguard's ``*`` column, Dreyfus's ``a`` column from 2013), in
  parentheses (``Lamar Class A(a)(b)``, ``Legg Mason Inc. (1)``) or after the share count
  (``145,225 a,b``);
* names wrapping onto a second line (``Charles River Laboratories`` / ``International``);
* headings wrapping onto a second line (``iSHARES S&P MIDCAP`` / ``400 INDEX FUND``);
* the same heading in a table of contents, a performance overview or a summary schedule,
  which is why the longest run of holdings after any heading wins.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree

from selectolax.parser import HTMLParser, Node

from historic_index_membership.filings import Filing
from historic_index_membership.net import CachedHttpClient

AMOUNT = re.compile(r"\d{1,3}(?:,\d{3})*")
# Footnote markers after a share count, e.g. "145,225 a,b" (Dreyfus) or "274,395(a)".
FOOTNOTES_AFTER_AMOUNT = re.compile(r"(?:\s*,?\s*\(?[a-z*†‡§]\)?)+$")
# Footnote markers around a name: "* Ford Motor Co." (Vanguard), "Lamar Class A(a)(b)",
# "Legg Mason Inc. (1)". Superscript markers are already gone by the time a name is read.
NAME_MARKERS_BEFORE = re.compile(r"^[*†‡§^#+\s]+")
NAME_MARKERS_AFTER = re.compile(r"(?:\s*\([a-z1-9]\)|\s*[*†‡§^#+])+$")
# Dot leaders between a name and its numbers in plain-text reports: "Alcoa, Inc. ......".
DOT_LEADER = re.compile(r"(?:\s+(?:\.\s*){3,}|\.{4,}[\s.]*)$")
# A cell holding nothing but footnote letters, e.g. "a" or "a,b".
FOOTNOTE_CELL = re.compile(r"\(?[a-z]\)?(?:\s*,\s*\(?[a-z]\)?)*")
# A cell holding nothing but a currency sign, a bracket or a footnote symbol.
FILLER_CELL = re.compile(r"[$()%*†‡§^#]+")
# The first part of a name that wraps onto the next line.
WRAPPED_NAME_START = re.compile(r"[A-Za-z][A-Za-z&.,'\u2019/\- ]{2,60}")
WRAPPED_NAME_NOT = re.compile(
    r"(?i)contents|schedule|statement|unaudited|continued|security|shares|value|stocks"
    r"|investments|fund"
)
# Words a wrapped name's second half may have at most ("of America Inc. Class A").
MAX_WRAPPED_WORDS = 6
NOT_A_HOLDING = re.compile(
    r"(?i)^(total|net assets|other assets|liabilities|number of (stocks|holdings|securities))"
    r"|\b(rights?|warrants?|escrow)\b|\(cost\s*\$"
)
# Rows of a fund-profile table printed under the fund's heading, which have the same
# "name | number | number" shape as a holding: Vanguard's 2005-2008 reports list the
# sector weights of fund and index ("Consumer Discretionary | 14 | 14"), in the GICS
# sectors from 2007 and in the older Russell sectors before.
PROFILE_ROW = re.compile(
    r"(?i)(consumer discretionary|consumer staples|energy|financials?|health care"
    r"|industrials|information technology|materials|real estate|telecommunication services"
    r"|communication services|utilities|other"
    r"|auto & transportation|financial services|integrated oils|other energy"
    r"|materials & processing|producer durables|technology)"
)
# The common-stock part of a schedule ends at the first of these lines.
END_OF_STOCKS = re.compile(
    r"(?i)^(total common stocks|total investments|total equities|short-term investments?"
    r"|temporary cash investments?|other investments?\b|investment of cash collateral"
    r"|money market)"
)
# A short line naming a fund that is not ours: the next fund's schedule has started.
# Words may run together ("DreyfusSmallcapStockIndexFund").
FUND_HEADING = re.compile(r"(?i)(index\s*fund|\betf)\b")
MAX_SECTION_LINES = 8000
# Lines between a heading and its first holding before the heading is given up on. Vanguard
# prints a fund profile and its performance (~100 lines) between the two.
MAX_LINES_BEFORE_FIRST_HOLDING = 200
_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "table", "center", "hr"}
)
_SKIPPED_TAGS = frozenset({"script", "style", "head", "title"})
_WHITESPACE = re.compile(r"\s+")
_DOCUMENT_TYPE = re.compile(r"^(N-|EX-99)")
_NOT_A_SCHEDULE = re.compile(r"CERT|CODE|302|906")


@dataclass(frozen=True)
class Holding:
    """One common-stock position in a fund report.

    Attributes:
        name: Security name as the fund writes it, footnote markers removed.
        shares: Number of shares held.
        value_usd: Market value in dollars.
        cusip: CUSIP, NPORT-P only.
        isin: ISIN, NPORT-P only.
    """

    name: str
    shares: int
    value_usd: int
    cusip: str = ""
    isin: str = ""


def _squash(text: str) -> str:
    """Collapse runs of whitespace (including non-breaking spaces) to single spaces.

    Args:
        text: Raw text.

    Returns:
        The trimmed, single-spaced text.
    """
    return _WHITESPACE.sub(" ", text.replace("\xa0", " ")).strip()


def _walk(node: Node, lines: list[str], pending: list[str]) -> None:
    """Append a node's text to ``lines``, one line per table row and per block element.

    Args:
        node: The element to walk.
        lines: Finished lines.
        pending: Text of the line being built, flushed at every block boundary.
    """

    def flush() -> None:
        text = _squash("".join(pending))
        if text:
            lines.append(text)
        pending.clear()

    for child in node.iter(include_text=True):
        tag = child.tag
        if tag == "-text":
            pending.append(child.text(deep=False) or "")
        elif tag == "tr":
            flush()
            cells = [_cell_text(cell) for cell in child.css("td, th")]
            cells = [cell for cell in cells if cell and not FILLER_CELL.fullmatch(cell)]
            if cells:
                lines.append(" | ".join(cells))
        elif tag in _SKIPPED_TAGS:
            continue
        elif tag in _BLOCK_TAGS:
            flush()
            _walk(child, lines, pending)
            flush()
        else:
            _walk(child, lines, pending)


def _cell_text(cell: Node) -> str:
    """Return a table cell's text, with block elements inside it kept apart by a space.

    A name can be split over two paragraphs of one cell (``<p>Alliance Data Systems</p>
    <p>Corp.</p>``); joining their text directly would glue the words together.

    Args:
        cell: A ``td`` or ``th`` element.

    Returns:
        The single-spaced cell text.
    """
    parts: list[str] = []
    pending: list[str] = []
    _walk(cell, parts, pending)
    return _squash(" ".join([*parts, "".join(pending)]))


def document_lines(raw: str, is_text: bool) -> list[str]:
    """Flatten a filing document into lines, one line per table row.

    Table cells are joined with `` | `` so HTML and plain-text schedules read alike.

    Args:
        raw: The document.
        is_text: True for plain-text (``.txt``) documents.

    Returns:
        Non-empty, single-spaced lines.
    """
    lines: list[str] = []
    if is_text:
        for line in raw.splitlines():
            cells = [cell.strip() for cell in re.split(r"\s{2,}|\$", line.strip())]
            joined = " | ".join(cell for cell in cells if cell)
            if joined:
                lines.append(joined)
        return lines

    tree = HTMLParser(raw)
    for superscript in tree.css("sup"):  # footnote markers such as "Equinix Inc.<sup>a,b</sup>"
        superscript.decompose()
    root = tree.body or tree.root
    if root is not None:
        pending: list[str] = []
        _walk(root, lines, pending)
        tail = _squash("".join(pending))
        if tail:
            lines.append(tail)
    return lines


def parse_holding_line(line: str) -> tuple[str, int, int] | None:
    """Read a ``name | shares | value`` schedule line.

    Invesco's reports put the share count first (``shares | name | value``); that order is
    read too.

    Args:
        line: One line from :func:`document_lines`.

    Returns:
        ``(name, shares, value)``, or None if the line is not a stock holding.
    """
    cells = [cell for cell in line.split(" | ") if not FOOTNOTE_CELL.fullmatch(cell)]
    if len(cells) != 3:
        return None
    name, shares, value = cells
    if AMOUNT.fullmatch(name.strip()) and not AMOUNT.fullmatch(shares.strip()):
        name, shares = shares, name
    shares = FOOTNOTES_AFTER_AMOUNT.sub("", shares).strip()
    value = value.strip()
    if not AMOUNT.fullmatch(shares) or not AMOUNT.fullmatch(value):
        return None
    if not re.search(r"[A-Za-z]{2}", name) or NOT_A_HOLDING.search(name):
        return None
    if PROFILE_ROW.fullmatch(name.strip()):
        return None
    name = DOT_LEADER.sub("", name)
    name = NAME_MARKERS_AFTER.sub("", NAME_MARKERS_BEFORE.sub("", name)).strip()
    return name, int(shares.replace(",", "")), int(value.replace(",", ""))


def fund_section(lines: list[str], heading: str) -> list[tuple[str, int, int]]:
    """Extract one fund's common stocks from a document that may cover many funds.

    Every line matching the heading is tried as a start. Holdings are read until the end of
    the common stocks or the start of another fund, and the longest run wins.

    Args:
        lines: Output of :func:`document_lines`.
        heading: Regex matching the fund's heading, applied case-insensitively. A space in
            it also matches no space or several: some filings run the heading's words
            together ("DreyfusSmallcapStockIndexFund").

    Returns:
        ``(name, shares, value)`` tuples of the longest schedule found; empty if none.
    """
    heading_pattern = re.compile(heading.replace(" ", r"\s*"), re.IGNORECASE)
    whole = [bool(heading_pattern.search(line)) for line in lines]
    # A heading can wrap onto a second line ("iSHARES S&P MIDCAP" / "400 INDEX FUND"): its
    # first half is a heading when it matches only together with the next line, and the
    # second half is then skipped.
    first_half = [
        not whole[position]
        and position + 1 < len(lines)
        and not whole[position + 1]
        and bool(heading_pattern.search(f"{lines[position]} {lines[position + 1]}"))
        for position in range(len(lines))
    ]
    is_heading = [a or b for a, b in zip(whole, first_half, strict=True)]
    best: list[tuple[str, int, int]] = []
    for start, starts_here in enumerate(is_heading):
        if not starts_here:
            continue
        rows: list[tuple[str, int, int]] = []
        stop = min(len(lines), start + MAX_SECTION_LINES)
        for position in range(start + 1, stop):
            line = lines[position]
            if rows and END_OF_STOCKS.match(line):
                break
            if is_heading[position] or first_half[position - 1]:  # page header, same fund
                continue
            if " | " not in line and len(line) < 150 and FUND_HEADING.search(line):
                break
            previous = lines[position - 1]
            # Industry headers are written in capitals ("IT SERVICES") and never wrap.
            wrapped = (
                WRAPPED_NAME_START.fullmatch(previous) is not None
                and previous != previous.upper()
                and not WRAPPED_NAME_NOT.search(previous)
                and not is_heading[position - 1]
            )
            holding = parse_holding_line(line)
            # A name wrapped onto a second line: "Charles River Laboratories" /
            # "International | 107,672 | 4,903,383", or "..., Cl." / "A | 568,355 | ...".
            if wrapped and (holding is None or len(holding[0].split()) <= MAX_WRAPPED_WORDS):
                holding = parse_holding_line(f"{previous} {line}") or holding
            if holding is None:
                if not rows and position - start > MAX_LINES_BEFORE_FIRST_HOLDING:
                    break
                continue
            rows.append(holding)
        if len(rows) > len(best):
            best = rows
    return best


def parse_schedule(client: CachedHttpClient, filing: Filing) -> list[Holding]:
    """Read a fund's common stocks from an N-Q / N-CSR / N-CSRS / N-30D / N-30B-2 filing.

    Args:
        client: EDGAR client.
        filing: The filing, with ``documents`` filled in.

    Returns:
        The holdings of the longest schedule of the fund in any document of the filing.
    """
    best: list[tuple[str, int, int]] = []
    for url, kind in filing.documents:
        if not _DOCUMENT_TYPE.match(kind) or _NOT_A_SCHEDULE.search(kind):
            continue
        lines = document_lines(client.fetch(url).text, is_text=url.lower().endswith(".txt"))
        rows = fund_section(lines, filing.fund.heading_pattern)
        if len(rows) > len(best):
            best = rows
    scale = 1000 if filing.fund.values_in_thousands else 1
    return [Holding(name, shares, value * scale) for name, shares, value in best]


def _local(tag: str) -> str:
    """Strip the namespace from an XML tag.

    Args:
        tag: A tag such as ``{http://www.sec.gov/edgar/nport}name``.

    Returns:
        The local name, e.g. ``name``.
    """
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ElementTree.Element, name: str) -> str:
    """Return the text of the first descendant with a local tag name.

    Args:
        element: Element to search.
        name: Local tag name.

    Returns:
        The stripped text, or an empty string if there is none.
    """
    for descendant in element.iter():
        if _local(descendant.tag) == name:
            return (descendant.text or "").strip()
    return ""


def parse_nport_xml(xml: str) -> list[Holding]:
    """Read the common stocks out of an NPORT-P ``primary_doc.xml``.

    Kept: equity-common (``EC``) positions counted in shares. Dropped: other registered
    funds (``issuerCat`` ``RF``), which index funds hold to park cash -- a Dreyfus small-cap
    fund reports its IJR position as an equity like any other -- plus rights and warrants.

    Args:
        xml: The document.

    Returns:
        The holdings, with CUSIP and ISIN.
    """
    root = ElementTree.fromstring(xml)
    holdings = []
    for security in root.iter():
        if _local(security.tag) != "invstOrSec":
            continue
        if _child_text(security, "assetCat") != "EC" or _child_text(security, "units") != "NS":
            continue
        if _child_text(security, "issuerCat") == "RF":
            continue
        name = _child_text(security, "name")
        if (
            not name
            or NOT_A_HOLDING.search(name)
            or NOT_A_HOLDING.search(_child_text(security, "title"))
        ):
            continue
        isin = next(
            (item.get("value", "") for item in security.iter() if _local(item.tag) == "isin"), ""
        )
        holdings.append(
            Holding(
                name=name,
                shares=int(float(_child_text(security, "balance") or 0)),
                value_usd=int(float(_child_text(security, "valUSD") or 0)),
                cusip=_child_text(security, "cusip"),
                isin=isin,
            )
        )
    return holdings


def parse_nport(client: CachedHttpClient, filing: Filing) -> list[Holding]:
    """Read a fund's common stocks from an NPORT-P filing.

    Args:
        client: EDGAR client.
        filing: The filing, with ``documents`` filled in.

    Returns:
        The holdings; empty if the filing has no raw XML document.
    """
    urls = [url for url, _ in filing.documents if url.endswith("/primary_doc.xml")]
    raw = [url for url in urls if "xsl" not in url.lower()]
    if not raw:
        return []
    return parse_nport_xml(client.fetch(raw[0]).text)


def parse_holdings(client: CachedHttpClient, filing: Filing) -> list[Holding]:
    """Read a fund's common stocks from any holdings report.

    Args:
        client: EDGAR client.
        filing: The filing, with ``documents`` filled in.

    Returns:
        The holdings.
    """
    if filing.form == "NPORT-P":
        return parse_nport(client, filing)
    return parse_schedule(client, filing)
