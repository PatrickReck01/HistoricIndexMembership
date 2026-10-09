<h1 align="center">📊 Historic Index Membership</h1>

![Status: under development](https://img.shields.io/badge/Status-under%20development-orange)
![Indices: 15](https://img.shields.io/badge/Indices-15-blue)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![Data: SEC EDGAR](https://img.shields.io/badge/Data%20Source-SEC%20EDGAR-lightgrey)
![License: MIT](https://img.shields.io/badge/License-MIT-green)

Point-in-time membership of stock indices such as the S&P 500, the Dow Jones Industrial
Average or the Russell 2000, reconstructed for free from the SEC filings of the index
funds that replicate them.

```bash
index-membership build sp500 --start 2010-01-01 --end 2013-12-31 --output sp500.csv
```
## Motivation:

Historic index membership (which companies were in an index on a given day) is the basis of
any survivorship-bias-free backtest or event study. It is also surprisingly hard to get
for free: index providers sell it, and public sources show today's members and, at best,
an incomplete list of changes.

Index funds, however, have to publish their complete portfolio with the SEC several times a
year. A fund that fully replicates an index holds, on its report date, exactly the index.
This project reads those reports from [SEC EDGAR](https://www.sec.gov/edgar), matches every
holding to the company's SEC identity (CIK) and its ticker at the time, and turns the
reports into dated membership spans.

## How it works

For every calendar quarter of the requested window, the most recent holdings report of the
index's preferred fund is read. If that fund has none, or it cannot be parsed, the next fund
for the index is tried. These are the filings used:

| Form | Filed by | Content | Typical interval |
|---|---|---|---|
| **NPORT-P** | all registered funds, 2019 – today | full holdings as XML | every fiscal quarter |
| **N-Q** | open-end funds, 2004 – 2019 | full holdings, fiscal Q1 and Q3 | twice a year |
| **N-CSR / N-CSRS** | open-end funds, 2003 – today | annual / semi-annual report with full holdings | twice a year |
| **N-30D / N-30B-2** | unit investment trusts (SPY, DIA, QQQ) | annual / semi-annual report with full holdings | once or twice a year |

Together, N-Q and N-CSR give one report per quarter before 2019, and NPORT-P gives one per
quarter after. Reports are filed 60–70 days after the date they describe. The full method is in
[docs/METHOD.md](docs/METHOD.md).

> ⚠️ **Warning: this is a reconstruction, not an official constituent list.**
>
> Membership is only observed on report dates, usually once per quarter (and only twice a year for some trusts before 2019).
>
> - Changes between two reports are not seen directly. An addition or removal shows up at the next report, so the true change date can be up to one reporting interval earlier than the output says.
> - A company that joins and leaves the index between two reports is missed entirely.
> - Funds may hold a removed stock for a few days around a rebalance, or buy a new one late.
> - Reports before 2019 are HTML or plain text and are parsed heuristically. Implausible reports are rejected, but a few lost or stray rows per report are possible.
>
> Use it where quarter-level precision is good enough, and check critical dates against another source.

## SEC User-Agent required

> ℹ️ **Important: an EDGAR contact identity is required.**
>
> The SEC requires every automated request to EDGAR to identify who is making it, with a name and a contact email ([SEC fair access policy](https://www.sec.gov/os/accessing-edgar-data)). Requests without one are rejected, so this tool refuses to run without one.

Provide yours in any of these ways (the first one found wins):

1. On the command line: `--user-agent "Jane Doe jane@example.com"`
2. In Python: `get_index_membership(..., user_agent="Jane Doe jane@example.com")`
3. As an environment variable: `export SEC_USER_AGENT="Jane Doe jane@example.com"`
4. In a `.env` file in your working directory. Copy [.env.example](.env.example) to `.env`
   and fill it in:

   ```bash
   cp .env.example .env
   ```

## Available indices

The index-to-fund table is a plain CSV file:
[`src/historic_index_membership/data/index_funds.csv`](src/historic_index_membership/data/index_funds.csv).
It currently covers 15 US indices. Some of them:

| Index | `index` id | Funds used (in order of preference) |
|---|---|---|
| S&P 500 | `sp500` | IVV, VOO, PEOPX, SPY |
| S&P MidCap 400 | `sp400` | IJH, IVOO, PESPX |
| S&P SmallCap 600 | `sp600` | IJR, VIOO, DISSX |
| S&P 100 | `sp100` | OEF |
| Dow Jones Industrial Average | `djia` | DIA |
| Nasdaq-100 | `nasdaq100` | QQQ, USNQX, QQQM |
| Russell 1000 | `russell1000` | IWB, VONE |
| Russell 2000 | `russell2000` | IWM, VTWO |
| Russell Midcap | `russellmidcap` | IWR |

Run `index-membership list` for all of them. S&P 500 Growth/Value (from 2006) and the
Russell 1000/2000 Growth/Value indices are also included. Each fund's reports were checked
against EDGAR from 2000 to 2026. A fund's coverage period is in the CSV's `notes` column.

The Russell 3000 is deliberately missing: its ETFs (IWV, VTHR) hold a sampled subset of
the index, about 2,600 of roughly 3,000 stocks, so they cannot give the exact membership.

**Adding an index** takes one row per fund in the CSV. Pass your own file with `--funds-csv`,
or contribute it to the bundled one. The columns are:

| Column | Meaning |
|---|---|
| `index_id`, `index_name` | Id used on the command line, readable name |
| `priority` | 1 is tried first, higher numbers fill quarters the others miss |
| `fund_ticker`, `fund_name` | For logs and the report list |
| `series_id` | EDGAR series id (`S0000…`); empty for a unit investment trust |
| `registrant_cik` | CIK of the trust that files for the fund |
| `heading_pattern` | Case-insensitive regex for the fund's heading in pre-2019 reports; spaces also match no space (can be left empty if you only need 2019 on) |
| `values_in_thousands` | `true` if those reports state values in $000 (Vanguard) |
| `min_holdings`, `max_holdings` | Plausible number of stocks; reports outside are rejected |

A fund's series id and trust CIK are listed in EDGAR's
[fund ticker file](https://www.sec.gov/files/company_tickers_mf.json). Only funds that **fully
replicate** their index belong in the table. A fund that samples its index (holds a
representative subset) gives an incomplete membership list.

## Installation

Requires Python 3.11 or newer.

```bash
git clone https://github.com/PatrickReck01/historic-index-membership.git
cd historic-index-membership
pip install .
```

This installs the package and the `index-membership` command. To install only the
dependencies, use `pip install -r requirements.txt`. For development, use
`pip install -e ".[dev]"`.

## Usage

### Command line

```bash
# Which indices are available?
index-membership list

# S&P 500 membership from 2010 to 2013, written to a CSV file
index-membership build sp500 --start 2010-01-01 --end 2013-12-31 --output sp500_2010_2013.csv

# The same, with the user agent given inline and a custom cache location
index-membership build russell2000 --start 2015-01-01 --end 2015-12-31 \
    --user-agent "Jane Doe jane@example.com" --cache-dir ~/.cache/index-membership -o r2000.csv
```

`python -m historic_index_membership` works the same way as `index-membership`. Without
`--output`, a summary is printed instead of the table.

### Python

```python
from historic_index_membership import get_index_membership, list_indices

print(list_indices())  # {'sp500': 'S&P 500', 'sp400': 'S&P MidCap 400', ...}

membership = get_index_membership(
    "sp500",
    start="2010-01-01",
    end="2013-12-31",
    user_agent="Jane Doe jane@example.com",  # optional if SEC_USER_AGENT is set
    output="sp500_2010_2013.csv",  # optional
)

# Who was in the index on a given day?
members = membership.on("2012-06-30")
print(len(members), members[0].name, members[0].ticker, members[0].cik)

# Everything that was read, and what could not be resolved
for report in membership.reports:
    print(report.period, report.fund, report.form, report.stocks)
print(membership.warnings)
```

### Output

One row per company and membership span. A company that left and rejoined the index has
two rows.

| Column | Meaning |
|---|---|
| `index` | Index id |
| `name` | Company name as the fund reports write it |
| `ticker` | Ticker at the time, if one could be found |
| `cik` | SEC Central Index Key, if the name could be matched |
| `valid_from` | First report date the company was held on |
| `valid_to` | Day before the first report it was missing from; empty if still held at the window end |
| `last_seen` | Last report date it was held on |
| `ticker_source` | `xbrl` (its 10-K of the time), `today` (EDGAR's current list), `press_release` (its 8-Ks) or `none` |
| `name_match` | `exact`, `loose`, `compact`, `ambiguous` or `none`: how the name was matched to the CIK (see [METHOD.md](docs/METHOD.md)) |
| `sec_name` | The company's current name on EDGAR |
| `tickers_today`, `exchanges_today` | Today's tickers and exchanges, if it still trades |
| `sic`, `sic_description` | Industry classification |
| `state_of_incorporation` | Two-letter code |
| `cusip`, `isin` | Security identifiers, from 2019 reports on (NPORT-P) |

Everything except the fund-report fields comes from EDGAR data that is fetched anyway, so
it adds no requests. A holding whose name matches no CIK is kept with empty `cik` and
`ticker` and is listed in `membership.warnings`.

### Caching and run time

Every EDGAR response is cached in `./cache` (change it with `--cache-dir`, `cache_dir=` or
`INDEX_MEMBERSHIP_CACHE_DIR`). Filings never change once filed, so a second run only fetches
the filing lists again. The first run is limited by the SEC's fair-access rate (this tool
stays at 8 requests per second). Most of that time goes into looking up each company's
historic ticker, so it grows with the number of companies: a few minutes for the Dow, about
10 minutes for four years of the S&P 400, and longest for the Russell 2000.

## Project structure

```
├── src/historic_index_membership/
│   ├── data/index_funds.csv   index → fund table
│   ├── cli.py                 the index-membership command
│   ├── membership.py          snapshots → membership spans, CSV output
│   ├── funds.py               reading the fund table
│   ├── filings.py             listing a fund's reports on EDGAR
│   ├── holdings.py            parsing holdings out of HTML, text and XML reports
│   ├── names.py               matching holding names to SEC CIKs
│   ├── tickers.py             the ticker a company had at the time
│   ├── sec.py                 EDGAR company data
│   ├── net.py                 rate-limited, caching HTTP client
│   └── config.py              user agent and cache settings
├── tests/                     offline tests against canned EDGAR pages
├── docs/METHOD.md             the method in detail
├── .env.example               template for your SEC user agent
├── requirements.txt
└── pyproject.toml
```

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
mypy
```

The tests run offline.

## License

[MIT](LICENSE). The data comes from SEC EDGAR, which is public. Please follow the SEC's
[fair access rules](https://www.sec.gov/os/accessing-edgar-data).
