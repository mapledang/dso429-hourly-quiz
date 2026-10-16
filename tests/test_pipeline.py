"""End-to-end pipeline tests against a local bare git remote standing in for GitHub."""

import hashlib
import json

import pytest

from quizbot import answers, config, source, state
from quizbot.gitops import GitError, Repo
from quizbot.pipeline import EXIT_FAILURE, EXIT_NEEDS_REVIEW, EXIT_OK, Pipeline
from tests.conftest import as_round, git, practice_rows, to_csv

R49 = "2026-10-17T21:00:00Z"
R50 = "2026-10-17T22:00:00Z"


def loader_for(rows):
    raw = to_csv(rows)
    return lambda: source.parse_questions(raw, origin="test")


def pipe(clone, rows, **kw):
    return Pipeline(clone, "production", repo=Repo(clone, sleep=lambda s: None), loader=loader_for(rows), **kw)


def remote_answer(clone, rid):
    git(clone, "fetch", "--quiet", "origin")
    proc = Repo(clone).remote_blob(config.answer_path(rid))
    return proc


@pytest.fixture
def at(monkeypatch):
    def set_now(iso):
        monkeypatch.setenv("QUIZ_NOW", iso)
    return set_now


def test_several_new_rounds_in_one_run_out_of_order(remote_and_clone, at, practice_keys):
    _, clone = remote_and_clone
    work = clone()
    at("2026-10-17T23:10:00Z")
    rows = (as_round(practice_rows({2}), 50, R50) + as_round(practice_rows({1}), 49, R49)
            + practice_rows({3}))[::-1]
    rep = pipe(work, rows).run()
    assert rep.exit_code == EXIT_OK, rep.errors
    assert sorted(rep.solved) == [3, 49, 50] and sorted(rep.verified) == [3, 49, 50]
    assert rep.solved[0] == 3  # oldest release processed first
    got = json.loads(remote_answer(work, 49))
    assert got["round_id"] == 49
    assert all(got["answers"][f"q{q}"] == practice_keys[(1, q)] for q in range(1, 26))
    manifest = json.loads(Repo(work).remote_blob(state.MANIFEST))
    assert manifest["rounds"]["49"]["verified_at"] and manifest["last_remote_verification"]


def test_repeated_runs_make_no_new_commits(remote_and_clone, at):
    _, clone = remote_and_clone
    work = clone()
    at("2026-10-17T21:10:00Z")
    rows = as_round(practice_rows({1}), 49, R49)
    pipe(work, rows).run()
    head = git(work, "rev-parse", "origin/main")
    rep = pipe(work, rows).run()
    git(work, "fetch", "--quiet")
    assert rep.solved == [] and rep.already_submitted == [49]
    assert git(work, "rev-parse", "origin/main") == head


def test_missed_runs_are_reconciled(remote_and_clone, at):
    _, clone = remote_and_clone
    work = clone()
    at("2026-10-17T21:10:00Z")
    pipe(work, as_round(practice_rows({1}), 49, R49)).run()
    at("2026-10-18T02:10:00Z")  # several hourly rounds appeared while no run happened
    rows = [r for n, rid in enumerate(range(49, 54)) for r in
            as_round(practice_rows({n + 1}), rid, (config.scheduled_release(rid)).isoformat())]
    rep = pipe(work, rows).run()
    assert sorted(rep.solved) == [50, 51, 52, 53] and rep.already_submitted == [49]


def test_incomplete_round_is_held_and_retried(remote_and_clone, at):
    _, clone = remote_and_clone
    work = clone()
    at("2026-10-17T21:10:00Z")
    rows = [r for r in as_round(practice_rows({1}), 49, R49) if r["q_index"] != "25"]
    rep = pipe(work, rows).run()
    assert rep.solved == [] and 49 in rep.incomplete and rep.exit_code == EXIT_NEEDS_REVIEW
    assert remote_answer(work, 49) is None
    m = json.loads(Repo(work).remote_blob(state.MANIFEST))
    assert m["rounds"]["49"]["status"] == "incomplete_in_source" and m["rounds"]["49"]["retries"] == 1


def test_incomplete_round_past_grace_is_submitted_with_flagged_guess(remote_and_clone, at):
    _, clone = remote_and_clone
    work = clone()
    at("2026-10-17T23:30:00Z")
    rows = [r for r in as_round(practice_rows({1}), 49, R49) if r["q_index"] != "25"]
    rep = pipe(work, rows).run()
    assert rep.solved == [49] and rep.needs_review == {49: [25]}
    assert answers.validate_bytes(remote_answer(work, 49), 49) == []


def test_malformed_existing_file_is_replaced(remote_and_clone, at):
    _, clone = remote_and_clone
    work = clone()
    (work / "answers").mkdir()
    (work / "answers/round-0049.json").write_text('{"round_id": 49, "answers": {"q1": "Z"}}')
    git(work, "add", "-A"); git(work, "commit", "--quiet", "-m", "bad"); git(work, "push", "--quiet", "origin", "HEAD:main")
    at("2026-10-17T21:30:00Z")
    rep = pipe(work, as_round(practice_rows({1}), 49, R49)).run()
    assert rep.solved == [49]
    assert answers.validate_bytes(remote_answer(work, 49), 49) == []


def test_hand_edited_valid_file_is_never_overwritten(remote_and_clone, at):
    _, clone = remote_and_clone
    work = clone()
    manual = answers.serialize(answers.build(49, {i: "D" for i in range(1, 26)}))
    (work / "answers").mkdir()
    (work / "answers/round-0049.json").write_bytes(manual)
    git(work, "add", "-A"); git(work, "commit", "--quiet", "-m", "manual"); git(work, "push", "--quiet", "origin", "HEAD:main")
    at("2026-10-17T21:30:00Z")
    rep = pipe(work, as_round(practice_rows({1}), 49, R49)).run()
    assert rep.solved == [] and rep.revised == []
    assert remote_answer(work, 49) == manual


def _submit_with_flag(work, at, flagged_q=3):
    """Submit round 49, then mark q`flagged_q` as a flagged guess with a wrong letter."""
    at("2026-10-17T21:10:00Z")
    rows = as_round(practice_rows({1}), 49, R49)
    pipe(work, rows).run()
    raw = Repo(work).remote_blob(config.answer_path(49))
    letters = answers.letters_of(raw)
    letters[flagged_q] = "ABCD"[("ABCD".index(letters[flagged_q]) + 1) % 4]
    data = answers.serialize(answers.build(49, letters))
    (work / config.answer_path(49)).write_bytes(data)
    m = json.loads(Repo(work).remote_blob(state.MANIFEST))
    m["rounds"]["49"].update(needs_review=[flagged_q], answers_sha256=hashlib.sha256(data).hexdigest())
    (work / state.MANIFEST).write_bytes(state.dump(m))
    git(work, "add", "-A"); git(work, "commit", "--quiet", "-m", "flag"); git(work, "push", "--quiet", "origin", "HEAD:main")
    return rows


def test_flagged_answer_revised_inside_window(remote_and_clone, at, practice_keys):
    _, clone = remote_and_clone
    work = clone()
    rows = _submit_with_flag(work, at)
    at("2026-10-17T22:00:00Z")
    rep = pipe(work, rows).run()
    assert rep.revised == [49]
    assert answers.letters_of(remote_answer(work, 49))[3] == practice_keys[(1, 3)]


def test_no_revision_after_three_hours(remote_and_clone, at, practice_keys):
    _, clone = remote_and_clone
    work = clone()
    rows = _submit_with_flag(work, at)
    before = remote_answer(work, 49)
    at("2026-10-18T00:45:00Z")  # 3h45m after release: locked
    rep = pipe(work, rows).run()
    assert rep.revised == [] and remote_answer(work, 49) == before


def test_no_revision_too_close_to_cutoff(remote_and_clone, at):
    _, clone = remote_and_clone
    work = clone()
    rows = _submit_with_flag(work, at)
    at("2026-10-17T23:50:00Z")  # 2h50m: inside the safety margin
    assert pipe(work, rows).run().revised == []


def test_recover_with_no_revise_never_touches_valid_files(remote_and_clone, at):
    _, clone = remote_and_clone
    work = clone()
    rows = _submit_with_flag(work, at)
    at("2026-10-17T22:00:00Z")
    assert pipe(work, rows, allow_revise=False).run().revised == []


def test_concurrent_runner_pushed_first(remote_and_clone, at):
    _, clone = remote_and_clone
    a, b = clone("a"), clone("b")
    at("2026-10-17T22:10:00Z")
    rows = as_round(practice_rows({1}), 49, R49) + as_round(practice_rows({2}), 50, R50)
    # Runner B fetched before A pushed: simulate by letting A finish, then running B from its stale clone.
    assert pipe(a, rows).run().exit_code == EXIT_OK
    head = git(a, "rev-parse", "HEAD")
    rep = pipe(b, rows).run()
    assert rep.exit_code == EXIT_OK and rep.solved == []
    git(b, "fetch", "--quiet")
    assert git(b, "rev-parse", "origin/main") == head  # nothing duplicated


def test_push_race_rebase_conflict_restarts_from_remote(remote_and_clone, at, monkeypatch):
    _, clone = remote_and_clone
    a, b = clone("a"), clone("b")
    at("2026-10-17T22:10:00Z")
    rows = as_round(practice_rows({1}), 49, R49)
    pb = pipe(b, rows)
    real_sync = Repo.sync_to_remote
    fired = {}

    def racing_sync(self):
        real_sync(self)
        if self.path == b and not fired:
            fired["x"] = True
            assert pipe(a, rows).run().exit_code == EXIT_OK  # A wins the race after B fetched
    monkeypatch.setattr(Repo, "sync_to_remote", racing_sync)
    rep = pb.run()
    assert rep.exit_code == EXIT_OK and rep.errors == []
    assert answers.validate_bytes(remote_answer(b, 49), 49) == []
    log = git(b, "log", "--oneline", "origin/main", "--", "answers/round-0049.json")
    assert len(log.strip().splitlines()) == 1


def test_push_failure_reports_error(remote_and_clone, at, monkeypatch):
    _, clone = remote_and_clone
    work = clone()
    at("2026-10-17T21:10:00Z")

    def boom(self, attempts=5):
        raise GitError("push not authorised: 403")
    monkeypatch.setattr(Repo, "push", boom)
    rep = pipe(work, as_round(practice_rows({1}), 49, R49)).run()
    assert rep.exit_code == EXIT_FAILURE and "403" in rep.errors[0]
    assert remote_answer(work, 49) is None


def test_remote_verification_failure_reports_error(remote_and_clone, at, monkeypatch):
    _, clone = remote_and_clone
    work = clone()
    at("2026-10-17T21:10:00Z")
    monkeypatch.setattr(Repo, "verify_remote", lambda self, exp: {p: False for p in exp})
    rep = pipe(work, as_round(practice_rows({1}), 49, R49)).run()
    assert rep.exit_code == EXIT_FAILURE and "verification failed" in rep.errors[0]


def test_source_failure_is_an_error_not_an_empty_run(remote_and_clone, at):
    _, clone = remote_and_clone
    work = clone()

    def bad():
        raise source.SourceError("HTTP 503")
    rep = Pipeline(work, "production", repo=Repo(work), loader=bad).run()
    assert rep.exit_code == EXIT_FAILURE and rep.solved == []


def test_unchanged_source_is_noop(remote_and_clone, at):
    _, clone = remote_and_clone
    work = clone()
    at("2026-10-17T21:10:00Z")
    rows = practice_rows({1, 2})
    pipe(work, rows).run()
    rep = pipe(work, rows).run()
    assert rep.solved == [] and rep.pushed == [] and rep.exit_code == EXIT_OK


def test_source_lag_detected(remote_and_clone, at):
    _, clone = remote_and_clone
    work = clone()
    at("2026-10-17T22:45:00Z")
    rep = pipe(work, practice_rows({1})).run()
    assert rep.source_lag == [49, 50]


def test_dry_run_never_touches_git(remote_and_clone, at, tmp_path):
    _, clone = remote_and_clone
    work = clone()
    head = git(work, "rev-parse", "HEAD")
    out = tmp_path / "out"
    rep = Pipeline(work, "dry-run", loader=loader_for(practice_rows({1})), out_dir=out).run()
    assert rep.solved == [1] and (out / "answers/round-0001.json").exists()
    assert git(work, "rev-parse", "HEAD") == head and not (work / "answers").exists()


def test_production_guard_blocks_local_and_simulated_runs(monkeypatch):
    from quizbot.__main__ import main
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(SystemExit, match="refusing production"):
        main(["run", "--mode", "production"])
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("QUIZ_NOW", "2026-10-17T21:00:00Z")
    with pytest.raises(SystemExit, match="simulated clock"):
        main(["run", "--mode", "production"])
