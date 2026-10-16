"""Practice-round evaluation harness: solve rounds 1-48 and score against the official keys."""

from __future__ import annotations

import collections
import json
from pathlib import Path

from . import config
from .solver import solve_round
from .source import Snapshot

ACCEPTANCE_THRESHOLD = 0.99


def evaluate(snap: Snapshot, keys: dict[tuple[int, int], str], llm=None) -> dict:
    per_round, per_cat = {}, collections.defaultdict(lambda: [0, 0])
    per_status = collections.Counter()
    errors, total, correct, unkeyed = [], 0, 0, 0
    for rid in sorted(r for r in snap.rounds if r in config.PRACTICE_ROUNDS):
        rnd = snap.rounds[rid]
        results = solve_round(rnd.question_list(), llm)
        right = 0
        for res in results:
            key = keys.get((rid, res.q_index))
            if key is None:
                unkeyed += 1
                continue
            total += 1
            ok = res.letter == key
            right += ok
            per_cat[res.category][0] += ok
            per_cat[res.category][1] += 1
            per_status[res.status] += 1
            if not ok:
                q = rnd.questions[res.q_index]
                errors.append({"round": rid, "q": res.q_index, "category": res.category, "status": res.status,
                               "predicted": res.letter, "key": key, "computed": res.value,
                               "question": q["question_text"],
                               "options": [q[f"option_{c}"] for c in "abcd"]})
        correct += right
        per_round[rid] = {"correct": right, "total": len(results), "complete": rnd.complete}
    accuracy = correct / total if total else 0.0
    present = {(rid, qi) for rid, rnd in snap.rounds.items() if rid in config.PRACTICE_ROUNDS
               for qi in rnd.questions}
    orphan_keys = sorted(k for k in keys if k[0] in config.PRACTICE_ROUNDS and k not in present)
    expected_total = len(config.PRACTICE_ROUNDS) * config.QUESTIONS_PER_ROUND
    full_coverage = total == expected_total and unkeyed == 0 and not orphan_keys
    return {
        "questions_scored": total, "correct": correct, "accuracy": round(accuracy, 6),
        "threshold": ACCEPTANCE_THRESHOLD, "expected_questions": expected_total,
        "full_coverage": full_coverage,
        "passes_threshold": full_coverage and accuracy >= ACCEPTANCE_THRESHOLD,
        "rounds_scored": len(per_round), "questions_without_key": unkeyed,
        "keys_without_question": [list(k) for k in orphan_keys],
        "by_category": {k: {"correct": v[0], "total": v[1], "accuracy": round(v[0] / v[1], 6)}
                        for k, v in sorted(per_cat.items())},
        "by_status": dict(per_status), "per_round": per_round, "errors": errors,
    }


def evaluate_llm_only(snap: Snapshot, keys: dict[tuple[int, int], str], llm, per_category: int = 2) -> dict:
    """Send a category-balanced sample of practice questions straight to the LLM fallback
    (templates bypassed) and score it against the official keys. Never writes answers."""
    from .solver.templates import SOLVERS

    picked, seen = [], collections.Counter()
    for rid in sorted(r for r in snap.rounds if r in config.PRACTICE_ROUNDS):
        for q in snap.rounds[rid].question_list():
            opts = [q[f"option_{c}"] for c in "abcd"]
            cat = next((c for c, fn in SOLVERS if fn(q["question_text"], opts)), "unknown")
            if seen[cat] < per_category:
                seen[cat] += 1
                picked.append((rid, q, opts, cat))
    rows, correct = [], 0
    for rid, q, opts, cat in picked:
        ans = llm.solve(q["question_text"], opts)
        key = keys[(rid, q["q_index"])]
        ok = ans is not None and ans.letter == key
        correct += ok
        rows.append({"round": rid, "q": q["q_index"], "category": cat, "key": key,
                     "llm": ans.letter if ans else None, "status": ans.status if ans else "failed", "correct": ok})
    return {"sampled": len(picked), "correct": correct, "llm_calls": llm.calls,
            "accuracy": round(correct / len(picked), 4) if picked else 0.0,
            "by_status": dict(collections.Counter(r["status"] for r in rows)), "rows": rows}


def write_report(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n")
