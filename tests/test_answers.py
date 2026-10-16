import json

import pytest

from quizbot import answers

LETTERS = {i: "ABCD"[i % 4] for i in range(1, 26)}


def test_build_matches_assignment_schema():
    p = answers.build(1, LETTERS)
    assert list(p) == ["round_id", "answers"] and list(p["answers"]) == [f"q{i}" for i in range(1, 26)]
    assert answers.validate(p, 1) == []


def test_assignment_example_file_validates():
    example = {"round_id": 1, "answers": dict(zip([f"q{i}" for i in range(1, 26)],
               "C A C B D A C A C A A D A B A D A B A C B D B B A".split()))}
    assert answers.validate_bytes(json.dumps(example).encode(), 1) == []


@pytest.mark.parametrize("mutate,expect", [
    (lambda p: p["answers"].pop("q25"), "missing"),
    (lambda p: p["answers"].update(q26="A"), "extra"),
    (lambda p: p["answers"].update(q3="E"), "invalid letters"),
    (lambda p: p["answers"].update(q3="a"), "invalid letters"),
    (lambda p: p["answers"].update(q3=None), "invalid letters"),
    (lambda p: p.update(round_id=2), "round_id"),
    (lambda p: p.update(round_id="1"), "round_id"),
    (lambda p: p.update(explanation="x"), "top-level keys"),
])
def test_validation_rejects(mutate, expect):
    p = answers.build(1, LETTERS)
    mutate(p)
    assert any(expect in e for e in answers.validate(p, 1))


def test_invalid_json_and_missing():
    assert answers.validate_bytes(b"{not json", 1)[0].startswith("invalid JSON")
    assert answers.validate_bytes(b"```json\n{}\n```", 1)
    assert answers.validate_bytes(None, 1) == ["missing"]


def test_duplicate_keys_rejected():
    raw = b'{"round_id": 1, "round_id": 1, "answers": {}}'
    assert any("duplicate" in e for e in answers.validate_bytes(raw, 1))


def test_atomic_write_and_reread(tmp_path):
    path = answers.write_atomic(tmp_path, 7, answers.build(7, LETTERS))
    assert path.name == "round-0007.json"
    assert answers.validate_bytes(path.read_bytes(), 7) == []
    assert not list(path.parent.glob(".tmp-*"))


def test_write_refuses_invalid(tmp_path):
    p = answers.build(7, LETTERS)
    p["answers"]["q1"] = "Z"
    with pytest.raises(ValueError):
        answers.write_atomic(tmp_path, 7, p)
    assert not (tmp_path / "answers" / "round-0007.json").exists()


def test_filename_round_parsing():
    assert answers.round_from_filename("answers/round-0049.json") == 49
    assert answers.round_from_filename("answers/round-49.json") is None
