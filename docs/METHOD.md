# Method

How a fund's SEC filings are turned into a dated index membership list, and where the
method's limits are. The [README](../README.md) covers usage.

## Why fund holdings

Free point-in-time constituent lists are hard to find. Index providers sell them, and
public sources such as Wikipedia show current members plus an incomplete change log at
best. What *is* free is the SEC's record of the funds that replicate an index. Every
registered fund files its complete portfolio several times a year, and a fund that fully
replicates its index holds, on its report date, exactly the index.

## Which filings

| Form | Filed by | Covers | Period |
|---|---|---|---|
| N-Q | open-end funds | fiscal Q1 and Q3 holdings | 2004 – 2019 |
| N-CSR / N-CSRS | open-end funds | annual / semi-annual report, including the Q4 and Q2 holdings | 2003 – today |
| N-30D / N-30B-2 | unit investment trusts (SPY, DIA, QQQ) and pre-2003 funds | annual / semi-annual report | 1990s – today |
| NPORT-P | all registered funds | holdings at each fiscal quarter end, as XML | 2019 – today |

Funds file these within 60–70 days of the report date. Because fiscal years differ between
fund families (iShares ends quarters in Mar/Jun/Sep/Dec, Vanguard's S&P funds in
Feb/May/Aug/Nov, BNY Mellon's in Jan/Apr/Jul/Oct), a second fund of the same index can fill
a quarter the first one misses.

## Pipeline

1. **Funds** (`funds.py`, [`index_funds.csv`](../src/historic_index_membership/data/index_funds.csv)).
   The requested index is looked up in the CSV, and its funds are tried in priority order.
2. **Filing lists** (`filings.py`). A fund's reports are listed by its EDGAR series id. Its
   trust's submissions document is read as well for anything filed before mid-2006, when
   EDGAR started tagging filings with series ids, and for unit investment trusts, which
   have no series.
3. **One snapshot per calendar quarter** of the window (`membership.py`), starting with the
   last report on or before the window start. The preferred fund's latest report in the
   quarter is used, but only if its number of stocks falls in the fund's
   `min_holdings`–`max_holdings` range. A report outside it was cut short, is a summary
   schedule (iShares' semi-annual reports since 2013), or ran two funds together. The next
   fund is then tried for the same quarter.
4. **Holdings** (`holdings.py`). NPORT-P is structured XML. Older reports are HTML or
   plain text, and one document often covers dozens of funds of one trust. The document
   is flattened into lines, the fund's section is found with its `heading_pattern`, and
   `name | shares | value` rows are read until the common stocks end.
5. **Names to CIKs** (`names.py`). Reports before 2019 carry names only. They are matched
   against every name EDGAR has on file (`cik-lookup-data.txt`), so companies that no
   longer exist still resolve. Three keys are tried in turn:
   - strict: legal forms and punctuation dropped;
   - loose: word order and initials also ignored, because EDGAR writes names like
     "HUNT J B TRANSPORT SERVICES INC";
   - compact: spacing inside words also ignored ("du Pont" / "DUPONT"), for long names only.

   A CIK is trusted when it was filing 10-Ks / 10-Qs around the report date, as a listed
   company does. A name several CIKs share goes to the one filing at the time, then to the
   one EDGAR still calls by that name. One that stays ambiguous is kept without a CIK.
6. **Spans.** The per-quarter sets are diffed into membership spans. Each span is
   stretched to the day before the first snapshot the company is missing from.
7. **Tickers of the time** (`tickers.py`). Fund reports have no tickers, and EDGAR's
   `company_tickers.json` only knows tickers of companies that still trade. Sources, in
   order:
   - the XBRL instance file name of the company's own 10-K, filed closest to its time in
     the index (`ads-20121231.xml`);
   - today's ticker for the same CIK;
   - the exchange tag in its 8-K press releases ("Sybase, Inc. (NYSE: SY)").

   The submissions document read here also gives each company's current EDGAR name,
   industry (SIC) and state of incorporation, which go into the output at no extra cost.

## Measured

**Reports.** For every fund in the table, one report from roughly every third year between
2000 and 2026 was parsed and its number of stocks compared with the index size. Every fund
matches its index in the years it existed and tracked it, except where noted in the CSV's
`notes` column. The Russell 3000 was dropped after this check: both of its ETFs hold a
sampled subset, about 2,600 of roughly 3,000 stocks.

**A full build, S&P 400, 2010–2013** (`index-membership build sp400 --start 2010-01-01
--end 2013-12-31`):

| | |
|---|---:|
| Quarterly reports read | 17 |
| Membership spans | 538 |
| with a CIK | 533 |
| with a ticker of the time | 529 (507 from XBRL, 20 from press releases, 2 from today's list) |
| Names matched to no CIK | 4 (2 ambiguous, 2 not found) |
| Members on 2013-12-31 | 400 |
| First run | about 11 minutes |

Companies without a ticker include Signature Bank, which files with the FDIC, not the SEC.
In TickerMatcher, where this code came from, the parser was also cross-checked against a
second fund in mid-2013. It agreed on 393 of ~400 S&P 400 names and 581 of ~600 S&P 600
names, and the differences were consistent with a month of index changes.

## Limitations

- **Dated to the report, not the day.** Membership is only observed on report dates,
  usually once a quarter and twice a year for unit investment trusts before 2019. A change
  between two reports shows up at the next report. A company added and removed again
  between two reports is never seen. Spans start at the first report that shows a company
  and are stretched to the day before the first report that no longer does, so the true
  dates can be up to a reporting interval earlier.
- **Replication is not perfect.** A fund may briefly hold a stock just removed from the
  index, or not yet hold a new one, around a rebalance. Large funds that replicate fully,
  as all funds in the table do, are within a handful of names of their index.
- **Parsing is heuristic before 2019.** HTML and text layouts vary across years and fund
  families. The `min_holdings`–`max_holdings` check rejects reports that clearly went
  wrong, but cannot catch a few lost or stray rows.
- **US-listed equities only.** CIKs and tickers come from SEC filings, so foreign
  companies that do not file with the SEC end up without a CIK or ticker.
- **Names, not identifiers, before 2019.** A holding whose name matches no CIK, or several
  equally well, is kept with an empty CIK and ticker rather than guessed.
