"""Matching the security names in fund reports to SEC CIKs.

Fund reports before 2019 carry names only ("J.B. Hunt Transport Services Inc.",
"Aaron's", "Warnaco Group Inc. (The)"). They are matched against every name any EDGAR filer
has ever used (``cik-lookup-data.txt``), which also covers companies that no longer exist.

Three keys are tried per name, in order:

* ``exact``: punctuation, share classes, state tags and legal forms dropped;
* ``loose``: also ignores generic words, word order and spacing between initials, because
  EDGAR writes "J.B. Hunt Transport" as "HUNT J B TRANSPORT";
* ``compact``: the loose key's letters alone, so that spacing inside words stops mattering
  ("du Pont" / "DUPONT"). Only for names of ten letters or more, and only trusted for a CIK
  that was filing as a listed company at the time.

A listed company files a 10-K or 10-Q every quarter; its namesakes -- an old shell, a
subsidiary, a predecessor, a filer of a single no-action letter -- do not. So a CIK is only
taken as is when it is the key's one candidate *and* was filing around the report date.
Several candidates are narrowed to those filing at the time, then to the one whose current
EDGAR name still has the same key (Travelers Group, CIK 831001, is Citigroup today), then
to those still trading. A name that stays ambiguous is left unmatched: a missing company
costs less than a company wearing another one's name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import structlog

from historic_index_membership.config import SUBMISSIONS_URL_TEMPLATE
from historic_index_membership.net import CachedHttpClient
from historic_index_membership.sec import load_company_tickers

log = structlog.get_logger(__name__)

CIK_LOOKUP_URL = "https://www.sec.gov/Archives/edgar/cik-lookup-data.txt"
LEGAL_WORDS = frozenset(
    {
        "INC",
        "INCORPORATED",
        "CORP",
        "CORPORATION",
        "CO",
        "COS",
        "COMPANY",
        "COMPANIES",
        "LTD",
        "LIMITED",
        "PLC",
        "LP",
        "LLC",
        "NV",
        "SA",
        "AG",
        "THE",
        "DEL",
        "DE",
        "NEW",
        "NVS",
    }  # NVS: non-voting shares
)
GENERIC_WORDS = frozenset({"GROUP", "HOLDINGS", "HOLDING", "INTERNATIONAL", "INTL", "AND"})
PERIODIC_FORMS = frozenset({"10-K", "10-K405", "10-KT", "10-Q", "10-QT", "20-F", "40-F"})
# A CIK counts as a listed company at a date if it filed a periodic report this close to it.
# Quarterly reports come every three months; the margin leaves room for a late filer.
ACTIVE_MARGIN = timedelta(days=200)
# Fewest letters a name needs before its letters alone are trusted as a key.
MIN_COMPACT_LETTERS = 10

# EDGAR state tags: "/DE/", "\\MD\\", "INC / MA", "INC/MN".
_STATE_TAG = re.compile(r"[/\\][A-Z]{2,4}[/\\]|\s*[/\\]\s*[A-Z]{2,4}$")
_SHARE_CLASS = re.compile(r"\b(CLASS|CL|SERIES|SER)\b\.? ?[A-Z]\b")
_INITIALS = re.compile(r"\b([A-Z]) (?=[A-Z]\b)")


def name_keys(name: str) -> tuple[str, str]:
    """Normalise a company name into a strict and a loose lookup key.

    Args:
        name: A company name as a fund report or EDGAR writes it.

    Returns:
        ``(strict, loose)``. The strict key drops punctuation, share classes, state tags and
        legal forms; the loose key also drops generic words, merges initials and ignores
        word order.
    """
    text = name.upper().replace("&", " AND ").replace("\u2019", "'")  # curly apostrophe
    text = _STATE_TAG.sub(" ", text)
    text = _SHARE_CLASS.sub(" ", text)
    text = re.sub(r"['.]", "", text)
    words = [word for word in re.sub(r"[^A-Z0-9]+", " ", text).split() if word not in LEGAL_WORDS]
    strict = " ".join(words)
    merged = _INITIALS.sub(r"\1", strict).split()
    loose = " ".join(sorted(word for word in merged if word not in GENERIC_WORDS))
    return strict, loose


def compact_key(loose: str) -> str:
    """Reduce a loose key to its letters, sorted, so that spacing inside words stops mattering.

    Args:
        loose: A loose key from :func:`name_keys`.

    Returns:
        The sorted letters, or an empty string for a name too short to trust this way.
    """
    letters = loose.replace(" ", "")
    return "".join(sorted(letters)) if len(letters) >= MIN_COMPACT_LETTERS else ""


@dataclass(frozen=True)
class NameMatch:
    """The CIK a fund-report name resolved to.

    Attributes:
        cik: The CIK without leading zeros, or None if unmatched.
        quality: ``exact``, ``loose``, ``compact``, ``ambiguous`` or ``none``.
        candidates: Every CIK the name's key pointed at.
    """

    cik: str | None
    quality: str
    candidates: tuple[str, ...] = ()


@dataclass
class CikMatcher:
    """Name -> CIK lookup over every name EDGAR has on file.

    Attributes:
        strict: Strict key -> CIKs.
        loose: Loose key -> CIKs.
        compact: Compact key -> CIKs.
        traded: CIKs with a ticker today (``company_tickers.json``).
        ticker_today: CIK -> its first ticker today.
        client: EDGAR client used to read when candidates were filing; None disables the
            checks that need it.
    """

    strict: dict[str, set[str]]
    loose: dict[str, set[str]]
    compact: dict[str, set[str]]
    traded: set[str]
    ticker_today: dict[str, str]
    client: CachedHttpClient | None = None
    _periodic: dict[str, list[date]] = field(default_factory=dict)
    _current_name: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_names(
        cls,
        names: list[tuple[str, str]],
        ticker_today: dict[str, str],
        client: CachedHttpClient | None = None,
    ) -> CikMatcher:
        """Build the lookup from ``(name, cik)`` pairs.

        Args:
            names: Every known ``(name, cik)`` pair; a CIK may appear under many names.
            ticker_today: CIK -> ticker for companies trading today.
            client: EDGAR client for resolving ambiguous names; None disables it.

        Returns:
            The matcher.
        """
        strict: dict[str, set[str]] = {}
        loose: dict[str, set[str]] = {}
        compact: dict[str, set[str]] = {}
        for name, cik in names:
            strict_key, loose_key = name_keys(name)
            if strict_key:
                strict.setdefault(strict_key, set()).add(cik)
            if loose_key:
                loose.setdefault(loose_key, set()).add(cik)
            if compact_name := compact_key(loose_key):
                compact.setdefault(compact_name, set()).add(cik)
        return cls(strict, loose, compact, set(ticker_today), ticker_today, client)

    @classmethod
    def from_edgar(cls, client: CachedHttpClient) -> CikMatcher:
        """Build the lookup from EDGAR's company lists.

        Args:
            client: EDGAR client.

        Returns:
            The matcher.
        """
        names: list[tuple[str, str]] = []
        ticker_today: dict[str, str] = {}
        for company in load_company_tickers(client):
            ticker_today.setdefault(company.cik, company.ticker)
            names.append((company.title, company.cik))
        for line in client.fetch(CIK_LOOKUP_URL).text.splitlines():
            parts = line.rsplit(":", 2)  # "NAME:0000123456:"; names may contain colons
            if len(parts) == 3 and parts[1].isdigit():
                names.append((parts[0], str(int(parts[1]))))
        log.info("names.cik_lookup", names=len(names), traded=len(ticker_today))
        return cls.from_names(names, ticker_today, client)

    def _periodic_report_dates(self, cik: str) -> list[date]:
        """List the dates a CIK filed 10-Ks, 10-Qs and their foreign equivalents.

        Args:
            cik: CIK without leading zeros.

        Returns:
            Filing dates, from the submissions document and all its older pages. Empty if
            EDGAR has no submissions for the CIK.
        """
        if self.client is None:
            return []
        if cik in self._periodic:
            return self._periodic[cik]
        template = SUBMISSIONS_URL_TEMPLATE
        dates: list[date] = []
        try:
            payload: dict[str, Any] = self.client.fetch_json(template.format(cik=cik.zfill(10)))
            self._current_name[cik] = str(payload.get("name") or "")
            tables = [payload.get("filings", {}).get("recent", {})]
            base = template.rsplit("/", 1)[0]
            for page in payload.get("filings", {}).get("files", []):
                tables.append(self.client.fetch_json(f"{base}/{page['name']}"))
        except Exception:  # a missing submissions document just means "unknown"
            tables = []
        for table in tables:
            for form, filed in zip(
                table.get("form", []), table.get("filingDate", []), strict=False
            ):
                if str(form) in PERIODIC_FORMS and filed:
                    dates.append(date.fromisoformat(str(filed)))
        self._periodic[cik] = dates
        return dates

    def _active(self, cik: str, on: date) -> bool:
        """Whether a CIK was filing periodic reports, as a listed company does, around a date.

        Args:
            cik: CIK without leading zeros.
            on: The date.

        Returns:
            True if it filed a 10-K / 10-Q (or 20-F / 40-F) within ``ACTIVE_MARGIN`` of ``on``.
        """
        return any(abs(filed - on) <= ACTIVE_MARGIN for filed in self._periodic_report_dates(cik))

    def _still_named(self, cik: str, strict_key: str) -> bool:
        """Whether a CIK's current EDGAR name has the same strict key as a fund-report name.

        Args:
            cik: CIK without leading zeros; its submissions document has been read.
            strict_key: The fund-report name's strict key.

        Returns:
            True if EDGAR still names the company that way.
        """
        return name_keys(self._current_name.get(cik, ""))[0] == strict_key

    def _resolve(
        self, candidates: tuple[str, ...], strict_key: str, on: date
    ) -> tuple[str | None, bool]:
        """Pick one CIK from the candidates of a key.

        Args:
            candidates: The CIKs the key points at, ascending.
            strict_key: The fund-report name's strict key.
            on: Report date.

        Returns:
            ``(cik, active)``: the CIK chosen (None if it stays ambiguous) and whether it
            was filing as a listed company around ``on``.
        """
        active = [cik for cik in candidates if self._active(cik, on)]
        if len(candidates) == 1:
            return candidates[0], bool(active)
        if len(active) == 1:
            return active[0], True
        named = [cik for cik in active if self._still_named(cik, strict_key)]
        if len(named) == 1:
            return named[0], True
        traded = [cik for cik in (active or candidates) if cik in self.traded]
        if len(traded) == 1:
            return traded[0], bool(active)
        return None, False

    def match(self, name: str, on: date) -> NameMatch:
        """Find the CIK of a fund-report name.

        Args:
            name: The name as the fund writes it.
            on: Report date, used to tell same-named CIKs apart.

        Returns:
            The match. A key whose only candidate was not filing at the time is passed
            over for a later key with an active candidate, and used only if none has one.
        """
        strict_key, loose_key = name_keys(name)
        fallback: NameMatch | None = None
        for quality, ciks in (
            ("exact", self.strict.get(strict_key)),
            ("loose", self.loose.get(loose_key)),
            ("compact", self.compact.get(compact_key(loose_key))),
        ):
            if not ciks:
                continue
            candidates = tuple(sorted(ciks, key=int))
            cik, active = self._resolve(candidates, strict_key, on)
            if cik is not None and active:
                return NameMatch(cik, quality, candidates)
            if fallback is None and quality != "compact":
                status = "ambiguous" if cik is None else quality
                fallback = NameMatch(cik, status, candidates)
        return fallback or NameMatch(None, "none")
