"""The index -> fund table: which funds' SEC holdings reports stand in for which index.

Every fund listed fully replicates its index, so its common-stock holdings on a report date
are the index membership on that date. The table lives in ``data/index_funds.csv`` and can
be extended, or replaced with a file of one's own, without touching the code. One row per
``(index, fund)`` pair; an index may list several funds, tried in ``priority`` order:

``index_id``
    Short name used on the command line, e.g. ``sp500``.
``index_name``
    Readable index name, e.g. ``S&P 500``.
``priority``
    1 is tried first. Later funds fill quarters the earlier ones cannot.
``fund_ticker``, ``fund_name``
    Used in logs and in the report list of a result.
``series_id``
    EDGAR series id (``S0000...``) of the fund. Empty for a unit investment trust such as
    SPY or DIA, which has no series and files under ``registrant_cik`` alone.
``registrant_cik``
    CIK of the trust that files for the fund. Filings before mid-2006 carry no series id
    and are listed by the trust instead.
``heading_pattern``
    Regex (case-insensitive) matching the fund's heading in its N-Q / N-CSR / N-30D
    documents, which often cover dozens of funds of one trust. A space also matches no
    space or several. Unused for NPORT-P, so it may be empty for a fund only needed
    from 2019 on.
``values_in_thousands``
    ``true`` if those documents report market values in $000 (Vanguard).
``min_holdings``, ``max_holdings``
    A parsed report is only trusted if its number of stocks falls in this range: below it
    a section was cut short or is a summary schedule, above it two funds ran together.
``notes``
    Free text.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

DEFAULT_FUNDS_CSV = Path(__file__).resolve().parent / "data" / "index_funds.csv"
REQUIRED_COLUMNS = (
    "index_id",
    "index_name",
    "priority",
    "fund_ticker",
    "fund_name",
    "series_id",
    "registrant_cik",
    "heading_pattern",
    "values_in_thousands",
    "min_holdings",
    "max_holdings",
)


@dataclass(frozen=True)
class FundSource:
    """A fund whose SEC holdings reports stand in for an index's membership.

    Attributes:
        index_id: Short index name, e.g. ``sp500``.
        index_name: Readable index name, e.g. ``S&P 500``.
        priority: Order in which the funds of one index are tried; 1 first.
        fund_ticker: The fund's ticker, e.g. ``IVV``.
        fund_name: Readable fund name.
        series_id: EDGAR series id, or None for a unit investment trust.
        registrant_cik: CIK of the filing trust, or None.
        heading_pattern: Case-insensitive regex for the fund's heading in HTML reports.
        values_in_thousands: True if HTML reports give market values in $000.
        min_holdings: Fewest stocks a trusted report may hold.
        max_holdings: Most stocks a trusted report may hold.
    """

    index_id: str
    index_name: str
    priority: int
    fund_ticker: str
    fund_name: str
    series_id: str | None
    registrant_cik: str | None
    heading_pattern: str
    values_in_thousands: bool
    min_holdings: int
    max_holdings: int

    @property
    def label(self) -> str:
        """Readable name for logs and report lists.

        Returns:
            E.g. ``"iShares Core S&P 500 ETF (IVV)"``.
        """
        return f"{self.fund_name} ({self.fund_ticker})"


def _parse_bool(value: str) -> bool:
    """Read a CSV boolean.

    Args:
        value: ``true`` / ``false`` (also ``1`` / ``0``, ``yes`` / ``no``), any case.

    Returns:
        The boolean.

    Raises:
        ValueError: If the value is none of those.
    """
    lowered = value.strip().lower()
    if lowered in ("true", "1", "yes"):
        return True
    if lowered in ("false", "0", "no"):
        return False
    raise ValueError(f"not a boolean: {value!r}")


def _fund_from_row(row: dict[str, str], line: int) -> FundSource:
    """Build a fund from one CSV row.

    Args:
        row: The row, keyed by column name.
        line: Line number in the file, for error messages.

    Returns:
        The fund.

    Raises:
        ValueError: If a value is missing or malformed.
    """
    series_id = row["series_id"].strip() or None
    registrant_cik = row["registrant_cik"].strip().lstrip("0") or None
    if series_id is None and registrant_cik is None:
        raise ValueError(f"line {line}: a fund needs a series_id, a registrant_cik or both")
    try:
        return FundSource(
            index_id=row["index_id"].strip(),
            index_name=row["index_name"].strip(),
            priority=int(row["priority"]),
            fund_ticker=row["fund_ticker"].strip(),
            fund_name=row["fund_name"].strip(),
            series_id=series_id,
            registrant_cik=registrant_cik,
            heading_pattern=row["heading_pattern"].strip(),
            values_in_thousands=_parse_bool(row["values_in_thousands"]),
            min_holdings=int(row["min_holdings"]),
            max_holdings=int(row["max_holdings"]),
        )
    except ValueError as error:
        raise ValueError(f"line {line}: {error}") from error


def load_index_funds(path: str | Path | None = None) -> dict[str, list[FundSource]]:
    """Read the index -> fund table.

    Args:
        path: CSV file; the bundled ``data/index_funds.csv`` when omitted.

    Returns:
        Index id -> its funds in priority order, indexes in file order.

    Raises:
        ValueError: If a required column is missing or a row is malformed.
    """
    path = Path(path) if path is not None else DEFAULT_FUNDS_CSV
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [column for column in REQUIRED_COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path}: missing columns {missing}")
        funds: dict[str, list[FundSource]] = {}
        for line, row in enumerate(reader, start=2):
            fund = _fund_from_row(row, line)
            funds.setdefault(fund.index_id, []).append(fund)
    for index_funds in funds.values():
        index_funds.sort(key=lambda fund: fund.priority)
    return funds


def funds_for(index_id: str, path: str | Path | None = None) -> list[FundSource]:
    """Look up the funds of one index.

    Args:
        index_id: Short index name, e.g. ``sp500``.
        path: CSV file; the bundled one when omitted.

    Returns:
        The index's funds in priority order.

    Raises:
        ValueError: If the index is not in the table.
    """
    funds = load_index_funds(path)
    if index_id not in funds:
        raise ValueError(f"unknown index {index_id!r}; available: {', '.join(funds)}")
    return funds[index_id]
