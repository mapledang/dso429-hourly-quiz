"""QA section D: round discovery and reconciliation (temporary bare remote, fixed clocks)."""

import json

import pytest

from quizbot import answers, config, source, state
from quizbot.gitops import GitError, Repo
from quizbot.pipeline import EXIT_FAILURE, EXIT_OK, Pipeline
from tests.conftest import as_round, git, practice_rows, to_csv

req = pytest.mark.req


def rel(rid):
    return config.scheduled_release(rid).isoformat().replace("+00:00", "Z")


def graded(rid, practice=None):
    return as_round(practice_rows({practice or (rid - 49) % 48 + 1}), rid, rel(rid))


def run(work, rows, **kw):
    raw = to_csv(rows)
    return Pipeline(work, "production", repo=Repo(work, sleep=lambda s: None),
                    loader=lambda: source.parse_questions(raw, origin="t"), **kw).run()


def remote(work, path):
    git(work, "fetch", "--quiet")
    return Repo(work).remote_blob(path)


def correct(work, rid, keys):
    raw = remote(work, config.answer_path(rid))
    if raw is None or answers.validate_bytes(raw, rid):
        return False
    pr = (rid - 49) % 48 + 1 if rid >= 49 else rid
    return all(v == keys[(pr, q)] for q, v in answers.letters_of(raw).items())


def commits(work):
    git(work, "fetch", "--quiet")
    return int(git(work, "rev-list", "--count", "origin/main"))


@pytest.fixture
def at(monkeypatch):
    return lambda iso: monkeypatch.setenv("QUIZ_NOW", iso)


@req("R01")
def test_r01_one_new_round(remote_and_clone, at, practice_keys):
    _, clone = remote_and_clone
    w = clone()
    at("2026-10-17T21:06:00Z")
    rep = run(w, graded(49))
    assert rep.exit_code == EXIT_OK and rep.verified == [49] and correct(w, 49, practice_keys)


@req("R02", "R11")
def test_r02_r11_five_rounds_one_run(remote_and_clone, at, practice_keys):
    _, clone = remote_and_clone
    w = clone()
    at("2026-10-18T01:10:00Z")
    rows = [r for rid in range(49, 54) for r in graded(rid)]
    rep = run(w, rows)
    assert sorted(rep.verified) == [49, 50, 51, 52, 53]
    assert all(correct(w, r, practice_keys) for r in range(49, 54))


@req("R03")
def test_r03_older_missing_round_backfilled(remote_and_clone, at, practice_keys):
    _, clone = remote_and_clone
    w = clone()
    at("2026-10-17T23:10:00Z")
    run(w, graded(50) + graded(51))
    rep = run(w, graded(49) + graded(50) + graded(51))
    assert rep.solved == [49] and correct(w, 49, practice_keys)


@req("R04", "R10")
def test_r04_r10_valid_file_untouched_and_idempotent(remote_and_clone, at):
    _, clone = remote_and_clone
    w = clone()
    at("2026-10-17T21:10:00Z")
    run(w, graded(49))
    n = commits(w)
    before = remote(w, config.answer_path(49))
    for _ in range(3):
        rep = run(w, graded(49))
        assert rep.solved == [] and rep.pushed == []
    assert commits(w) == n and remote(w, config.answer_path(49)) == before


@req("R05")
@pytest.mark.parametrize("bad", [b"{", b'{"round_id": 49}', b'{"round_id": 50, "answers": {}}', b""])
def test_r05_malformed_remote_file_replaced(remote_and_clone, at, practice_keys, bad):
    _, clone = remote_and_clone
    w = clone()
    (w / "answers").mkdir()
    (w / config.answer_path(49)).write_bytes(bad)
    git(w, "add", "-A"); git(w, "commit", "-qm", "bad"); git(w, "push", "-q", "origin", "HEAD:main")
    at("2026-10-17T21:10:00Z")
    rep = run(w, graded(49))
    assert rep.solved == [49] and correct(w, 49, practice_keys)
    assert any("existing file invalid" in x for x in rep.warnings)


@req("R06")
def test_r06_remote_file_absent_locally_is_respected(remote_and_clone, at):
    _, clone = remote_and_clone
    a, b = clone("a"), clone("b")
    at("2026-10-17T21:10:00Z")
    run(a, graded(49))
    n = commits(a)
    rep = run(b, graded(49))  # b's checkout predates a's push
    assert rep.solved == [] and rep.already_submitted == [49] and commits(b) == n


@req("R07")
def test_r07_local_unpushed_file_is_reconciled(remote_and_clone, at, practice_keys):
    _, clone = remote_and_clone
    w = clone()
    (w / "answers").mkdir()
    (w / config.answer_path(49)).write_bytes(answers.serialize(answers.build(49, {i: "A" for i in range(1, 26)})))
    git(w, "add", "-A"); git(w, "commit", "-qm", "local only")  # committed locally, never pushed
    at("2026-10-17T21:10:00Z")
    rep = run(w, graded(49))
    assert rep.solved == [49] and correct(w, 49, practice_keys)  # remote state wins, local junk discarded


@req("R08")
def test_r08_crash_after_solve_before_push(remote_and_clone, at, monkeypatch, practice_keys):
    _, clone = remote_and_clone
    w = clone()
    at("2026-10-17T21:10:00Z")
    monkeypatch.setattr(Repo, "commit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("runner killed")))
    with pytest.raises(RuntimeError):
        run(w, graded(49))
    assert remote(w, config.answer_path(49)) is None
    monkeypatch.undo()
    at("2026-10-17T21:20:00Z")
    rep = run(w, graded(49))
    assert rep.verified == [49] and correct(w, 49, practice_keys)


@req("R09", "CLOUD14")
def test_r09_push_landed_but_manifest_never_recorded(remote_and_clone, at, monkeypatch):
    _, clone = remote_and_clone
    w = clone()
    at("2026-10-17T21:10:00Z")
    monkeypatch.setattr(Pipeline, "_commit_state", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("killed")))
    with pytest.raises(RuntimeError):
        run(w, graded(49))
    monkeypatch.undo()
    assert remote(w, config.answer_path(49)) is not None
    git(w, "log", "--oneline", "origin/main")
    rep = run(w, graded(49))
    log = git(w, "log", "--oneline", "origin/main", "--", config.answer_path(49))
    assert rep.solved == [] and len(log.strip().splitlines()) == 1  # no duplicate submission


@req("R12")
def test_r12_scheduled_but_unpublished_is_awaiting_not_missing(remote_and_clone, at):
    _, clone = remote_and_clone
    w = clone()
    at("2026-10-17T22:45:00Z")  # 49 and 50 scheduled, only 49 published
    rep = run(w, graded(49))
    assert rep.source_lag == [50] and rep.exit_code == EXIT_OK and rep.solved == [49]


@req("R13")
def test_r13_source_down_for_hours_then_recovers(remote_and_clone, at, practice_keys):
    _, clone = remote_and_clone
    w = clone()

    def down():
        raise source.SourceError("HTTP 503")
    for t in ("2026-10-17T21:10:00Z", "2026-10-17T22:10:00Z", "2026-10-17T23:10:00Z"):
        at(t)
        rep = Pipeline(w, "production", repo=Repo(w), loader=down).run()
        assert rep.exit_code == EXIT_FAILURE
    at("2026-10-17T23:20:00Z")
    rep = run(w, graded(49) + graded(50) + graded(51))
    assert sorted(rep.verified) == [49, 50, 51] and all(correct(w, r, practice_keys) for r in (49, 50, 51))


@req("R14")
def test_r14_manifest_missing_rebuilt_from_remote(remote_and_clone, at):
    _, clone = remote_and_clone
    w = clone()
    at("2026-10-17T21:10:00Z")
    run(w, graded(49))
    git(w, "rm", "-q", state.MANIFEST); git(w, "commit", "-qm", "rm manifest"); git(w, "push", "-q", "origin", "HEAD:main")
    rep = run(w, graded(49) + graded(50))
    assert rep.already_submitted == [49] and rep.solved == [50]
    assert json.loads(remote(w, state.MANIFEST))["rounds"]["49"]["status"] == "submitted"


@req("R15")
@pytest.mark.parametrize("junk", [b"{not json", b"[]", b'{"rounds": "oops"}'])
def test_r15_corrupt_manifest_recovered_and_reported(remote_and_clone, at, junk):
    _, clone = remote_and_clone
    w = clone()
    at("2026-10-17T21:10:00Z")
    run(w, graded(49))
    (w / state.MANIFEST).write_bytes(junk)
    git(w, "commit", "-qam", "corrupt"); git(w, "push", "-q", "origin", "HEAD:main")
    rep = run(w, graded(49))
    assert any("corrupt" in x for x in rep.warnings)
    assert rep.already_submitted == [49] and rep.solved == []  # submission untouched
    assert isinstance(json.loads(remote(w, state.MANIFEST))["rounds"], dict)


@req("R16")
def test_r16_manifest_says_submitted_but_file_missing(remote_and_clone, at, practice_keys):
    _, clone = remote_and_clone
    w = clone()
    at("2026-10-17T21:10:00Z")
    run(w, graded(49))
    git(w, "rm", "-q", config.answer_path(49)); git(w, "commit", "-qm", "lost"); git(w, "push", "-q", "origin", "HEAD:main")
    rep = run(w, graded(49))
    assert any("manifest says submitted but remote file is missing" in x for x in rep.warnings)
    assert rep.solved == [49] and correct(w, 49, practice_keys)


@req("R17")
def test_r17_round_disappears_from_source(remote_and_clone, at):
    _, clone = remote_and_clone
    w = clone()
    at("2026-10-17T22:10:00Z")
    run(w, graded(49) + graded(50))
    before = remote(w, config.answer_path(49))
    rep = run(w, graded(50))  # 49 vanished (stale CDN / regression)
    assert any("round 49" in x and "no longer in source" in x for x in rep.warnings)
    assert remote(w, config.answer_path(49)) == before


@req("R18")
def test_r18_new_round_while_another_incomplete(remote_and_clone, at, practice_keys):
    _, clone = remote_and_clone
    w = clone()
    at("2026-10-17T22:10:00Z")
    rows = [r for r in graded(49) if r["q_index"] != "3"] + graded(50)
    rep = run(w, rows)
    assert 49 in rep.incomplete and rep.solved == [50] and correct(w, 50, practice_keys)
    assert remote(w, config.answer_path(49)) is None


@req("R19")
def test_r19_out_of_range_round_not_submitted(remote_and_clone, at):
    _, clone = remote_and_clone
    w = clone()
    at("2026-10-17T21:10:00Z")
    rows = graded(49) + as_round(practice_rows({2}), 169, rel(168)) + as_round(practice_rows({3}), 500, rel(168))
    rep = run(w, rows)
    assert any("outside 1-168" in x and "169" in x and "500" in x for x in rep.warnings)
    assert remote(w, "answers/round-0169.json") is None and remote(w, "answers/round-0500.json") is None
    assert rep.solved == [49]


@req("R20")
def test_r20_conflicting_release_times_use_earliest_and_flag(remote_and_clone, at):
    _, clone = remote_and_clone
    w = clone()
    rows = graded(49)
    rows[7]["released_at"] = "2026-10-17T23:00:00Z"  # one row claims a later release
    snap = source.parse_questions(to_csv(rows))
    assert snap.rounds[49].released_at.isoformat().startswith("2026-10-17T21:00")
    assert snap.rounds[49].complete and any("conflicts" in x for x in snap.warnings)
    at("2026-10-17T21:10:00Z")
    rep = run(w, rows)
    assert rep.solved == [49] and any("conflicts" in x for x in rep.warnings)
