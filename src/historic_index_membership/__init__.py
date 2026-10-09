"""Historic index membership, reconstructed from the SEC holdings reports of index funds.

Free point-in-time constituent lists are hard to come by. What is free is the SEC's record
of the funds that replicate an index: every registered fund files its complete portfolio
several times a year, and a fund that holds every stock of its index holds, on its report
date, exactly the index. See ``docs/METHOD.md``.

Public API::

    from historic_index_membership import get_index_membership, list_indices

    membership = get_index_membership("sp500", "2010-01-01", "2013-12-31", output="sp500.csv")

Modules, in pipeline order:

* ``funds`` -- the index -> fund table (``data/index_funds.csv``);
* ``filings`` -- listing a fund's holdings reports on EDGAR and their report dates;
* ``holdings`` -- parsing the stock holdings out of N-Q / N-CSR / N-30D (HTML, text) and
  NPORT-P (XML);
* ``names`` -- matching a holding's name to a CIK;
* ``tickers`` -- the ticker a company traded under at the time, from its own filings;
* ``membership`` -- ties it together into membership spans.
"""

from historic_index_membership.funds import FundSource, load_index_funds
from historic_index_membership.membership import IndexMembership, Member, get_index_membership


def list_indices(funds_csv: str | None = None) -> dict[str, str]:
    """List the indices the fund table covers.

    Args:
        funds_csv: Fund table to read; the bundled one when None.

    Returns:
        Index id -> readable index name.
    """
    table = load_index_funds(funds_csv)
    return {index_id: funds[0].index_name for index_id, funds in table.items()}


__all__ = [
    "FundSource",
    "IndexMembership",
    "Member",
    "get_index_membership",
    "list_indices",
    "load_index_funds",
]
