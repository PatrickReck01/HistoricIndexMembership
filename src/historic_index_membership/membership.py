"""Index membership over a date window, reconstructed from index-fund holdings reports.

For every calendar quarter the window touches, the holdings report of the index's preferred
fund is read; a report that does not parse to a plausible number of stocks falls back to the
next fund in ``data/index_funds.csv``. The first snapshot is the last report on or before
the window start, so companies that were members when the window opened are included.

Each holding is matched to a CIK, and the per-quarter sets are diffed into membership spans
(``intervals_from_snapshots``). A span is then stretched to the day before the first
snapshot the company is missing from: the exact change date lies somewhere in between, and
the reconstruction errs towards inclusion.

A company's ticker is the one its own filings named around its time in the index (see
``tickers``). A holding that matches no CIK, or a CIK with no ticker, is kept with the
missing fields left empty, and listed in the result's warnings.
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

import structlog

from historic_index_membership.config import Settings, load_settings
from historic_index_membership.filings import Filing, list_filings, read_filing_index
from historic_index_membership.funds import FundSource, funds_for
from historic_index_membership.holdings import Holding, parse_holdings
from historic_index_membership.names import CikMatcher, name_keys
from historic_index_membership.net import CachedHttpClient, edgar_client
from historic_index_membership.sec import CompanyProfile
from historic_index_membership.tickers import HistoricalTickers

log = structlog.get_logger(__name__)

# N-CSR and N-30D are due 70 days after the report date, N-Q and NPORT-P 60 days.
MAX_FILING_LAG = timedelta(days=75)
# Share-class and listing suffixes a fund writes after a name, which are not part of it.
_NAME_SUFFIX = re.compile(r"(?:,?\s+(?:Cl\.?|Class)\s+[A-Z]|\s+NVS|\s+NYS|\s+ADR)+$", re.I)
# Key prefix for a holding whose name matched no CIK.
UNMATCHED_PREFIX = "name:"


@dataclass(frozen=True)
class Snapshot:
    """The index membership read from one fund report.

    Attributes:
        period: Report date of the holdings.
        filing: The report it was read from.
        holdings: The common stocks held.
    """

    period: date
    filing: Filing
    holdings: list[Holding]


@dataclass(frozen=True)
class Report:
    """A fund report a membership was read from.

    Attributes:
        period: Report date of the holdings.
        fund: The fund, e.g. ``"iShares Core S&P 500 ETF (IVV)"``.
        form: EDGAR form type.
        url: The filing's EDGAR index page.
        stocks: Number of stocks read from it.
    """

    period: date
    fund: str
    form: str
    url: str
    stocks: int


@dataclass
class Member:
    """One company's membership span, with what EDGAR knows about the company.

    Attributes:
        index: Index id, e.g. ``sp500``.
        name: Company name as the fund reports write it, share-class suffixes removed.
        ticker: Ticker at the time, or None if none could be found.
        cik: SEC CIK without leading zeros, or None if the name matched none.
        valid_from: First report date the company was held on.
        valid_to: Day before the first report it was missing from; None if it was still
            held on the last report of the window.
        last_seen: Last report date the company was held on.
        ticker_source: Where the ticker came from: ``xbrl``, ``today``,
            ``press_release`` or ``none``; empty without a CIK.
        name_match: How the name was matched to the CIK: ``exact``, ``loose``,
            ``ambiguous`` or ``none``.
        sec_name: The company's current name on EDGAR.
        tickers_today: Tickers EDGAR lists for the company today, ``|``-separated.
        exchanges_today: Exchanges of those tickers, ``|``-separated.
        sic: Standard Industrial Classification code.
        sic_description: The SIC code's industry name.
        state_of_incorporation: Two-letter state or country code.
        cusip: CUSIP, from NPORT-P reports (2019 on) only.
        isin: ISIN, from NPORT-P reports (2019 on) only.
    """

    index: str
    name: str
    ticker: str | None
    cik: str | None
    valid_from: date
    valid_to: date | None
    last_seen: date
    ticker_source: str = ""
    name_match: str = ""
    sec_name: str = ""
    tickers_today: str = ""
    exchanges_today: str = ""
    sic: str = ""
    sic_description: str = ""
    state_of_incorporation: str = ""
    cusip: str = ""
    isin: str = ""

    def covers(self, on: date) -> bool:
        """Whether the company was a member on a date.

        Args:
            on: The date.

        Returns:
            True if ``on`` falls inside the span.
        """
        return self.valid_from <= on and (self.valid_to is None or on <= self.valid_to)


@dataclass
class IndexMembership:
    """An index's reconstructed membership over a window.

    Attributes:
        index: Index id, e.g. ``sp500``.
        index_name: Readable index name.
        start: Window start.
        end: Window end.
        members: One entry per company and membership span.
        reports: The fund reports read, ascending by report date.
        warnings: Quarters without a usable report, unmatched names, missing tickers.
    """

    index: str
    index_name: str
    start: date
    end: date
    members: list[Member]
    reports: list[Report] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def on(self, day: date | str) -> list[Member]:
        """List the members on one date.

        Args:
            day: The date, or an ISO date string.

        Returns:
            The companies whose span covers the date.
        """
        day = _as_date(day)
        return [member for member in self.members if member.covers(day)]

    def to_csv(self, path: str | Path) -> Path:
        """Write the members to a CSV file, one row per company and span.

        Args:
            path: Output file; parent directories are created.

        Returns:
            The path written.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        columns = list(Member.__dataclass_fields__)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for member in self.members:
                row = asdict(member)
                writer.writerow({key: "" if value is None else value for key, value in row.items()})
        return path


def _as_date(value: date | str) -> date:
    """Accept a date or an ISO date string.

    Args:
        value: ``date(2013, 6, 30)`` or ``"2013-06-30"``.

    Returns:
        The date.
    """
    return value if isinstance(value, date) else date.fromisoformat(value)


def _quarter_end(year: int, quarter: int) -> date:
    """Return the last day of a calendar quarter.

    Args:
        year: The year.
        quarter: Quarter number counted from January of ``year``; values outside 0..3
            roll into neighbouring years (-1 is the previous October-December).

    Returns:
        March 31, June 30, September 30 or December 31 of the right year.
    """
    year, quarter = year + quarter // 4, quarter % 4
    month = 3 * quarter + 3
    return date(year, month, 31 if month in (3, 12) else 30)


def quarter_ends(start: date, end: date) -> list[date]:
    """List the calendar quarter ends from the last one on or before ``start`` to ``end``.

    Args:
        start: Window start.
        end: Window end.

    Returns:
        Quarter-end dates, ascending.
    """
    quarter = (start.month - 1) // 3
    if _quarter_end(start.year, quarter) > start:
        quarter -= 1
    ends = []
    while (current := _quarter_end(start.year, quarter)) <= end:
        ends.append(current)
        quarter += 1
    return ends


def intervals_from_snapshots(
    snapshots: list[tuple[date, set[str]]],
) -> list[tuple[str, date, date | None]]:
    """Diff consecutive constituent snapshots into per-key intervals.

    A key that leaves and later returns yields two intervals. ``valid_to`` is the last
    date the key was actually present; a key present in the final snapshot gets ``None``,
    meaning "still a member".

    Args:
        snapshots: ``(date, keys)`` pairs, sorted by date.

    Returns:
        ``(key, valid_from, valid_to)`` triples.
    """
    intervals: list[tuple[str, date, date | None]] = []
    open_since: dict[str, date] = {}
    previous_date: date | None = None
    previous_keys: set[str] = set()

    for current_date, keys in snapshots:
        for key in keys - previous_keys:
            open_since[key] = current_date
        for key in previous_keys - keys:
            start = open_since.pop(key, current_date)
            intervals.append((key, start, previous_date))
        previous_date, previous_keys = current_date, keys

    for key, start in open_since.items():
        intervals.append((key, start, None))
    intervals.sort(key=lambda triple: (triple[0], triple[1]))
    return intervals


def extend_spans(
    intervals: list[tuple[str, date, date | None]], snapshot_dates: list[date]
) -> list[tuple[str, date, date | None]]:
    """Stretch each span's end to the day before the next snapshot.

    Args:
        intervals: ``(key, first seen, last seen)`` from ``intervals_from_snapshots``.
        snapshot_dates: All snapshot dates, ascending.

    Returns:
        The intervals with ``valid_to`` moved to the day before the first snapshot the key
        was missing from. Open-ended intervals stay open.
    """
    extended = []
    for key, valid_from, valid_to in intervals:
        if valid_to is not None:
            later = [day for day in snapshot_dates if day > valid_to]
            if later:
                valid_to = later[0] - timedelta(days=1)
        extended.append((key, valid_from, valid_to))
    return extended


def clean_name(name: str) -> str:
    """Drop the share-class and listing suffixes fund reports put after a name.

    Args:
        name: Name as a fund report writes it, e.g. ``"Under Armour, Cl. A"``.

    Returns:
        The company name, e.g. ``"Under Armour"``.
    """
    return _NAME_SUFFIX.sub("", name).strip()


class MembershipBuilder:
    """Reconstructs one index's membership from its funds' holdings reports."""

    def __init__(
        self,
        funds: list[FundSource],
        start: date,
        end: date,
        client: CachedHttpClient,
    ) -> None:
        """Initialise the builder.

        Args:
            funds: The index's funds, in priority order.
            start: Window start, inclusive.
            end: Window end, inclusive.
            client: EDGAR client.

        Raises:
            ValueError: If there are no funds or the window is empty.
        """
        if not funds:
            raise ValueError("an index needs at least one fund")
        if start > end:
            raise ValueError(f"window start {start} is after its end {end}")
        self.funds = funds
        self.index = funds[0].index_id
        self.index_name = funds[0].index_name
        self.start = start
        self.end = end
        self.client = client

    def snapshots(self) -> tuple[list[Snapshot], list[date], list[str]]:
        """Read one membership snapshot per calendar quarter of the window.

        Returns:
            ``(snapshots ascending by report date, quarters no fund report could be read
            for, filing lists that could not be read)``.
        """
        quarters = quarter_ends(self.start, self.end)
        earliest_filing = quarters[0] - timedelta(days=92)
        filings_by_fund: list[list[Filing]] = []
        listing_errors: list[str] = []
        for fund in self.funds:
            listed = list_filings(
                self.client, fund, earliest_filing, self.end + MAX_FILING_LAG, listing_errors
            )
            for filing in listed:
                read_filing_index(self.client, filing)
            filings_by_fund.append(listed)

        snapshots: list[Snapshot] = []
        missing: list[date] = []
        for quarter_end in quarters:
            snapshot = self._snapshot_for(quarter_end, filings_by_fund)
            if snapshot is None:
                missing.append(quarter_end)
                continue
            log.info(
                "membership.snapshot",
                index=self.index,
                period=str(snapshot.period),
                fund=snapshot.filing.fund.label,
                form=snapshot.filing.form,
                stocks=len(snapshot.holdings),
            )
            snapshots.append(snapshot)
        return snapshots, missing, listing_errors

    def _snapshot_for(
        self, quarter_end: date, filings_by_fund: list[list[Filing]]
    ) -> Snapshot | None:
        """Read the membership for one calendar quarter, trying the funds in order.

        Args:
            quarter_end: Last day of the quarter.
            filings_by_fund: Each fund's filings, report dates filled in, in fund order.

        Returns:
            The snapshot from the latest report in the quarter whose number of stocks is
            plausible for its fund, or None if there is none.
        """
        quarter_start = quarter_end - timedelta(days=91)
        for filings in filings_by_fund:
            in_quarter = [
                (filing.period, filing)
                for filing in filings
                if filing.period is not None and quarter_start < filing.period <= quarter_end
            ]
            for period, filing in sorted(in_quarter, key=lambda item: item[0], reverse=True):
                holdings = parse_holdings(self.client, filing)
                fund = filing.fund
                if fund.min_holdings <= len(holdings) <= fund.max_holdings:
                    return Snapshot(period, filing, holdings)
                log.info(
                    "membership.report_rejected",
                    fund=fund.label,
                    form=filing.form,
                    period=str(period),
                    stocks=len(holdings),
                )
        return None

    def build(self) -> IndexMembership:
        """Reconstruct the index membership over the window.

        Returns:
            One member per company and membership span, with the reports read and the
            warnings collected on the way.
        """
        snapshots, missing_quarters, listing_errors = self.snapshots()
        warnings: list[str] = list(listing_errors)
        if missing_quarters:
            warnings.append(
                f"no usable fund report for {len(missing_quarters)} quarter(s): "
                + ", ".join(str(quarter) for quarter in missing_quarters)
            )
        if not snapshots:
            return IndexMembership(
                self.index, self.index_name, self.start, self.end, [], [], warnings
            )

        matcher = CikMatcher.from_edgar(self.client)
        tickers = HistoricalTickers(self.client, matcher.ticker_today)

        # Each holding gets a key: its CIK, or its normalised name when it matched none.
        members_by_period: list[tuple[date, set[str]]] = []
        name_by_key: dict[str, str] = {}
        match_by_key: dict[str, str] = {}
        cusip_by_key: dict[str, str] = {}
        isin_by_key: dict[str, str] = {}
        unmatched: Counter[str] = Counter()
        for snapshot in snapshots:
            keys = set()
            for holding in snapshot.holdings:
                name = clean_name(holding.name)
                match = matcher.match(holding.name, snapshot.period)
                if match.cik is None:
                    unmatched[f"{holding.name} ({match.quality})"] += 1
                    key = UNMATCHED_PREFIX + (name_keys(name)[0] or name.upper())
                else:
                    key = match.cik
                keys.add(key)
                name_by_key[key] = name  # the latest snapshot wins
                match_by_key[key] = match.quality
                cusip_by_key[key] = holding.cusip or cusip_by_key.get(key, "")
                isin_by_key[key] = holding.isin or isin_by_key.get(key, "")
            members_by_period.append((snapshot.period, keys))

        dates = [period for period, _ in members_by_period]
        intervals = intervals_from_snapshots(members_by_period)
        extended = extend_spans(intervals, dates)

        # One ticker per company, read around the middle of its time in the window.
        presence: dict[str, list[date]] = {}
        for period, keys in members_by_period:
            for key in keys:
                if not key.startswith(UNMATCHED_PREFIX):
                    presence.setdefault(key, []).append(period)
        ticker_by_cik: dict[str, str] = {}
        source_by_cik: dict[str, str] = {}
        owner_by_ticker: dict[str, str] = {}
        by_presence = sorted(presence.items(), key=lambda item: -len(item[1]))
        for done, (cik, seen) in enumerate(by_presence, start=1):
            if done % 100 == 0:
                log.info("membership.tickers", index=self.index, done=done, total=len(presence))
            ticker, how = tickers.lookup(cik, seen[len(seen) // 2])
            source_by_cik[cik] = how
            if ticker is None:
                warnings.append(f"no ticker found for {name_by_key[cik]} (CIK {cik})")
                continue
            if ticker in owner_by_ticker:
                other = owner_by_ticker[ticker]
                warnings.append(
                    f"ticker {ticker} found for both {name_by_key[other]} (CIK {other}) and "
                    f"{name_by_key[cik]} (CIK {cik})"
                )
            ticker_by_cik[cik] = ticker
            owner_by_ticker.setdefault(ticker, cik)

        members = []
        for (key, valid_from, last_seen), (_, _, valid_to) in zip(
            intervals, extended, strict=True
        ):
            member_cik = None if key.startswith(UNMATCHED_PREFIX) else key
            profile = tickers.profile(member_cik) if member_cik else CompanyProfile()
            members.append(
                Member(
                    index=self.index,
                    name=name_by_key[key],
                    ticker=ticker_by_cik.get(member_cik) if member_cik else None,
                    cik=member_cik,
                    valid_from=valid_from,
                    valid_to=valid_to,
                    last_seen=last_seen if last_seen is not None else dates[-1],
                    ticker_source=source_by_cik.get(member_cik, "") if member_cik else "",
                    name_match=match_by_key[key],
                    sec_name=profile.name,
                    tickers_today="|".join(profile.tickers),
                    exchanges_today="|".join(profile.exchanges),
                    sic=profile.sic,
                    sic_description=profile.sic_description,
                    state_of_incorporation=profile.state_of_incorporation,
                    cusip=cusip_by_key.get(key, ""),
                    isin=isin_by_key.get(key, ""),
                )
            )
        members.sort(key=lambda member: (member.name.lower(), member.valid_from))

        if unmatched:
            warnings.insert(
                0,
                f"{len(unmatched)} fund-report names matched no CIK (kept without CIK and "
                "ticker): " + "; ".join(name for name, _ in unmatched.most_common()),
            )
        reports = [
            Report(
                period=snapshot.period,
                fund=snapshot.filing.fund.label,
                form=snapshot.filing.form,
                url=snapshot.filing.index_url,
                stocks=len(snapshot.holdings),
            )
            for snapshot in snapshots
        ]
        log.info(
            "membership.built",
            index=self.index,
            reports=len(reports),
            members=len(members),
            with_cik=sum(member.cik is not None for member in members),
            with_ticker=sum(member.ticker is not None for member in members),
        )
        return IndexMembership(
            self.index, self.index_name, self.start, self.end, members, reports, warnings
        )


def get_index_membership(
    index: str,
    start: date | str,
    end: date | str,
    *,
    user_agent: str | None = None,
    output: str | Path | None = None,
    cache_dir: str | Path | None = None,
    funds_csv: str | Path | None = None,
) -> IndexMembership:
    """Reconstruct an index's membership over a date window.

    Args:
        index: Index id from the fund table, e.g. ``"sp500"``.
        start: Window start, inclusive (date or ISO string).
        end: Window end, inclusive (date or ISO string).
        user_agent: SEC contact string, e.g. ``"Jane Doe jane@example.com"``; read from
            ``SEC_USER_AGENT`` (environment or ``.env``) when omitted.
        output: CSV file to write the members to; nothing is written when omitted.
        cache_dir: Where EDGAR responses are cached; ``cache`` when omitted.
        funds_csv: Index -> fund table to use instead of the bundled one.

    Returns:
        The membership.
    """
    settings: Settings = load_settings(user_agent, cache_dir)
    funds = funds_for(index, funds_csv)
    with edgar_client(settings) as client:
        membership = MembershipBuilder(funds, _as_date(start), _as_date(end), client).build()
    if output is not None:
        membership.to_csv(output)
    return membership
