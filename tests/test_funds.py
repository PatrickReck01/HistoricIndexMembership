"""The index -> fund table."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from historic_index_membership import list_indices
from historic_index_membership.funds import REQUIRED_COLUMNS, funds_for, load_index_funds


def test_the_bundled_table_is_well_formed() -> None:
    table = load_index_funds()
    assert {"sp500", "sp400", "sp600", "djia", "nasdaq100", "russell2000"} <= set(table)
    for index_id, funds in table.items():
        priorities = [fund.priority for fund in funds]
        assert priorities == sorted(set(priorities)), index_id
        assert len({fund.index_name for fund in funds}) == 1, index_id
        for fund in funds:
            re.compile(fund.heading_pattern)
            assert 0 < fund.min_holdings <= fund.max_holdings, fund.fund_ticker
            assert fund.series_id is None or re.fullmatch(r"S\d{9}", fund.series_id)


def test_funds_come_in_priority_order(tmp_path: Path) -> None:
    path = tmp_path / "funds.csv"
    path.write_text(
        ",".join(REQUIRED_COLUMNS)
        + "\n"
        + "x,X Index,2,BBB,Second,S000000002,,X FUND,false,1,10\n"
        + "x,X Index,1,AAA,First,,0000123,X FUND,true,1,10\n",
        encoding="utf-8",
    )
    funds = funds_for("x", path)
    assert [fund.fund_ticker for fund in funds] == ["AAA", "BBB"]
    assert funds[0].registrant_cik == "123" and funds[0].series_id is None
    assert funds[0].values_in_thousands is True
    assert list_indices(str(path)) == {"x": "X Index"}


def test_a_fund_needs_a_series_or_a_registrant(tmp_path: Path) -> None:
    path = tmp_path / "funds.csv"
    path.write_text(
        ",".join(REQUIRED_COLUMNS) + "\nx,X Index,1,AAA,First,,,X FUND,false,1,10\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="line 2"):
        load_index_funds(path)


def test_missing_columns_and_unknown_indices_are_reported(tmp_path: Path) -> None:
    path = tmp_path / "funds.csv"
    path.write_text("index_id,fund_ticker\nx,AAA\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing columns"):
        load_index_funds(path)
    with pytest.raises(ValueError, match="unknown index 'sp100000'"):
        funds_for("sp100000")
