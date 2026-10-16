"""QA sections I (deadlines/timing), J (GitHub Actions & cloud independence, static + simulated),
and K (security, privacy, prompt injection). Fixed clocks only; the system clock is never touched."""

import datetime as dt
import json
import logging
import re
from pathlib import Path

import pytest
import yaml

from quizbot import audit, config, http, redact, source
from quizbot.gitops import GitError, Repo
from quizbot.pipeline import EXIT_FAILURE, EXIT_OK, Pipeline
from tests.conftest import as_round, git, practice_rows, to_csv

req = pytest.mark.req
UTC = dt.timezone.utc
ROOT = Path(__file__).parent.parent
WF = ROOT / ".github/workflows"
T0 = dt.datetime(2026, 10, 17, 21, 0, tzinfo=UTC)  # round 49 = 2:00 PM PDT


def h(x):
    return dt.timedelta(hours=x)


# --- I: timing ---------------------------------------------------------------------------------

@req("T01")
def test_t01_ten_minutes():
    assert T0.astimezone(config.PACIFIC).strftime("%H:%M %Z") == "14:00 PDT"
    assert config.timing_credit(T0, T0 + dt.timedelta(minutes=10)) == 1.0


@req("T02", "T03", "T04")
def test_t02_t03_t04_three_hour_boundary():
    # Table: 0-3 -> 100%, 3-4 -> 90%. Exactly 3:00:00 is ambiguous; we take the conservative reading
    # (outside) and never schedule a push near it (20-minute safety margin).
    assert config.timing_credit(T0, T0 + h(3) - dt.timedelta(seconds=1)) == 1.0
    assert config.timing_credit(T0, T0 + h(3)) == 0.9
    assert config.timing_credit(T0, T0 + h(3) + dt.timedelta(seconds=1)) == 0.9
    assert config.graded_version([(T0 + dt.timedelta(hours=2, minutes=59), b"v")], T0)[1] == b"v"
    assert not config.can_revise(T0, T0 + dt.timedelta(hours=2, minutes=41))
    assert config.can_revise(T0, T0 + dt.timedelta(hours=2, minutes=39))


@req("T05")
def test_t05_first_push_after_three_hours_is_not_full_credit():
    v = config.graded_version([(T0 + h(5) + dt.timedelta(minutes=20), b"late")], T0)
    assert v[1] == b"late" and config.timing_credit(T0, v[0]) == 0.7  # assignment example: 5h20m -> 70%


@req("T06", "T07")
def test_t06_t07_which_version_counts():
    v1, v2, v3 = (T0 + h(0.2), b"one"), (T0 + h(2.5), b"two"), (T0 + h(3.5), b"three")
    assert config.graded_version([v1, v2], T0)[1] == b"two"          # last within 3h
    assert config.graded_version([v3, v1, v2], T0)[1] == b"two"      # within-window version kept
    assert config.graded_version([(T0 + h(4), b"a"), (T0 + h(6), b"b")], T0)[1] == b"a"  # first after


@req("T08", "T09")
def test_t08_t09_timing_uses_actual_release_from_source(remote_and_clone, monkeypatch):
    _, clone = remote_and_clone
    w = clone()
    late_release = "2026-10-17T23:00:00Z"  # round 49 actually published 2h late
    rows = as_round(practice_rows({1}), 49, late_release)
    snap = source.parse_questions(to_csv(rows))
    assert snap.rounds[49].released_at == dt.datetime(2026, 10, 17, 23, tzinfo=UTC)
    monkeypatch.setenv("QUIZ_NOW", "2026-10-17T23:05:00Z")  # CSV lag 5 min
    rep = Pipeline(w, "production", repo=Repo(w), loader=lambda: snap).run()
    assert rep.verified == [49] and not any("timing credit at risk" in x for x in rep.warnings)


@req("T10", "T11")
def test_t10_t11_midnight_and_dst_via_tz_database():
    # 5:00 PM PDT Oct 17 is 00:00 UTC Oct 18 (date rolls over in UTC only)
    r52 = config.scheduled_release(52)
    assert (r52.day, r52.hour) == (18, 0) and r52.astimezone(config.PACIFIC).strftime("%d %H") == "17 17"
    # Every graded round is exactly one hour after the previous one, in absolute time.
    rel = [config.scheduled_release(r) for r in config.GRADED_ROUNDS]
    assert all((b - a) == h(1) for a, b in zip(rel, rel[1:]))
    # DST really is handled by the tz database: Nov 1 2026 01:30 PDT -> 01:30 PST is one hour later.
    before = dt.datetime(2026, 11, 1, 8, 30, tzinfo=UTC).astimezone(config.PACIFIC)
    after = dt.datetime(2026, 11, 1, 9, 30, tzinfo=UTC).astimezone(config.PACIFIC)
    assert (before.strftime("%H:%M %Z"), after.strftime("%H:%M %Z")) == ("01:30 PDT", "01:30 PST")
    assert "-07" not in Path(ROOT / "quizbot/config.py").read_text()  # no hard-coded offsets


@req("T12", "S10")
def test_t12_s10_round_168_published_late_is_processed(remote_and_clone, monkeypatch, practice_keys):
    _, clone = remote_and_clone
    w = clone()
    rows = as_round(practice_rows({24}), 168, "2026-10-22T20:00:00Z")
    monkeypatch.setenv("QUIZ_NOW", "2026-10-22T21:40:00Z")  # discovered 1h40m late
    rep = Pipeline(w, "production", repo=Repo(w), loader=lambda: source.parse_questions(to_csv(rows))).run()
    assert rep.verified == [168]
    assert any("round 168" in x and "timing credit at risk" in x for x in rep.warnings)   # T13 too


@req("T13")
def test_t13_late_discovery_reported(remote_and_clone, monkeypatch):
    _, clone = remote_and_clone
    w = clone()
    monkeypatch.setenv("QUIZ_NOW", "2026-10-17T23:30:00Z")
    rep = Pipeline(w, "production", repo=Repo(w),
                   loader=lambda: source.parse_questions(to_csv(as_round(practice_rows({1}), 49, "2026-10-17T21:00:00Z")))).run()
    assert rep.verified == [49] and any("150 min after release" in x for x in rep.warnings)


@req("T14")
def test_t14_receive_time_from_github_not_commit_dates(remote_and_clone):
    """Audit timing comes from the activity API's push timestamps; a forged commit date is ignored."""
    _, clone = remote_and_clone
    w = clone()
    (w / "answers").mkdir()
    from quizbot import answers
    (w / config.answer_path(49)).write_bytes(answers.serialize(answers.build(49, {i: "A" for i in range(1, 26)})))
    git(w, "add", "-A")
    import os
    env = dict(os.environ, GIT_COMMITTER_DATE="2026-10-17T21:01:00Z", GIT_AUTHOR_DATE="2026-10-17T21:01:00Z")
    import subprocess
    subprocess.run(["git", "commit", "-qm", "backdated"], cwd=w, env=env, check=True)
    git(w, "push", "-q", "origin", "HEAD:main")
    sha = git(w, "rev-parse", "HEAD").strip()
    received = "2026-10-18T03:30:00Z"   # GitHub received it 6.5h after release -> table bucket 6-7h = 60%

    def api(path):
        if "activity" in path:
            return [{"activity_type": "push", "timestamp": received, "after": sha}]
        return {"private": False, "full_name": config.EXPECTED_REPO, "default_branch": "main"} if "/actions" not in path else {}
    snap = source.parse_questions(to_csv(as_round(practice_rows({1}), 49, "2026-10-17T21:00:00Z")))
    rep = audit.run_audit(Repo(w), snap, config.EXPECTED_REPO, now=dt.datetime(2026, 10, 18, 5, tzinfo=UTC), api=api)
    assert rep["rounds"][49]["timing_credit"] == 0.6 and rep["rounds"][49]["hours_after_release"] == 6.5


@req("T15", "N09")
def test_t15_after_deadline_reports_honestly(remote_and_clone):
    _, clone = remote_and_clone
    w = clone()
    rep = audit.run_audit(Repo(w), None, None, now=dt.datetime(2026, 10, 23, tzinfo=UTC))
    assert rep["usage_report_due"] is True and "Separate requirement" in rep["usage_report_note"]
    assert rep["counts"]["expected_not_observed"] == 168   # nothing claimed, nothing hidden


# --- J: workflows (static) -----------------------------------------------------------------------

def workflows():
    return {f.name: yaml.safe_load(f.read_text()) for f in WF.glob("*.yml")}


@req("CLOUD01")
def test_cloud01_yaml_valid_and_expected_files():
    w = workflows()
    assert {"poll.yml", "reconcile.yml", "recover.yml", "ci.yml", "llm-check.yml"} <= set(w)
    for name, d in w.items():
        assert d["jobs"] and d.get(True, d.get("on")), name


@req("CLOUD02")
def test_cloud02_github_hosted_only():
    for name, d in workflows().items():
        for job in d["jobs"].values():
            assert job["runs-on"] == "ubuntu-latest", name   # never self-hosted / laptop
            assert job.get("timeout-minutes", 0) > 0, name


@req("CLOUD09")
def test_cloud09_writers_share_one_concurrency_group():
    w = workflows()
    for name in ("poll.yml", "reconcile.yml", "recover.yml"):
        assert w[name]["concurrency"] == {"group": "quiz-submit", "cancel-in-progress": False}


def cron_minutes(expr):
    minute = expr.split()[0]
    if "/" in minute:
        rng, step = minute.split("/")
        start = int(rng.split("-")[0]) if rng != "*" else 0
        return list(range(start, 60, int(step)))
    return [int(minute)]


@req("CLOUD13")
def test_cloud13_cron_cadence_matches_pacific_schedule():
    w = workflows()
    poll = [c["cron"] for c in w["poll.yml"][True]["schedule"]]
    rec = [c["cron"] for c in w["reconcile.yml"][True]["schedule"]]
    assert poll == ["3-59/10 * * * *"] and rec == ["37 * * * *"]
    mins = cron_minutes(poll[0])
    assert mins == [3, 13, 23, 33, 43, 53]
    # Every hour field is '*', so the cadence is identical in UTC and Pacific; for every graded
    # release (top of the hour, Pacific) count poll fires inside the 3h full-credit window, after the
    # source's ~5 min publication lag.
    for r in config.GRADED_ROUNDS:
        rel = config.scheduled_release(r)
        fires = [rel + dt.timedelta(hours=k, minutes=m) for k in range(4) for m in mins]
        usable = [f for f in fires if rel + dt.timedelta(minutes=5) <= f < rel + h(3)]
        assert len(usable) >= 17, r
        assert rel.astimezone(config.PACIFIC).minute == 0


@req("CLOUD16")
def test_cloud16_artifact_upload_cannot_fail_submission():
    steps = workflows()["reconcile.yml"]["jobs"]["reconcile"]["steps"]
    names = [s.get("name", s.get("uses", "")) for s in steps]
    up = next(i for i, s in enumerate(steps) if "upload-artifact" in s.get("uses", ""))
    sub = next(i for i, n in enumerate(names) if n == "Reconcile all rounds")
    assert sub < up and steps[up].get("continue-on-error") is True


@req("SEC07")
def test_sec07_dependencies_and_actions_pinned():
    for line in (ROOT / "requirements.txt").read_text().splitlines() + (ROOT / "requirements-dev.txt").read_text().splitlines():
        if line and not line.startswith("#"):
            assert re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][0-9A-Za-z.]*", line), line
    for f in WF.glob("*.yml"):
        for use in re.findall(r"uses:\s*(\S+)", f.read_text()):
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", use), (f.name, use)
        for npm in re.findall(r"npm install[^\n]*", f.read_text()):
            assert re.search(r"@\d+\.\d+\.\d+", npm) and "curl" not in npm, npm
        assert "| sh" not in f.read_text() and "| bash" not in f.read_text()


# --- J: simulated cloud behaviour ----------------------------------------------------------------

@req("CLOUD06", "CLOUD07", "CLOUD08", "S07")
def test_cloud06_08_delayed_skipped_and_killed_runs_reconciled(remote_and_clone, monkeypatch, practice_keys):
    _, clone = remote_and_clone
    w = clone()
    rows = lambda n: [r for rid, p in zip(range(49, 49 + n), range(1, 49)) for r in  # noqa: E731
                      as_round(practice_rows({p}), rid, config.scheduled_release(rid).isoformat())]
    monkeypatch.setenv("QUIZ_NOW", "2026-10-17T21:10:00Z")
    killed = {}

    def kill_once(self, *a, **k):
        killed[1] = True
        raise KeyboardInterrupt("runner hit timeout-minutes")   # job killed mid-run
    monkeypatch.setattr(Repo, "push", kill_once)
    with pytest.raises(KeyboardInterrupt):
        Pipeline(w, "production", repo=Repo(w), loader=lambda: source.parse_questions(to_csv(rows(1)))).run()
    monkeypatch.undo()
    # next three hourly runs never happened (skipped/delayed); a run 3h later sees rounds 49-52
    monkeypatch.setenv("QUIZ_NOW", "2026-10-18T00:20:00Z")
    rep = Pipeline(w, "production", repo=Repo(w), loader=lambda: source.parse_questions(to_csv(rows(4)))).run()
    assert sorted(rep.verified) == [49, 50, 51, 52] and killed


@req("CLOUD10", "CLOUD15")
@pytest.mark.parametrize("minutes_ago,expect", [(12, 0), (90, 1)])
def test_cloud10_15_watchdog(monkeypatch, capsys, minutes_ago, expect):
    from quizbot import __main__ as cli
    last = (dt.datetime(2026, 10, 18, 12, tzinfo=UTC) - dt.timedelta(minutes=minutes_ago)).isoformat()
    monkeypatch.setenv("QUIZ_NOW", "2026-10-18T12:00:00Z")
    monkeypatch.setattr(cli.audit, "_api", lambda p: {"workflow_runs": [{"updated_at": last, "html_url": "u"}]})
    assert cli.main(["watchdog", "--repo", "x/y"]) == expect


@req("CLOUD10")
def test_cloud10_disabled_workflow_no_runs_after_start_alerts(monkeypatch):
    from quizbot import __main__ as cli
    monkeypatch.setenv("QUIZ_NOW", "2026-10-18T12:00:00Z")
    monkeypatch.setattr(cli.audit, "_api", lambda p: {"workflow_runs": []})
    assert cli.main(["watchdog", "--repo", "x/y"]) == 1


@req("CLOUD11", "CLOUD17")
def test_cloud11_17_no_credentials_in_cloud_is_explicit(monkeypatch, caplog, capsys):
    from quizbot.solver.llm import LLMSolver
    for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "QUIZ_USE_CLAUDE_CLI"):
        monkeypatch.delenv(k, raising=False)
    assert LLMSolver.from_env() is None   # a local login session is never assumed
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "x" * 20)
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.delenv("QUIZ_CLAUDE_BIN", raising=False)
    with caplog.at_level(logging.ERROR):
        assert LLMSolver.from_env() is None
    assert "CLI is not installed" in caplog.text
    from quizbot import __main__ as cli
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    monkeypatch.setattr(cli.source, "fetch_questions", lambda: source.parse_questions(to_csv(practice_rows({1}))))
    monkeypatch.setattr(cli.source, "fetch_keys", lambda: {})
    assert cli.main(["evaluate", "--llm-only", "--report", "/dev/null"]) == 1
    assert "not set" in capsys.readouterr().out


@req("CLOUD18", "N08")
def test_cloud18_green_run_but_round_missing_is_flagged_by_coverage(remote_and_clone):
    _, clone = remote_and_clone
    w = clone()
    snap = source.parse_questions(to_csv(as_round(practice_rows({1}), 49, "2026-10-17T21:00:00Z")))
    rep = audit.run_audit(Repo(w), snap, None, now=dt.datetime(2026, 10, 17, 23, tzinfo=UTC))
    assert rep["rounds"][49]["state"] == "ready_for_processing" and 49 in rep["needs_attention"]


@req("CLOUD20")
def test_cloud20_24_simulated_hours_no_duplicates_or_runaway():
    from tests import simulate_week
    assert simulate_week.main(["--hours", "24", "--seed", "11"]) == 0


# --- K: security -----------------------------------------------------------------------------

@req("SEC03", "SEC10")
def test_sec03_sec10_malicious_csv_is_inert(remote_and_clone, monkeypatch, tmp_path):
    _, clone = remote_and_clone
    w = clone()
    canary = tmp_path / "pwned"
    rows = as_round(practice_rows({1}), 49, "2026-10-17T21:00:00Z")
    rows[0]["question_text"] = (f"$(touch {canary}); `touch {canary}`; __import__('os').system('touch {canary}'); "
                                "{{7*7}} ../../.github/workflows/poll.yml rm -rf / " + rows[0]["question_text"])
    rows[1]["option_a"] = "'; DROP TABLE answers; --"
    monkeypatch.setenv("QUIZ_NOW", "2026-10-17T21:10:00Z")
    before = (w / ".github").exists()
    rep = Pipeline(w, "production", repo=Repo(w), loader=lambda: source.parse_questions(to_csv(rows))).run()
    assert rep.verified == [49] and not canary.exists()
    git(w, "fetch", "--quiet")
    tree = git(w, "ls-tree", "-r", "--name-only", "origin/main").split()
    assert all(p == "README.md" or p.startswith(("answers/", "state/")) for p in tree)
    assert (w / ".github").exists() == before


SECRET = "ghp_" + "A1b2C3d4" * 5


@req("SEC04", "SEC05", "SEC08")
def test_sec04_05_08_secrets_redacted_everywhere(monkeypatch, caplog, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", SECRET)
    msgs = [f"remote: auth failed for https://x-access-token:{SECRET}@github.com/a/b.git",
            f"Authorization: Bearer {SECRET}", "key sk-ant-api03-" + "Z9" * 20, f"token={SECRET}"]
    for m in msgs:
        assert SECRET not in redact.redact(m) and "sk-ant-api03-Z9Z9" not in redact.redact(m)
    logging.getLogger().addHandler(caplog.handler)
    redact.install()
    logging.getLogger("t").error("git push failed: %s", msgs[0])
    assert SECRET not in caplog.text and "[REDACTED]" in caplog.text
    err = GitError("x")
    from quizbot.gitops import redact as git_redact
    assert SECRET not in git_redact(msgs[0])


@req("SEC06")
@pytest.mark.parametrize("rid", ["../../etc/passwd", "49/../../x", "-1", "0", "169", "1e3"])
def test_sec06_no_path_traversal(rid):
    rows = as_round(practice_rows({1}), 49, "2026-10-17T21:00:00Z")
    for r in rows:
        r["round_id"] = rid
    snap = source.parse_questions(to_csv(rows + as_round(practice_rows({2}), 50, "2026-10-17T22:00:00Z")))
    for r in snap.rounds:
        assert isinstance(r, int) and re.fullmatch(r"answers/round-\d{4,}\.json", config.answer_path(r))
    assert all(r >= 1 for r in snap.rounds)


@req("CLOUD06", "CLOUD07")
def test_keepalive_chain_is_cron_independent_bounded_and_single():
    k = workflows()["keepalive.yml"]
    on = k[True]
    assert "workflow_dispatch" in on                                  # chain links use dispatch, not cron
    assert k["permissions"] == {"actions": "write", "contents": "read"}   # cannot push answers itself
    assert k["concurrency"] == {"group": "keepalive", "cancel-in-progress": False}  # never >1 running
    job = k["jobs"]["tick"]
    assert job["timeout-minutes"] < 360
    steps = job["steps"]
    assert "keepalive.yml" in steps[0]["run"] and "STOP_AT" in steps[0]["run"]   # successor queued FIRST
    assert "poll.yml" in steps[1]["run"] and "sleep 600" in steps[1]["run"]
    stop = dt.datetime.fromisoformat(job["env"]["STOP_AT"].replace("Z", "+00:00"))
    assert config.scheduled_release(168) + h(3) < stop < config.scheduled_release(168) + h(12)


@req("CLOUD11")
def test_cloud11_optional_llm_installs_can_never_block_submission():
    """Adding a Claude credential must not create a new way for a run to fail before submitting."""
    for name in ("poll.yml", "reconcile.yml", "recover.yml"):
        job = next(iter(workflows()[name]["jobs"].values()))
        optional = [s for s in job["steps"] if str(s.get("if", "")).startswith(("env.HAS_LLM", "env.HAS_CLAUDE"))]
        assert len(optional) == 2, name
        for s in optional:
            assert s.get("continue-on-error") is True and 0 < s.get("timeout-minutes", 0) <= 10, (name, s)
