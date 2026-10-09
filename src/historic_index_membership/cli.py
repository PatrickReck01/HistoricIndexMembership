"""Command line interface: ``index-membership list`` and ``index-membership build``.

Examples::

    index-membership list
    index-membership build sp500 --start 2010-01-01 --end 2013-12-31 --output sp500.csv
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

import structlog

from historic_index_membership.funds import load_index_funds
from historic_index_membership.membership import IndexMembership, get_index_membership


def _configure_logging(verbose: bool) -> None:
    """Send log lines to stderr, at INFO (or DEBUG with ``--verbose``).

    Args:
        verbose: Also log every HTTP request.
    """
    level = logging.DEBUG if verbose else logging.INFO
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(sys.stderr),
    )


def _list_indices(funds_csv: str | None) -> None:
    """Print every index in the fund table with its funds.

    Args:
        funds_csv: Fund table to read; the bundled one when None.
    """
    table = load_index_funds(funds_csv)
    width = max(len(index_id) for index_id in table)
    for index_id, funds in table.items():
        tickers = ", ".join(fund.fund_ticker for fund in funds)
        print(f"{index_id:<{width}}  {funds[0].index_name}  ({tickers})")


def _print_summary(membership: IndexMembership, output: str | None) -> None:
    """Print what a build produced.

    Args:
        membership: The result.
        output: Where it was written, or None.
    """
    members = membership.members
    print(f"{membership.index_name}, {membership.start} to {membership.end}")
    if membership.reports:
        first, last = membership.reports[0].period, membership.reports[-1].period
        print(f"  fund reports read:  {len(membership.reports)} ({first} to {last})")
    print(f"  membership spans:   {len(members)}")
    print(f"  with CIK / ticker:  {sum(m.cik is not None for m in members)} / "
          f"{sum(m.ticker is not None for m in members)}")
    print(f"  members on {membership.end}: {len(membership.on(membership.end))}")
    for warning in membership.warnings:
        print(f"  warning: {warning[:300]}{'...' if len(warning) > 300 else ''}")
    if output:
        print(f"Written to {output}")
    else:
        print("Pass --output members.csv to write the full table.")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        The parser with the ``list`` and ``build`` commands.
    """
    parser = argparse.ArgumentParser(
        prog="index-membership",
        description="Historic index membership from the SEC holdings reports of index funds.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    funds_help = "index -> fund table to use instead of the bundled one"
    list_command = commands.add_parser("list", help="list the available indices")
    list_command.add_argument("--funds-csv", help=funds_help)

    build = commands.add_parser("build", help="reconstruct an index's membership over a window")
    build.add_argument("index", help="index id, e.g. sp500 (see `index-membership list`)")
    build.add_argument(
        "--start", required=True, type=date.fromisoformat, help="window start, YYYY-MM-DD"
    )
    build.add_argument(
        "--end", required=True, type=date.fromisoformat, help="window end, YYYY-MM-DD"
    )
    build.add_argument("--output", "-o", help="CSV file to write the members to")
    build.add_argument(
        "--user-agent",
        help='SEC contact string, e.g. "Jane Doe jane@example.com" '
        "(default: $SEC_USER_AGENT or .env)",
    )
    build.add_argument("--cache-dir", help="where EDGAR responses are cached (default: ./cache)")
    build.add_argument("--funds-csv", help=funds_help)
    build.add_argument("--verbose", "-v", action="store_true", help="log every HTTP request")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command line interface.

    Args:
        argv: Arguments without the program name; ``sys.argv[1:]`` when None.

    Returns:
        The exit code.
    """
    args = build_parser().parse_args(argv)
    if args.command == "list":
        _list_indices(args.funds_csv)
        return 0

    _configure_logging(args.verbose)
    try:
        membership = get_index_membership(
            args.index,
            args.start,
            args.end,
            user_agent=args.user_agent,
            output=args.output,
            cache_dir=args.cache_dir,
            funds_csv=args.funds_csv,
        )
    except (RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    _print_summary(membership, args.output)
    return 0
