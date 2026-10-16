"""End-to-end pipeline tests against a local bare git remote standing in for GitHub."""

import hashlib
import json

import pytest

from types import SimpleNamespace

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
    log_path = work / state.round_log_path(49)
    rlog = json.loads(log_path.read_text())
    for q in rlog["questions"]:
        if q["q_index"] == flagged_q:
            q["status"] = "guess"
    log_path.write_text(json.dumps(rlog))
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


# --- unfamiliar graded questions + slow/broken Claude: must still submit, then upgrade ---------

from quizbot.solver.llm import ClaudeCLISolver  # noqa: E402

UNKNOWN_TEXT = "Q{n}: A widget costs $40.00 to make and $10.00 to ship. What is the total cost of 3 widgets?"


def _unknown_round(rid, released):
    rows = as_round(practice_rows({1}), rid, released)
    for r in rows:
        r.update(question_text=UNKNOWN_TEXT.format(n=r["q_index"]),
                 option_a="$150.00", option_b="$120.00", option_c="$130.00", option_d="$50.00")
    return rows


class ScriptedCLI(ClaudeCLISolver):
    """Claude backend whose behaviour (hang / answer) is scripted per run, no subprocesses."""

    def __init__(self, mode, **kw):
        super().__init__(runner=self._run, **kw)
        self.mode = mode

    def _run(self, cmd, **kw):
        import subprocess, time as _t
        if self.mode == "hang":
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        out = {"letter": "A", "result": "150", "expression": "3*(40+10)", "method": "m"}
        return SimpleNamespace(returncode=0, stdout=json.dumps(
            {"type": "result", "subtype": "success", "is_error": False, "structured_output": out}))


def test_time_budget_still_submits_round_with_flagged_guesses(remote_and_clone, at):
    _, clone = remote_and_clone
    work = clone()
    at("2026-10-17T21:10:00Z")
    fake_now = [0.0]

    def clock():
        fake_now[0] += 100.0  # every check advances 100s: budget of 480s runs out after a few calls
        return fake_now[0]

    llm = ScriptedCLI("hang", time_budget=480, clock=clock)
    rep = pipe(work, _unknown_round(49, R49), llm=llm).run()
    assert rep.solved == [49] and rep.verified == [49]          # submitted despite Claude failing
    assert rep.needs_review[49] == list(range(1, 26)) and rep.exit_code == EXIT_NEEDS_REVIEW
    assert llm.calls < 25                                        # budget stopped further calls


def test_flagged_guesses_upgraded_by_later_run_then_locked(remote_and_clone, at):
    _, clone = remote_and_clone
    work = clone()
    rows = _unknown_round(49, R49)
    at("2026-10-17T21:10:00Z")
    pipe(work, rows, llm=ScriptedCLI("hang")).run()            # run 1: all guesses (median heuristic)
    at("2026-10-17T21:20:00Z")
    rep = pipe(work, rows, llm=ScriptedCLI("ok")).run()          # run 2: Claude works -> upgrade
    assert rep.revised == [49] and rep.exit_code == EXIT_OK
    assert set(answers.letters_of(remote_answer(work, 49)).values()) == {"A"}
    m = json.loads(Repo(work).remote_blob(state.MANIFEST))
    assert m["rounds"]["49"]["needs_review"] == []
    head = git(work, "rev-parse", "origin/main")
    at("2026-10-17T21:30:00Z")
    rep = pipe(work, rows, llm=ScriptedCLI("ok")).run()          # run 3: nothing left to do
    git(work, "fetch", "--quiet")
    assert rep.revised == [] and git(work, "rev-parse", "origin/main") == head


def test_flagged_guesses_not_upgraded_after_window(remote_and_clone, at):
    _, clone = remote_and_clone
    work = clone()
    rows = _unknown_round(49, R49)
    at("2026-10-17T21:10:00Z")
    pipe(work, rows, llm=ScriptedCLI("hang")).run()
    before = remote_answer(work, 49)
    at("2026-10-18T00:30:00Z")                                   # 3.5h after release: locked
    assert pipe(work, rows, llm=ScriptedCLI("ok")).run().revised == []
    assert remote_answer(work, 49) == before


def test_source_text_change_resolves_only_that_question(remote_and_clone, at, practice_keys):
    _, clone = remote_and_clone
    work = clone()
    rows = as_round(practice_rows({1}), 49, R49)
    at("2026-10-17T21:10:00Z")
    pipe(work, rows).run()
    # Instructor corrects q2 by swapping it for practice round 2's q2 (different numbers/answer).
    replacement = next(r for r in practice_rows({2}) if r["q_index"] == "2")
    rows2 = [dict(replacement, round_id="49", released_at=R49) if r["q_index"] == "2" else r for r in rows]
    at("2026-10-17T21:40:00Z")
    rep = pipe(work, rows2).run()
    assert rep.revised == [49]
    got = answers.letters_of(remote_answer(work, 49))
    assert got[2] == practice_keys[(2, 2)]
    assert all(got[q] == practice_keys[(1, q)] for q in range(1, 26) if q != 2)


def test_parallel_llm_respects_call_cap():
    from quizbot.solver import solve_round
    llm = ScriptedCLI("ok", max_calls=5)
    qs = [{"q_index": i, "question_text": UNKNOWN_TEXT.format(n=i), "option_a": "$150.00",
           "option_b": "$120.00", "option_c": "$130.00", "option_d": "$50.00"} for i in range(1, 26)]
    res = solve_round(qs, llm)
    assert llm.calls == 5 and sum(r.status == "llm_verified" for r in res) == 5
    assert sum(r.status == "guess" for r in res) == 20 and len(res) == 25
