"""Property/fuzz tests: random questions in the exact practice wording, answers computed by an
independent Decimal reference (written separately from quizbot.solver.templates), correct option
shuffled among distractors. The solver must pick the correct option every time."""

import datetime as dt
import random
from decimal import ROUND_HALF_UP, Decimal

import pytest

from quizbot.solver import solve_question

N = 400  # cases per template
D = Decimal


def q2(x):  # money / 2dp, half-up as a human would print it
    return D(x).quantize(D("0.01"), rounding=ROUND_HALF_UP)


def q1(x):
    return D(x).quantize(D("0.1"), rounding=ROUND_HALF_UP)


def money(x):
    return f"${q2(x):,.2f}"


def plain2(x):
    return f"{q2(x):,.2f}"


def pct1(x):
    return f"{q1(x):.1f}%"


def build(rng, correct, distractors, fmt):
    """Shuffle the formatted correct answer among distinct formatted distractors."""
    shown = [fmt(correct)]
    for d in distractors:
        s = fmt(d)
        if s not in shown:
            shown.append(s)
    k = 2
    while len(shown) < 4:  # top up if some distractors collided after rounding
        s = fmt(D(correct) * D(1 + 0.37 * k) + k)
        k += 1
        if s not in shown:
            shown.append(s)
    shown = shown[:4]
    correct_text = shown[0]
    rng.shuffle(shown)
    return shown, "ABCD"[shown.index(correct_text)]


def gen_compound(rng):
    p, r, n = D(rng.randint(100, 2_000_000)) / 100 * 100, rng.randint(1, 20), rng.randint(1, 30)
    p = q2(D(rng.randint(10_000, 100_000_000)) / 100)
    v = p * (1 + D(r) / 100) ** n
    q = f"An investment of {money(p)} grows at {r}% compounded annually. What is its value after {n} years?"
    return q, v, [p * (1 + D(r) * n / 100), p * (1 + D(r) / 100) ** (n + 1), p * (1 - D(r) * n / 100)], money


def gen_margin(rng):
    rev = q2(D(rng.randint(100_000, 50_000_000)) / 100)
    cogs = q2(rev * D(rng.randint(5, 990)) / 1000)
    v = (rev - cogs) / rev * 100
    q = f"A product line generates {money(rev)} in revenue against {money(cogs)} in cost of goods sold. What is the gross margin?"
    return q, v, [100 - v, (rev - cogs) / cogs * 100, v / 2], pct1


def gen_sequence(rng):
    a, d = rng.randint(-50, 500), rng.choice([x for x in range(-40, 41) if x != 0])
    terms = [a + i * d for i in range(5)]
    q = f"A monthly headcount series runs {', '.join(map(str, terms))}. If the pattern continues, what is the next value?"
    return q, D(a + 5 * d), [D(a + 6 * d), D(a + 4 * d), D(a + 5 * d + 2 * d + 1)], lambda x: str(int(x))


def gen_discount(rng):
    price, pct = q2(D(rng.randint(100, 500_000)) / 100), rng.randint(1, 90)
    v = price * (1 - D(pct) / 100)
    q = f"An item listed at {money(price)} is discounted by {pct}%. What is the sale price?"
    return q, v, [price * (1 + D(pct) / 100), price * D(pct) / 100, price - pct], money


def gen_change(rng):
    old = q2(D(rng.randint(100, 10_000_000)) / 100)
    new = q2(old * D(rng.randint(10, 300)) / 100)
    v = (new - old) / old * 100
    q = f"Quarterly revenue moved from {money(old)} to {money(new)}. What is the percentage change?"
    return q, v, [-v, new / old * 100, (new - old) / new * 100], pct1


def gen_date(rng):
    start = dt.date(2026, 1, 1) + dt.timedelta(days=rng.randint(0, 900))
    n = rng.randint(1, 400)
    v = start + dt.timedelta(days=n)
    fmt = lambda d: f"{d.day} {d.strftime('%B')} {d.year}"  # noqa: E731
    q = f"A contract signed on {fmt(start)} has a {n}-day settlement period. On what date does it settle?"
    ds = [v + dt.timedelta(days=1), v - dt.timedelta(days=7), v + dt.timedelta(days=30)]
    shown = [fmt(v)] + [fmt(d) for d in ds]
    rng.shuffle(shown)
    return q, shown, "ABCD"[shown.index(fmt(v))]


def gen_break_even(rng):
    fixed = D(rng.randint(1_000, 500_000))
    var = q2(D(rng.randint(100, 20_000)) / 100)
    price = var + q2(D(rng.randint(50, 10_000)) / 100)
    exact = fixed / (price - var)
    v = exact.quantize(D("1"), rounding=ROUND_HALF_UP)
    q = (f"Fixed costs are {money(fixed)}. Each unit sells for {money(price)} with a variable cost of "
         f"{money(var)}. How many units must be sold to break even, to the nearest unit?")
    unit = lambda x: f"{int(D(x).quantize(D('1'), rounding=ROUND_HALF_UP))} units"  # noqa: E731
    return q, v, [fixed / price, fixed / var if var else v * 3, v * 2 + 7], unit


def gen_ratio(rng):
    total = q2(D(rng.randint(10_000, 10_000_000)) / 100)
    a, b = rng.randint(1, 9), rng.randint(1, 9)
    v = total * a / (a + b)
    q = f"A {money(total)} marketing budget is split between two channels in the ratio {a}:{b}. What does the first channel receive?"
    return q, v, [total * b / (a + b), total * a / b if a != b else v / 2, total / (a + b)], money


def gen_weighted(rng):
    n1, n2 = rng.randint(1, 500), rng.randint(1, 500)
    a1, a2 = D(rng.randint(10, 200)), D(rng.randint(10, 200))
    v = (n1 * a1 + n2 * a2) / (n1 + n2)
    q = (f"Region A had {n1} orders averaging {money(a1)}; region B had {n2} orders averaging "
         f"{money(a2)}. What is the overall average order value?")
    return q, v, [(a1 + a2) / 2 + D("0.37"), (n1 * a2 + n2 * a1) / (n1 + n2) + D("0.11"), v * D("1.2")], plain2


def gen_fx(rng):
    amt, rate = q2(D(rng.randint(100, 5_000_000)) / 100), D(rng.randint(5000, 20000)) / 10000
    v = amt * rate
    q = f"An invoice for {money(amt)} is converted to euros at {rate:.4f} euros per US dollar. How many euros is that?"
    return q, v, [amt / rate, v * D("1.07"), amt * (rate - D("0.1"))], plain2


def gen_prob(rng):
    b, t, a = rng.randint(1, 60), rng.randint(0, 60), rng.randint(0, 60)
    v = D(b) / (b + t + a) * 100
    q = (f"A support queue holds {b} billing tickets, {t} technical tickets and {a} account tickets. "
         f"If one is drawn at random, what is the probability it is a billing ticket?")
    return q, v, [D(b) / (t + a + 1) * 100 + 1, v / 2 + D("0.3"), 100 - v + D("0.2")], pct1


def gen_revenue(rng):
    units, price = rng.randint(1, 5000), q2(D(rng.randint(50, 100_000)) / 100)
    v = units * price
    q = f"A retailer sells {units} units at {money(price)} each. What is total revenue?"
    return q, v, [(units + 10) * price, units * (price + 1), v * D("1.1")], money


GENERATORS = [gen_compound, gen_margin, gen_sequence, gen_discount, gen_change, gen_break_even,
              gen_ratio, gen_weighted, gen_fx, gen_prob, gen_revenue]


@pytest.mark.parametrize("gen", GENERATORS, ids=lambda g: g.__name__)
def test_fuzz_numeric_templates(gen):
    rng = random.Random(gen.__name__)
    failures = []
    for case in range(N):
        q, value, distractors, fmt = gen(rng)
        opts, key = build(rng, value, distractors, fmt)
        r = solve_question(1, q, opts)
        if r.letter != key or not r.confident:
            failures.append((case, q, opts, key, r.letter, r.status, r.value))
    assert failures == [], f"{len(failures)}/{N} failures, first: {failures[:3]}"


def test_fuzz_dates_including_leap_years_and_year_ends():
    rng = random.Random("dates")
    failures = []
    for case in range(N * 2):
        q, opts, key = gen_date(rng)
        r = solve_question(1, q, opts)
        if r.letter != key or not r.confident:
            failures.append((q, opts, key, r.letter))
    assert failures == []
