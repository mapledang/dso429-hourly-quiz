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
    return {
        "questions_scored": total, "correct": correct, "accuracy": round(accuracy, 6),
        "threshold": ACCEPTANCE_THRESHOLD, "passes_threshold": accuracy >= ACCEPTANCE_THRESHOLD and total > 0,
        "rounds_scored": len(per_round), "questions_without_key": unkeyed,
        "by_category": {k: {"correct": v[0], "total": v[1], "accuracy": round(v[0] / v[1], 6)}
                        for k, v in sorted(per_cat.items())},
        "by_status": dict(per_status), "per_round": per_round, "errors": errors,
    }


def write_report(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n")
