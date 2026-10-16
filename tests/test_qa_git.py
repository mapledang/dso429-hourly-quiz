"""QA section H: git and remote submission (temporary bare remote; never the real repo)."""

import json
import subprocess

import pytest

from quizbot import answers, audit, config, source, state
from quizbot.gitops import GitError, Repo
from quizbot.pipeline import EXIT_FAILURE, EXIT_OK, Pipeline
from tests.conftest import as_round, git, practice_rows, to_csv

req = pytest.mark.req
REL = "2026-10-17T21:00:00Z"


def rows49():
    return as_round(practice_rows({1}), 49, REL)


def run(work, rows=None, **kw):
    raw = to_csv(rows or rows49())
    return Pipeline(work, "production", repo=Repo(work, sleep=lambda s: None),
                    loader=lambda: source.parse_questions(raw), **kw).run()


def remote(work, path):
    git(work, "fetch", "--quiet")
    return Repo(work).remote_blob(path)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setenv("QUIZ_NOW", "2026-10-17T21:10:00Z")


def fake_push(monkeypatch, behaviour):
    """Intercept only `git push`; `behaviour(real_run)` returns a CompletedProcess."""
    real = Repo.git
    calls = {"n": 0}

    def git_(self, *args, check=True, input=None):
        if args and args[0] == "push":
            calls["n"] += 1
            proc = behaviour(lambda: real(self, *args, check=False), calls["n"])
            if check and proc.returncode:
                raise GitError(proc.stderr.decode())
            return proc
        return real(self, *args, check=check, input=input)
    monkeypatch.setattr(Repo, "git", git_)
    return calls


def failed(stderr):
    return subprocess.CompletedProcess(["git", "push"], 1, b"", stderr.encode())


@req("G01", "S01")
def test_g01_push_and_verify(remote_and_clone):
    _, clone = remote_and_clone
    w = clone()
    rep = run(w)
    assert rep.verified == [49] and answers.validate_bytes(remote(w, config.answer_path(49)), 49) == []


@req("G02", "S04")
def test_g02_transient_push_failure_retried(remote_and_clone, monkeypatch):
    _, clone = remote_and_clone
    w = clone()
    calls = fake_push(monkeypatch, lambda real, n: failed("fatal: unable to access: Connection reset by peer")
                      if n == 1 else real())
    rep = run(w)
    assert rep.verified == [49] and calls["n"] >= 2 and rep.errors == []


@req("G04")
@pytest.mark.parametrize("stderr", ["fatal: could not read Username for 'https://github.com': terminal prompts disabled",
                                    "remote: Invalid username or password.\nfatal: Authentication failed"])
def test_g04_missing_auth_fails_fast_and_clearly(remote_and_clone, monkeypatch, stderr):
    _, clone = remote_and_clone
    w = clone()
    calls = fake_push(monkeypatch, lambda real, n: failed(stderr))
    rep = run(w)
    assert rep.exit_code == EXIT_FAILURE and calls["n"] == 1 and "not authorised" in rep.errors[0]


@req("G05", "CLOUD12")
def test_g05_read_only_token_detected(remote_and_clone, monkeypatch):
    _, clone = remote_and_clone
    w = clone()
    fake_push(monkeypatch, lambda real, n: failed(
        "remote: Permission to mapledang/dso429-hourly-quiz.git denied to github-actions[bot].\n"
        "fatal: unable to access: The requested URL returned error: 403"))
    rep = run(w)
    assert rep.exit_code == EXIT_FAILURE and "contents: write" in rep.errors[0]


def fake_api(meta):
    def api(path):
        if path.endswith("/activity") or "activity?" in path:
            return []
        if "/actions/runs" in path:
            return {"workflow_runs": []}
        return meta
    return api


@req("G06", "G07", "N04")
@pytest.mark.parametrize("meta,problem", [
    ({"private": True, "full_name": "mapledang/dso429-hourly-quiz", "default_branch": "main"}, "NOT public"),
    ({"private": False, "full_name": "mapledang/dso429-hourly-quiz", "default_branch": "master"}, "default branch"),
    ({"private": False, "full_name": "someone/renamed-repo", "default_branch": "main"}, "registered as"),
    (None, "not reachable"),
])
def test_g06_g07_audit_flags_repo_problems(remote_and_clone, meta, problem):
    _, clone = remote_and_clone
    w = clone()
    rep = audit.run_audit(Repo(w), None, "mapledang/dso429-hourly-quiz", api=fake_api(meta))
    assert any(problem in p for p in rep["problems"])


@req("G03", "G08", "G18", "S06", "F20")
def test_g08_g18_two_workers_race(remote_and_clone, monkeypatch):
    _, clone = remote_and_clone
    a, b = clone("a"), clone("b")
    real_sync = Repo.sync_to_remote
    fired = {}

    def racing(self):
        real_sync(self)
        if self.path == b and not fired:
            fired[1] = True
            assert run(a).exit_code == EXIT_OK   # A pushes after B fetched
    monkeypatch.setattr(Repo, "sync_to_remote", racing)
    rep = run(b)
    assert rep.exit_code == EXIT_OK
    log = git(b, "log", "--oneline", "origin/main", "--", config.answer_path(49))
    assert len(log.strip().splitlines()) == 1


@req("G09", "F07", "S05")
def test_g09_push_landed_but_client_lost_confirmation(remote_and_clone, monkeypatch):
    _, clone = remote_and_clone
    w = clone()

    def behaviour(real, n):
        if n == 1:
            real()   # the server receives the push...
            return failed("fatal: the remote end hung up unexpectedly (timeout)")  # ...client never hears
        return real()
    fake_push(monkeypatch, behaviour)
    rep = run(w)
    assert rep.exit_code == EXIT_OK and rep.verified == [49]
    assert len(git(w, "log", "--oneline", "origin/main", "--", config.answer_path(49)).strip().splitlines()) == 1


@req("G10", "SEC09")
def test_g10_git_claims_success_but_file_absent(remote_and_clone, monkeypatch):
    _, clone = remote_and_clone
    w = clone()
    fake_push(monkeypatch, lambda real, n: subprocess.CompletedProcess(["git"], 0, b"", b""))  # lies
    rep = run(w)
    assert rep.exit_code == EXIT_FAILURE and "verification failed" in " ".join(rep.errors)


@req("G11", "V17")
def test_g11_v17_remote_differs_from_validated(remote_and_clone, monkeypatch):
    _, clone = remote_and_clone
    w, other = clone("w"), clone("other")

    def behaviour(real, n):
        proc = real()
        if n == 1:  # someone rewrites the file right after our push, before verification
            git(other, "pull", "-q", "origin", "main")
            (other / config.answer_path(49)).write_bytes(answers.serialize(answers.build(49, {i: "D" for i in range(1, 26)})))
            git(other, "commit", "-qam", "tamper"); git(other, "push", "-q", "origin", "HEAD:main")
        return proc
    fake_push(monkeypatch, behaviour)
    rep = run(w)
    assert rep.exit_code == EXIT_FAILURE and "verification failed for rounds [49]" in " ".join(rep.errors)


@req("V16")
def test_v16_changed_after_validation_before_commit_is_not_pushed(remote_and_clone, monkeypatch):
    _, clone = remote_and_clone
    w = clone()
    real_commit = Repo.commit

    def tampering_commit(self, paths, message):
        (self.path / config.answer_path(49)).write_text('{"round_id": 49, "answers": {}}')
        return real_commit(self, paths, message)
    monkeypatch.setattr(Repo, "commit", tampering_commit)
    rep = run(w)
    assert rep.exit_code == EXIT_FAILURE and "differs from validated" in " ".join(rep.errors)
    assert remote(w, config.answer_path(49)) is None


@req("G12")
def test_g12_no_empty_commit(remote_and_clone):
    _, clone = remote_and_clone
    w = clone()
    run(w)
    assert Repo(w).commit([config.answer_path(49)], "noop") is False


@req("G13")
def test_g13_no_recursive_triggers():
    import yaml
    wf = config_dir = __import__("pathlib").Path(__file__).parent.parent / ".github/workflows"
    for f in wf.glob("*.yml"):
        d = yaml.safe_load(f.read_text())
        on = d.get(True, d.get("on"))
        writes = d.get("permissions", {}).get("contents") == "write"
        if writes:   # anything that pushes must never be triggered by a push
            assert "push" not in on and "workflow_run" not in on, f.name
    ci = yaml.safe_load((wf / "ci.yml").read_text())
    assert ci["permissions"] == {"contents": "read"}


@req("G14")
def test_g14_renamed_or_transferred_repo_refused(monkeypatch):
    from quizbot.__main__ import main
    monkeypatch.delenv("QUIZ_NOW", raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "someone-else/fork-of-quiz")
    with pytest.raises(SystemExit, match="registered repository"):
        main(["run", "--mode", "production"])


@req("G15")
def test_g15_unrelated_files_not_staged(remote_and_clone):
    _, clone = remote_and_clone
    w = clone()
    (w / "my_notes.txt").write_text("private scratch work\n")
    run(w)
    git(w, "fetch", "--quiet")
    tree = git(w, "ls-tree", "-r", "--name-only", "origin/main").split()
    assert "my_notes.txt" not in tree and (w / "my_notes.txt").exists()
    assert all(p == "README.md" or p.startswith(("answers/", "state/")) for p in tree)


@req("G16")
def test_g16_several_rounds_one_push(remote_and_clone, monkeypatch):
    _, clone = remote_and_clone
    w = clone()
    monkeypatch.setenv("QUIZ_NOW", "2026-10-17T23:10:00Z")
    rows = [r for rid, p in ((49, 1), (50, 2), (51, 3)) for r in as_round(practice_rows({p}), rid, REL)]
    rep = run(w, rows)
    msgs = git(w, "log", "--format=%s", "origin/main").splitlines()
    assert sorted(rep.verified) == [49, 50, 51] and sum(m.startswith("answers:") for m in msgs) == 1


@req("G17", "F12", "S08")
def test_g17_one_bad_round_others_still_submitted(remote_and_clone, monkeypatch, practice_keys):
    import quizbot.pipeline as pl
    _, clone = remote_and_clone
    w = clone()
    monkeypatch.setenv("QUIZ_NOW", "2026-10-17T23:10:00Z")
    real = pl._solve_with_gaps

    def flaky(rnd, llm):
        if rnd.round_id == 50:
            raise ValueError("solver produced an invalid payload")
        return real(rnd, llm)
    monkeypatch.setattr(pl, "_solve_with_gaps", flaky)
    rows = [r for rid, p in ((49, 1), (50, 2), (51, 3), (52, 4), (53, 5)) for r in as_round(practice_rows({p}), rid, REL)]
    rep = run(w, rows)
    assert sorted(rep.verified) == [49, 51, 52, 53] and 50 in rep.round_errors and rep.exit_code == EXIT_FAILURE
    assert remote(w, config.answer_path(50)) is None
    assert json.loads(remote(w, state.MANIFEST))["rounds"]["50"]["status"] == "solve_failed"


@req("G19")
def test_g19_push_retries_exhausted_then_next_run_recovers(remote_and_clone, monkeypatch):
    _, clone = remote_and_clone
    w = clone()
    calls = fake_push(monkeypatch, lambda real, n: failed("fatal: unable to access: Could not resolve host"))
    rep = run(w)
    assert rep.exit_code == EXIT_FAILURE and calls["n"] == 5 and remote(w, config.answer_path(49)) is None
    monkeypatch.undo()
    monkeypatch.setenv("QUIZ_NOW", "2026-10-17T21:20:00Z")
    assert run(w).verified == [49]


@req("G20", "F10")
def test_g20_github_unreachable_reported_then_recovers(remote_and_clone, monkeypatch):
    _, clone = remote_and_clone
    w = clone()
    real = Repo.git

    def down(self, *args, check=True, input=None):
        if args and args[0] == "fetch":
            return subprocess.CompletedProcess(["git"], 128, b"", b"fatal: unable to access github.com: 503")
        return real(self, *args, check=check, input=input)
    monkeypatch.setattr(Repo, "git", down)
    rep = run(w)
    assert rep.exit_code == EXIT_FAILURE and "GitHub unavailable" in rep.errors[0]
    monkeypatch.setattr(Repo, "git", real)
    assert run(w).verified == [49]
