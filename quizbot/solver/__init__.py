"""Question solving: deterministic templates first, optional LLM second, flagged guess last."""

from __future__ import annotations

import logging
import statistics
from dataclasses import asdict, dataclass

from .llm import LLMSolver
from .numbers import LETTERS, parse_number
from .templates import SOLVERS

log = logging.getLogger(__name__)

# Statuses that count as trustworthy without human review.
CONFIDENT = {"exact", "close", "llm_verified"}


@dataclass
class QuestionResult:
    q_index: int
    letter: str
    status: str      # exact | close | llm_verified | llm_unverified | llm_expression_override | guess
    category: str
    method: str
    value: str

    @property
    def confident(self) -> bool:
        return self.status in CONFIDENT

    def to_dict(self) -> dict:
        return asdict(self)


def heuristic_guess(options: list[str]) -> str:
    """Last resort when nothing can solve a question: the option nearest the median of
    the numeric options (distractors tend to bracket the answer). Always flagged."""
    nums = [parse_number(o) for o in options]
    if all(n is not None for n in nums):
        med = statistics.median(nums)
        return min(zip(LETTERS, nums), key=lambda t: abs(t[1] - med))[0]
    return "A"


def solve_question(q_index: int, question: str, options: list[str], llm: LLMSolver | None = None) -> QuestionResult:
    candidates = []
    for category, fn in SOLVERS:
        try:
            sol = fn(question, options)
        except Exception as exc:  # a template bug must never take down the round
            log.exception("solver %s crashed on q%d: %s", category, q_index, exc)
            continue
        if sol is not None:
            candidates.append(sol)

    good = [s for s in candidates if s.letter is not None]
    letters = {s.letter for s in good}
    if len(letters) == 1:
        best = min(good, key=lambda s: 0 if s.match.status == "exact" else 1)
        return QuestionResult(q_index, best.letter, best.match.status, best.category, best.method, str(best.value))
    if len(letters) > 1:
        log.warning("q%d: templates disagree %s", q_index, [(s.category, s.letter) for s in good])
    elif candidates:
        log.warning("q%d: template %s matched text but not options: %s",
                    q_index, candidates[0].category, candidates[0].match.detail)

    if llm is not None:
        ans = llm.solve(question, options)
        if ans is not None:
            return QuestionResult(q_index, ans.letter, ans.status, "llm", ans.method, ans.result)

    letter = heuristic_guess(options)
    log.error("q%d: no reliable solution, flagged guess %s", q_index, letter)
    return QuestionResult(q_index, letter, "guess", "unknown", "median-option heuristic", "")


def solve_round(questions: list[dict], llm: LLMSolver | None = None) -> list[QuestionResult]:
    """`questions` are dicts with q_index, question_text, option_a..option_d (see source.py)."""
    return [
        solve_question(q["q_index"], q["question_text"],
                       [q["option_a"], q["option_b"], q["option_c"], q["option_d"]], llm)
        for q in sorted(questions, key=lambda q: q["q_index"])
    ]
