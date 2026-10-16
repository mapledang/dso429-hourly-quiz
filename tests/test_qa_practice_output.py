"""QA sections F (practice-key regression) and G (output-file validation)."""

import json
import os
import random

import pytest

from quizbot import answers, config, evaluate, source
from tests.conftest import FIX, practice_rows, to_csv

req = pytest.mark.req
KEYS_RAW = (FIX / "practice_keys.csv").read_bytes()


def keys_csv(rows):
    return ("round_id,q_index,answer\n" + "".join(f"{r},{q},{a}\n" for r, q, a in rows)).encode()


def key_rows():
    return [(r, q, a) for (r, q), a in sorted(source.parse_keys(KEYS_RAW).items())]


# --- F: practice regression -----------------------------------------------------------------

@req("P01", "P06", "P08")
def test_p01_p06_p08_full_official_evaluation(practice_snapshot, practice_keys):
    rep = evaluate.evaluate(practice_snapshot, practice_keys)
    assert rep["questions_scored"] == 1200 and rep["rounds_scored"] == 48 and rep["full_coverage"]
    assert rep["correct"] == 1200 and rep["accuracy"] == 1.0 and rep["passes_threshold"]
    assert all(v["total"] == 25 for v in rep["per_round"].values())
    assert len(rep["by_category"]) == 12 and sum(v["total"] for v in rep["by_category"].values()) == 1200


@req("P02")
def test_p02_shuffled_keys_matched_by_id(practice_snapshot):
    rows = key_rows()
    random.Random(3).shuffle(rows)
    rep = evaluate.evaluate(practice_snapshot, source.parse_keys(keys_csv(rows)))
    assert rep["correct"] == 1200


@req("P03")
@pytest.mark.parametrize("bad", ["E", "a", "", "AB"])
def test_p03_invalid_key_letter_rejected(bad):
    rows = key_rows()
    rows[10] = (rows[10][0], rows[10][1], bad)
    with pytest.raises(source.SourceError):
        source.parse_keys(keys_csv(rows))


@req("P04")
def test_p04_missing_key_is_incomplete_coverage(practice_snapshot):
    keys = source.parse_keys(KEYS_RAW)
    del keys[(7, 13)]
    rep = evaluate.evaluate(practice_snapshot, keys)
    assert rep["questions_without_key"] == 1 and not rep["full_coverage"] and not rep["passes_threshold"]


@req("P05")
def test_p05_orphan_key_reported(practice_snapshot):
    snap = source.parse_questions(to_csv([r for r in practice_rows() if not (r["round_id"] == "5" and r["q_index"] == "9")]))
    rep = evaluate.evaluate(snap, source.parse_keys(KEYS_RAW))
    assert [5, 9] in rep["keys_without_question"] and not rep["passes_threshold"]


@req("P07")
def test_p07_below_threshold_fails_readiness(practice_snapshot, monkeypatch):
    import quizbot.evaluate as ev
    real = ev.solve_round

    def sabotaged(questions, llm=None):
        res = real(questions, llm)
        res[0].letter = "ABCD"[("ABCD".index(res[0].letter) + 1) % 4]  # 1 wrong per round = 96%
        return res
    monkeypatch.setattr(ev, "solve_round", sabotaged)
    rep = ev.evaluate(practice_snapshot, source.parse_keys(KEYS_RAW))
    assert rep["accuracy"] == pytest.approx(0.96) and not rep["passes_threshold"] and len(rep["errors"]) == 48


@req("P09")
def test_p09_ci_runs_full_regression_on_solver_change():
    ci = (FIX.parent.parent / ".github/workflows/ci.yml").read_text()
    assert "quizbot/**" in ci and "python -m pytest" in ci and "quizbot evaluate" in ci


@req("P10")
def test_p10_reproducible(practice_snapshot, practice_keys):
    a = evaluate.evaluate(practice_snapshot, practice_keys)
    b = evaluate.evaluate(practice_snapshot, practice_keys)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


@req("P11")
def test_p11_misaligned_keys_are_caught(practice_snapshot):
    """If keys were joined by position instead of (round, q), accuracy would collapse - prove the
    evaluator would expose it by shifting every key by one question."""
    keys = source.parse_keys(KEYS_RAW)
    shifted = {(r, q): keys[(r, q % 25 + 1)] for (r, q) in keys}
    rep = evaluate.evaluate(practice_snapshot, shifted)
    assert rep["accuracy"] < 0.5 and not rep["passes_threshold"]


@req("P12")
def test_p12_keys_unavailable_is_blocked_not_100(monkeypatch, capsys):
    from quizbot import __main__ as cli
    monkeypatch.setattr(cli.source, "fetch_questions", lambda: source.parse_questions(to_csv(practice_rows({1}))))
    monkeypatch.setattr(cli.source, "fetch_keys", lambda: (_ for _ in ()).throw(source.SourceError("HTTP 404")))
    code = cli.main(["evaluate", "--report", os.devnull])
    out = capsys.readouterr().out
    assert code == cli.EXIT_BLOCKED and "BLOCKED" in out and "accuracy" not in out.split("BLOCKED")[1][:40]


# --- G: output validation --------------------------------------------------------------------

LET = {i: "ABCD"[i % 4] for i in range(1, 26)}


def payload(**over):
    p = answers.build(49, LET)
    p.update(over)
    return p


@req("V01")
def test_v01_valid():
    assert answers.validate(payload(), 49) == []
    assert answers.validate_bytes(answers.serialize(payload()), 49) == []


@req("V02", "V14")
def test_v02_v14_missing_q25():
    p = payload()
    del p["answers"]["q25"]
    assert answers.validate(p, 49)


@req("V03")
def test_v03_extra_q26():
    p = payload()
    p["answers"]["q26"] = "A"
    assert answers.validate(p, 49)


@req("V04")
def test_v04_duplicate_question_keys():
    raw = answers.serialize(payload()).replace(b'"q1": "B"', b'"q1": "B", "q1": "C"')
    assert any("duplicate" in e for e in answers.validate_bytes(raw, 49))


@req("V05")
def test_v05_round_id_mismatch():
    assert answers.validate(payload(round_id=50), 49)


@req("V06", "V07")
@pytest.mark.parametrize("name,ok", [("answers/round-0049.json", True), ("answers/round-049.json", False),
                                     ("answers/round-00049.json", False), ("round-0049.json", False),
                                     ("answers/sub/round-0049.json", False), ("answers/Round-0049.json", False)])
def test_v06_v07_filename_and_directory(name, ok):
    from quizbot.audit import _ANSWER_NAME
    assert bool(_ANSWER_NAME.match(name)) is ok
    assert (name == config.answer_path(49)) is ok


@req("V08")
@pytest.mark.parametrize("raw", [b'{"round_id": 49, "answers": {"q1": "A",}}', b"{'round_id': 49}", b"```json\n{}\n```"])
def test_v08_malformed(raw):
    assert answers.validate_bytes(raw, 49)


@req("V09")
def test_v09_lowercase_rejected_in_final_file():
    p = payload()
    p["answers"]["q4"] = "a"
    assert answers.validate(p, 49)


@req("V10")
def test_v10_trailing_whitespace_rejected_in_final_file():
    p = payload()
    p["answers"]["q4"] = "A "
    assert answers.validate(p, 49)


@req("V11")
@pytest.mark.parametrize("v", [1, None, True, ["A"], {"A": 1}])
def test_v11_non_string_answers(v):
    p = payload()
    p["answers"]["q4"] = v
    assert answers.validate(p, 49)


@req("V12")
def test_v12_extra_top_level():
    assert answers.validate(payload(explanation="because"), 49)


@req("V13", "F02")
def test_v13_f02_interrupted_write_keeps_previous_file(tmp_path, monkeypatch):
    good = answers.write_atomic(tmp_path, 49, payload())
    before = good.read_bytes()

    def boom(src, dst):
        raise OSError("power loss during rename")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        answers.write_atomic(tmp_path, 49, answers.build(49, {i: "D" for i in range(1, 26)}))
    assert good.read_bytes() == before and not list(good.parent.glob(".tmp-*"))


@req("V15", "A23")
def test_v15_shift_detected_by_index_mapping(practice_snapshot, practice_keys):
    """A one-question shift is structurally valid JSON; the index-keyed regression check catches it."""
    from quizbot.solver import solve_round
    res = solve_round(practice_snapshot.rounds[1].question_list())
    shifted = {r.q_index % 25 + 1: r.letter for r in res}
    correct = sum(shifted[q] == practice_keys[(1, q)] for q in range(1, 26))
    assert answers.validate(answers.build(1, shifted), 1) == [] and correct < 20


@req("V18")
def test_v18_crlf_and_utf8_tolerated():
    raw = answers.serialize(payload()).replace(b"\n", b"\r\n")
    assert answers.validate_bytes(raw, 49) == []
    assert answers.validate_bytes("﻿".encode() + answers.serialize(payload()), 49)  # BOM is not valid JSON per RFC


@req("V19")
def test_v19_malformed_never_replaces_valid(tmp_path):
    good = answers.write_atomic(tmp_path, 49, payload())
    before = good.read_bytes()
    bad = payload()
    bad["answers"]["q1"] = "Z"
    with pytest.raises(ValueError):
        answers.write_atomic(tmp_path, 49, bad)
    assert good.read_bytes() == before
