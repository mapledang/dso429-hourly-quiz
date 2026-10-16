"""QA sections L (failure injection at every boundary) and M (end-to-end scenarios).

Pattern for every injected failure:
  durable state before -> inject -> check state after -> restart -> recovery verified ->
  answers equal the official keys (nothing fabricated) -> pre-existing valid files untouched ->
  the error was visible (exception or non-zero exit with a message)."""

import json
import logging
import os

import pytest

import quizbot.pipeline as pl
from quizbot import answers, config, source, state
from quizbot.gitops import GitError, Repo
from quizbot.pipeline import EXIT_FAILURE, EXIT_NEEDS_REVIEW, EXIT_OK, Pipeline
from tests.conftest import as_round, git, practice_rows, to_csv

req = pytest.mark.req


def rel(rid):
    return config.scheduled_release(rid).isoformat()


def rows_for(*rids):
    return [r for rid in rids for r in as_round(practice_rows({(rid - 49) % 48 + 1}), rid, rel(rid))]


def run(w, rows, **kw):
    raw = to_csv(rows)
    return Pipeline(w, "production", repo=Repo(w, sleep=lambda s: None),
                    loader=lambda: source.parse_questions(raw), **kw).run()


def remote(w, path):
    git(w, "fetch", "--quiet")
    return Repo(w).remote_blob(path)


def all_correct(w, rids, keys):
    for rid in rids:
        raw = remote(w, config.answer_path(rid))
        if raw is None or answers.validate_bytes(raw, rid):
            return False
        if any(v != keys[((rid - 49) % 48 + 1, q)] for q, v in answers.letters_of(raw).items()):
            return False
    return True


@pytest.fixture
def world(remote_and_clone, monkeypatch, practice_keys):
    """Round 49 already submitted (pre-existing valid file); round 50 is the one under test."""
    _, clone = remote_and_clone
    w = clone()
    monkeypatch.setenv("QUIZ_NOW", "2026-10-17T22:10:00Z")
    assert run(w, rows_for(49)).verified == [49]
    return w, remote(w, config.answer_path(49)), practice_keys


def boom(msg):
    def f(*a, **k):
        raise RuntimeError(msg)
    return f


BOUNDARIES = {
    "F01": ("quizbot.answers.write_atomic", "before writing"),
    "F03": ("quizbot.answers.validate_bytes", "after write, during validation"),
    "F04": ("quizbot.gitops.Repo.commit", "before staging"),
    "F06": ("quizbot.gitops.Repo.push", "after commit, before push"),
    "F08": ("quizbot.gitops.Repo.verify_remote", "after push, before verification"),
    "F09": ("quizbot.pipeline.Pipeline._commit_state", "after verification, before manifest"),
    "F13": ("quizbot.state.write", "while writing logs/state"),
}


@pytest.mark.parametrize("fid", sorted(BOUNDARIES))
def test_failure_boundaries(world, monkeypatch, fid, request):
    request.node.add_marker(pytest.mark.req(fid))
    w, before49, keys = world
    target, where = BOUNDARIES[fid]
    mod, _, attr = target.rpartition(".")
    if mod.endswith(("Repo", "Pipeline")):
        owner = Repo if mod.endswith("Repo") else Pipeline
        monkeypatch.setattr(owner, attr, boom(f"injected {fid}: {where}"))
    else:
        import importlib
        monkeypatch.setattr(importlib.import_module(mod), attr, boom(f"injected {fid}: {where}"))
        if mod == "quizbot.answers":   # pipeline imported the module, patch is visible through it
            pass
    # 1. inject -> the failure must be visible (exception or failing exit code)
    try:
        rep = run(w, rows_for(49, 50))
        visible = rep.exit_code != EXIT_OK and (rep.errors or rep.round_errors)
    except RuntimeError as exc:
        visible = "injected" in str(exc)
    assert visible, f"{fid} failure was swallowed"
    # 2. pre-existing valid submission untouched
    assert remote(w, config.answer_path(49)) == before49
    # 3. restart without the fault -> recovery, correct answers, no duplicate commit for round 50
    monkeypatch.undo()
    os.environ["QUIZ_NOW"] = "2026-10-17T22:20:00Z"
    rep = run(w, rows_for(49, 50))
    assert rep.exit_code == EXIT_OK and all_correct(w, [49, 50], keys)
    log = git(w, "log", "--oneline", "origin/main", "--", config.answer_path(50)).strip().splitlines()
    assert len(log) == 1


@req("F05")
def test_f05_after_staging_before_commit(world, monkeypatch):
    w, before49, keys = world
    real = Repo.git

    def git_(self, *args, check=True, input=None):
        if args and args[0] == "commit":
            raise RuntimeError("injected F05: index staged, commit never happened")
        return real(self, *args, check=check, input=input)
    monkeypatch.setattr(Repo, "git", git_)
    with pytest.raises(RuntimeError):
        run(w, rows_for(49, 50))
    monkeypatch.undo()
    assert run(w, rows_for(49, 50)).verified == [50] and all_correct(w, [49, 50], keys)


@req("F07")
def test_f07_server_received_client_did_not(world, monkeypatch):
    w, before49, keys = world
    real = Repo.push

    def push(self, attempts=5):
        real(self, attempts)
        raise GitError("injected F07: connection dropped after server accepted the push")
    monkeypatch.setattr(Repo, "push", push)
    rep = run(w, rows_for(49, 50))
    assert rep.exit_code == EXIT_FAILURE
    monkeypatch.undo()
    rep = run(w, rows_for(49, 50))
    assert rep.solved == [] and all_correct(w, [49, 50], keys)   # remote state recognised: no duplicate
    assert len(git(w, "log", "--oneline", "origin/main", "--", config.answer_path(50)).strip().splitlines()) == 1


@req("F11")
def test_f11_failure_while_solving_q25(world, monkeypatch):
    w, before49, keys = world
    import quizbot.solver as solver_mod
    real = solver_mod.template_solve

    def ts(q_index, question, options):
        if q_index == 25:
            raise RuntimeError("injected F11: crash on q25")
        return real(q_index, question, options)
    monkeypatch.setattr(solver_mod, "template_solve", ts)
    rep = run(w, rows_for(49, 50))
    assert rep.exit_code == EXIT_FAILURE and 50 in rep.round_errors and remote(w, config.answer_path(50)) is None
    monkeypatch.undo()
    assert run(w, rows_for(49, 50)).verified == [50] and all_correct(w, [50], keys)


@req("F14", "F15")
def test_f14_f15_optional_side_channels_cannot_block_submission():
    """Artifacts are continue-on-error; notifications are GitHub's own failure e-mails, sent
    after the job ends - neither sits between solving and pushing."""
    import yaml
    from pathlib import Path
    wf = yaml.safe_load((Path(__file__).parent.parent / ".github/workflows/reconcile.yml").read_text())
    steps = wf["jobs"]["reconcile"]["steps"]
    art = [s for s in steps if "upload-artifact" in s.get("uses", "")]
    assert art and all(s.get("continue-on-error") for s in art)
    poll = yaml.safe_load((Path(__file__).parent.parent / ".github/workflows/poll.yml").read_text())
    assert not any("notify" in json.dumps(s).lower() or "slack" in json.dumps(s).lower()
                   for s in poll["jobs"]["submit"]["steps"])


@req("F16")
def test_f16_terminated_with_several_rounds_pending(world, monkeypatch):
    w, before49, keys = world
    calls = {"n": 0}
    real = pl._solve_with_gaps

    def slow(rnd, llm):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt("SIGTERM: job cancelled")
        return real(rnd, llm)
    monkeypatch.setattr(pl, "_solve_with_gaps", slow)
    monkeypatch.setenv("QUIZ_NOW", "2026-10-18T00:10:00Z")
    with pytest.raises(KeyboardInterrupt):
        run(w, rows_for(49, 50, 51, 52))
    monkeypatch.undo()
    os.environ["QUIZ_NOW"] = "2026-10-18T00:20:00Z"
    rep = run(w, rows_for(49, 50, 51, 52))
    assert sorted(rep.verified) == [50, 51, 52] and all_correct(w, [49, 50, 51, 52], keys)
    assert remote(w, config.answer_path(49)) == before49


@req("F19")
def test_f19_200_with_malformed_csv(world):
    w, before49, keys = world
    malformed = b'round_id,q_index,released_at,question_text,option_a,option_b,option_c,option_d\n50,1,"2026'

    def loader():
        return source.parse_questions(malformed)   # raises SourceError (unterminated quote)
    rep = Pipeline(w, "production", repo=Repo(w), loader=loader).run()
    assert rep.exit_code == EXIT_FAILURE and "source unavailable/invalid" in rep.errors[0]
    assert remote(w, config.answer_path(49)) == before49 and remote(w, config.answer_path(50)) is None


# --- M: end-to-end scenarios -----------------------------------------------------------------

@req("S02")
def test_s02_five_rounds_together(remote_and_clone, monkeypatch, practice_keys):
    _, clone = remote_and_clone
    w = clone()
    monkeypatch.setenv("QUIZ_NOW", "2026-10-18T01:10:00Z")
    rep = run(w, rows_for(49, 50, 51, 52, 53))
    assert sorted(rep.verified) == [49, 50, 51, 52, 53] and all_correct(w, range(49, 54), practice_keys)


@req("S03")
def test_s03_one_incomplete_round(remote_and_clone, monkeypatch, practice_keys):
    _, clone = remote_and_clone
    w = clone()
    monkeypatch.setenv("QUIZ_NOW", "2026-10-17T23:10:00Z")
    rows = [r for r in rows_for(49, 50, 51) if not (r["round_id"] == "50" and r["q_index"] == "20")]
    rep = run(w, rows)
    assert sorted(rep.verified) == [49, 51] and 50 in rep.incomplete and rep.exit_code == EXIT_NEEDS_REVIEW
    assert remote(w, config.answer_path(50)) is None


@req("S09")
def test_s09_hard_question_follows_fallback_policy(remote_and_clone, monkeypatch, practice_keys):
    """An unrecognisable question (no LLM configured) is answered by the documented flagged
    heuristic, the round is still submitted on time, the run exits NEEDS REVIEW, and the flag is
    recorded for the 3-hour correction window. The other 24 answers are the true keys."""
    _, clone = remote_and_clone
    w = clone()
    monkeypatch.setenv("QUIZ_NOW", "2026-10-17T21:10:00Z")
    rows = rows_for(49)
    rows[6]["question_text"] = "Which accounting principle governs revenue recognition timing?"
    rows[6].update(option_a="Matching", option_b="Accrual", option_c="Going concern", option_d="Materiality")
    rep = run(w, rows)
    assert rep.verified == [49] and rep.needs_review == {49: [7]} and rep.exit_code == EXIT_NEEDS_REVIEW
    got = answers.letters_of(remote(w, config.answer_path(49)))
    assert all(got[q] == practice_keys[(1, q)] for q in range(1, 26) if q != 7)
    log = json.loads(remote(w, state.round_log_path(49)))
    assert next(q for q in log["questions"] if q["q_index"] == 7)["status"] == "guess"
