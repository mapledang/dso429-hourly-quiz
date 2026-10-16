"""Parsing option strings and matching a computed value to one option."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

LETTERS = ("A", "B", "C", "D")

_NUM_RE = re.compile(r"[-+−–]?\s*\$?\s*\d[\d,]*(?:\.\d+)?|[-+−–]?\s*\$?\s*\.\d+")
_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"], start=1)
}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})
_DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\.?,?\s+(\d{4})")


def parse_number(text: str) -> float | None:
    """Return the single numeric value in an option like '$6,608.61', '-15.1%', '667 units'.

    Returns None when the string contains no number or more than one number
    (e.g. a ratio '2:5'), so callers never silently compare the wrong quantity.
    """
    s = text.strip().replace("−", "-").replace("–", "-")
    if s.startswith("(") and s.endswith(")"):  # accounting negative: ($1.00)
        s = "-" + s[1:-1]
    found = _NUM_RE.findall(s)
    if len(found) != 1:
        return None
    raw = found[0].replace("$", "").replace(",", "").replace(" ", "")
    try:
        return float(raw)
    except ValueError:
        return None


def decimals_shown(text: str) -> int:
    m = re.search(r"\d\.(\d+)", text)
    return len(m.group(1)) if m else 0


def parse_date(text: str) -> dt.date | None:
    m = _DATE_RE.search(text)
    if not m:
        return None
    day, month_name, year = m.groups()
    month = _MONTHS.get(month_name.lower()) or _MONTHS.get(month_name.lower()[:3])
    if not month:
        return None
    try:
        return dt.date(int(year), month, int(day))
    except ValueError:
        return None


@dataclass
class Match:
    letter: str | None
    status: str  # "exact" | "close" | "ambiguous" | "no_match"
    detail: str = ""


def match_number(value: float, options: list[str], *, percent: bool = False) -> Match:
    """Pick the option whose displayed number equals `value` at the option's own precision.

    `value` is in display units (e.g. 15.1 for 15.1% when percent=True).
    An option "matches" when |option - value| <= half of its last displayed digit
    (plus float slack). Exactly one matching option -> exact. Otherwise fall back to
    the nearest option only if it is clearly separated from the runner-up.
    """
    parsed = [parse_number(o) for o in options]
    if any(p is None for p in parsed):
        return Match(None, "no_match", f"unparseable options {options!r}")
    exact = []
    for letter, opt, p in zip(LETTERS, options, parsed):
        tol = 0.5 * 10 ** (-decimals_shown(opt)) + 1e-9 * max(1.0, abs(value))
        if abs(p - value) <= tol:
            exact.append(letter)
    if len(exact) == 1:
        return Match(exact[0], "exact")
    if len(exact) > 1:
        return Match(None, "ambiguous", f"value {value} matches {exact}")
    dists = sorted((abs(p - value), letter) for letter, p in zip(LETTERS, parsed))
    best, second = dists[0], dists[1]
    scale = max(abs(value), 1e-9)
    # Accept a near miss (different rounding convention) only if it is within 0.5%
    # of the value and the runner-up is at least 10x further away.
    if best[0] / scale <= 0.005 and second[0] >= 10 * max(best[0], 1e-12):
        return Match(best[1], "close", f"value {value}, nearest off by {best[0]:.6g}")
    return Match(None, "no_match", f"value {value} not among {options!r}")


def match_date(value: dt.date, options: list[str]) -> Match:
    hits = [letter for letter, o in zip(LETTERS, options) if parse_date(o) == value]
    if len(hits) == 1:
        return Match(hits[0], "exact")
    if hits:
        return Match(None, "ambiguous", f"date {value} matches {hits}")
    return Match(None, "no_match", f"date {value} not among {options!r}")
