"""Deterministic solvers for the business-arithmetic question families.

Each solver receives the question text and returns either None (does not apply)
or a Solution describing the method, the computed value and the matched option.
Question text is only ever matched with regular expressions; it is never executed.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass, field
from typing import Callable

from .numbers import Match, match_date, match_number, parse_date

NUM = r"\$?\s*(-?\d[\d,]*(?:\.\d+)?)"


def num(s: str) -> float:
    return float(s.replace(",", "").replace("$", "").strip())


@dataclass
class Solution:
    category: str
    method: str
    value: object
    match: Match
    notes: list[str] = field(default_factory=list)

    @property
    def letter(self) -> str | None:
        return self.match.letter


Solver = Callable[[str, list[str]], "Solution | None"]
SOLVERS: list[tuple[str, Solver]] = []


def solver(category: str):
    def deco(fn: Solver) -> Solver:
        SOLVERS.append((category, fn))
        return fn
    return deco


def _search(pattern: str, text: str):
    return re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)


# --- time value of money -------------------------------------------------------

_FREQ = {
    "annually": 1, "annual": 1, "yearly": 1,
    "semiannually": 2, "semi-annually": 2, "semiannual": 2, "semi-annual": 2,
    "quarterly": 4, "monthly": 12, "weekly": 52, "daily": 365,
}


@solver("compound_interest")
def compound_interest(q: str, opts: list[str]):
    m = _search(
        NUM + r".{0,60}?(?:grows|invested|deposited|earns|compounds).{0,40}?" + NUM
        + r"\s*%.{0,30}?compounded\s+([a-z-]+).{0,80}?(?:after|in|for)\s+" + NUM
        + r"\s*(years?|months?|quarters?)", q)
    if not m:
        return None
    principal, rate, freq_word, periods, unit = m.groups()
    p, r, n = num(principal), num(rate) / 100, num(periods)
    years = n if unit.lower().startswith("year") else n / 12 if unit.lower().startswith("month") else n / 4
    if freq_word.lower() == "continuously":
        value = p * math.exp(r * years)
        method = "FV = P*e^(r*t)"
    else:
        k = _FREQ.get(freq_word.lower())
        if k is None:
            return None
        value = p * (1 + r / k) ** (k * years)
        method = f"FV = P*(1+r/{k})^({k}*t)"
    return Solution("compound_interest", method, round(value, 6), match_number(value, opts))


@solver("simple_interest")
def simple_interest(q: str, opts: list[str]):
    m = _search(NUM + r".{0,60}?" + NUM + r"\s*%\s*simple interest.{0,80}?" + NUM + r"\s*years?", q)
    if not m:
        return None
    p, r, t = num(m.group(1)), num(m.group(2)) / 100, num(m.group(3))
    asks_interest = bool(_search(r"how much interest|interest earned|total interest", q))
    value = p * r * t if asks_interest else p * (1 + r * t)
    return Solution("simple_interest", "I = P*r*t" if asks_interest else "A = P*(1+r*t)",
                    value, match_number(value, opts))


# --- pricing ---------------------------------------------------------------------

@solver("discount")
def discount(q: str, opts: list[str]):
    m = _search(r"(?:listed|priced|costs?)\s+(?:at\s+)?" + NUM + r".{0,40}?discounted\s+by\s+" + NUM + r"\s*%", q)
    if not m:
        return None
    price, pct = num(m.group(1)), num(m.group(2))
    if _search(r"how much (?:is|does).*sav|discount amount|amount of the discount", q):
        value, method = price * pct / 100, "discount = price*p"
    else:
        value, method = price * (1 - pct / 100), "sale = price*(1-p)"
    return Solution("discount", method, value, match_number(value, opts))


@solver("markup")
def markup(q: str, opts: list[str]):
    m = _search(r"costs?\s+" + NUM + r".{0,60}?mark(?:ed)?[\s-]*up\s+(?:by\s+)?" + NUM + r"\s*%", q)
    if not m:
        return None
    cost, pct = num(m.group(1)), num(m.group(2))
    value = cost * (1 + pct / 100)
    return Solution("markup", "price = cost*(1+m)", value, match_number(value, opts))


@solver("sales_tax")
def sales_tax(q: str, opts: list[str]):
    m = _search(NUM + r".{0,60}?" + NUM + r"\s*%\s*(?:sales\s+)?tax", q)
    if not m or not _search(r"total|including tax|after tax", q):
        return None
    base, pct = num(m.group(1)), num(m.group(2))
    value = base * (1 + pct / 100)
    return Solution("sales_tax", "total = base*(1+t)", value, match_number(value, opts))


@solver("revenue")
def revenue(q: str, opts: list[str]):
    m = _search(r"sells\s+" + NUM + r"\s+units?\s+at\s+" + NUM + r"\s*(?:each|per unit|apiece)", q)
    if not m:
        return None
    value = num(m.group(1)) * num(m.group(2))
    return Solution("revenue", "revenue = units*price", value, match_number(value, opts))


@solver("currency_conversion")
def currency(q: str, opts: list[str]):
    m = _search(r"(?:for|of)\s+" + NUM + r".{0,40}?converted\s+to\s+([a-z ]+?)\s+at\s+" + NUM
                + r"\s+([a-z ]+?)\s+per\s+(?:one\s+|1\s+)?([a-z .]+?)[.?,]", q)
    if not m:
        return None
    amount, target, rate, unit_num, unit_den = m.groups()
    amount_f, rate_f = num(amount), num(rate)
    target, unit_num = target.strip().lower(), unit_num.strip().lower()
    # "R <target> per <source>" -> multiply; "R <source> per <target>" -> divide.
    if unit_num.rstrip("s") == target.rstrip("s"):
        value, method = amount_f * rate_f, "amount*rate"
    else:
        value, method = amount_f / rate_f, "amount/rate"
    return Solution("currency_conversion", method, value, match_number(value, opts))


# --- margins, averages, ratios ----------------------------------------------------

@solver("gross_margin")
def gross_margin(q: str, opts: list[str]):
    m = _search(NUM + r"\s+in\s+revenue.{0,40}?" + NUM + r"\s+in\s+cost of goods sold", q)
    if not m or not _search(r"gross margin", q):
        return None
    rev, cogs = num(m.group(1)), num(m.group(2))
    if rev == 0:
        return None
    value = (rev - cogs) / rev * 100
    return Solution("gross_margin", "(rev-cogs)/rev", value, match_number(value, opts, percent=True))


@solver("weighted_average")
def weighted_average(q: str, opts: list[str]):
    groups = re.findall(r"(\d[\d,]*)\s+\w+\s+averaging\s+" + NUM, q, flags=re.IGNORECASE)
    if len(groups) < 2 or not _search(r"overall average|combined average|weighted average", q):
        return None
    counts = [num(c) for c, _ in groups]
    avgs = [num(a) for _, a in groups]
    total = sum(counts)
    if total == 0:
        return None
    value = sum(c * a for c, a in zip(counts, avgs)) / total
    return Solution("weighted_average", "sum(n_i*avg_i)/sum(n_i)", value, match_number(value, opts))


@solver("break_even")
def break_even(q: str, opts: list[str]):
    m = _search(r"fixed costs (?:are|of|total)\s+" + NUM + r".{0,60}?sells for\s+" + NUM
                + r".{0,40}?variable cost (?:of\s+)?" + NUM, q)
    if not m:
        return None
    fixed, price, var = (num(g) for g in m.groups())
    cm = price - var
    if cm <= 0:
        return None
    exact = fixed / cm
    # Assignment wording: "to the nearest unit". The practice keys confirm standard rounding
    # (see tests); ceil is used only if the question explicitly asks for whole units to cover.
    if _search(r"at least|round(?:ed)? up|minimum", q):
        value, method = math.ceil(exact - 1e-9), "ceil(F/(P-V))"
    else:
        value, method = math.floor(exact + 0.5), "round(F/(P-V))"
    return Solution("break_even", method, value, match_number(value, opts), [f"exact={exact:.4f}"])


@solver("probability")
def probability(q: str, opts: list[str]):
    m = _search(r"holds\s+(.*?)\.\s.*?probability (?:that )?it is an?\s+([a-z-]+)", q)
    if not m:
        return None
    parts = re.findall(r"(\d[\d,]*)\s+([a-z-]+)", m.group(1), flags=re.IGNORECASE)
    target = m.group(2).lower()
    counts = {k.lower(): num(v) for v, k in parts}
    total = sum(counts.values())
    if target not in counts or total == 0:
        return None
    value = counts[target] / total * 100
    return Solution("probability", "count/total", value, match_number(value, opts, percent=True))


@solver("percent_change")
def percent_change(q: str, opts: list[str]):
    m = _search(r"(?:moved|went|changed|rose|fell|grew|increased|decreased) from\s+" + NUM + r"\s+to\s+" + NUM, q)
    if not m or not _search(r"percent(?:age)? change|% change", q):
        return None
    old, new = num(m.group(1)), num(m.group(2))
    if old == 0:
        return None
    value = (new - old) / old * 100
    return Solution("percent_change", "(new-old)/old", value, match_number(value, opts, percent=True))


_ORDINALS = {"first": 0, "second": 1, "third": 2, "fourth": 3}


@solver("ratio_split")
def ratio_split(q: str, opts: list[str]):
    m = _search(NUM + r".{0,60}?split\s+.{0,60}?ratio\s+(\d+(?:\s*:\s*\d+)+).{0,60}?(first|second|third|fourth)", q)
    if not m:
        return None
    total = num(m.group(1))
    parts = [num(x) for x in m.group(2).split(":")]
    idx = _ORDINALS[m.group(3).lower()]
    if idx >= len(parts) or sum(parts) == 0:
        return None
    value = total * parts[idx] / sum(parts)
    return Solution("ratio_split", "total*part/sum(parts)", value, match_number(value, opts))


@solver("sequence")
def sequence(q: str, opts: list[str]):
    m = _search(r"series runs\s+([-\d.,\s]+?)\.?\s+If the pattern continues", q)
    if not m:
        return None
    terms = [num(t) for t in re.findall(r"-?\d+(?:\.\d+)?", m.group(1))]
    if len(terms) < 3:
        return None
    diffs = {round(b - a, 9) for a, b in zip(terms, terms[1:])}
    if len(diffs) == 1:
        value, method = terms[-1] + diffs.pop(), "arithmetic"
    elif all(t != 0 for t in terms) and len({round(b / a, 9) for a, b in zip(terms, terms[1:])}) == 1:
        value, method = terms[-1] * terms[-1] / terms[-2], "geometric"
    else:
        d2 = {round((c - b) - (b - a), 9) for a, b, c in zip(terms, terms[1:], terms[2:])}
        if len(d2) != 1:
            return None
        value, method = terms[-1] + (terms[-1] - terms[-2]) + d2.pop(), "second-difference"
    return Solution("sequence", method, value, match_number(value, opts))


@solver("date_settlement")
def date_settlement(q: str, opts: list[str]):
    m = _search(r"signed on\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4}).{0,40}?(\d+)[\s-]*day", q)
    if not m:
        return None
    start = parse_date(m.group(1))
    if start is None:
        return None
    value = start + dt.timedelta(days=int(m.group(2)))
    return Solution("date_settlement", "start + N calendar days", value.isoformat(), match_date(value, opts))
