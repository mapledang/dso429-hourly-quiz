"""QA section N: final assignment audit over rounds 1-168 (temporary remote, fixed clock)."""

import datetime as dt
import hashlib
import json

import pytest

from quizbot import answers, audit, config, source, state
from quizbot.gitops import Repo
from tests.conftest import as_round, git, practice_rows, to_csv

req = pytest.mark.req
UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 18, 1, 30, tzinfo=UTC)   # rounds 49-53 scheduled, 54+ future except as set up


def good(rid, letters=None):
    return answers.serialize(answers.build(rid, letters or {i: "A" for i in range(1, 26)}))


@pytest.fixture(scope="module")
def audited(tmp_path_factory, practice_keys):
    tmp = tmp_path_factory.mktemp("audit")
    bare = tmp / "r.git"
    git(tmp, "init", "-q", "--bare", "-b", "main", str(bare))
    w = tmp / "w"
    git(tmp, "clone", "-q", str(bare), str(w))
    git(w, "config", "user.email", "a@b"); git(w, "config", "user.name", "a")
    (w / "answers").mkdir(); (w / "state").mkdir()
    files = {}
    # practice 1: correct answers from the keys; practice 2: all "A" (partly wrong)
    files[config.answer_path(1)] = good(1, {q: practice_keys[(1, q)] for q in range(1, 26)})
    files[config.answer_path(2)] = good(2)
    files[config.answer_path(49)] = good(49)                       # submitted_verified
    files[config.answer_path(53)] = b'{"round_id": 53, "answers": {"q1": "Z"}}'   # validation_failed
    files[config.answer_path(55)] = good(55)                       # requires_review
    files[config.answer_path(56)] = good(56, {i: "D" for i in range(1, 26)})      # hand-edited
    files["answers/round-57.json"] = good(57)                      # misnamed (3-digit padding)
    files["answers/round-0170.json"] = good(170)                   # out of range
    files["answers/notes.txt"] = b"scratch"                        # stray file
    files["config/leak.env"] = b"TOKEN=ghp_" + b"Q" * 36 + b"\n"   # planted secret
    manifest = state.empty()
    manifest["rounds"] = {
        "49": {"status": "submitted", "answers_sha256": hashlib.sha256(good(49)).hexdigest(), "verified_at": "x"},
        "52": {"status": "solve_failed"},
        "54": {"status": "submitted"},                             # push_failed: no file on remote
        "55": {"status": "submitted", "answers_sha256": hashlib.sha256(good(55)).hexdigest(), "needs_review": [7]},
        "56": {"status": "submitted", "answers_sha256": hashlib.sha256(good(56)).hexdigest()},
    }
    files[state.MANIFEST] = state.dump(manifest)
    for p, data in files.items():
        (w / p).parent.mkdir(parents=True, exist_ok=True)
        (w / p).write_bytes(data)
    git(w, "add", "-A"); git(w, "commit", "-qm", "fixture"); git(w, "push", "-q", "origin", "HEAD:main")
    rows = (practice_rows({1, 2}) + as_round(practice_rows({3}), 49, "2026-10-17T21:00:00Z")
            + [r for r in as_round(practice_rows({4}), 50, "2026-10-17T22:00:00Z") if r["q_index"] != "9"]
            + as_round(practice_rows({5}), 51, "2026-10-17T23:00:00Z")
            + as_round(practice_rows({6}), 52, "2026-10-18T00:00:00Z")
            + as_round(practice_rows({7}), 55, "2026-10-17T21:00:00Z")
            + as_round(practice_rows({8}), 56, "2026-10-17T21:00:00Z"))
    snap = source.parse_questions(to_csv(rows))

    def api(path):
        if "/actions/runs" in path:
            return {"workflow_runs": [{"name": "poll", "event": "schedule", "html_url": "https://x/run/1",
                                       "updated_at": "2026-10-18T01:23:00Z"}]}
        if "activity" in path:
            return []
        return {"private": False, "full_name": config.EXPECTED_REPO, "default_branch": "main"}
    return audit.run_audit(Repo(w), snap, config.EXPECTED_REPO, now=NOW, keys=practice_keys, api=api)


@req("N01")
def test_n01_every_round_has_exactly_one_defined_state(audited):
    assert set(audited["rounds"]) == set(config.ALL_ROUNDS)
    assert all(i["state"] in audit.STATES for i in audited["rounds"].values())
    assert sum(audited["counts"].values()) == 168
    expected = {1: "submitted_verified", 49: "submitted_verified", 50: "observed_incomplete",
                51: "ready_for_processing", 52: "solving_failed", 53: "validation_failed", 54: "push_failed",
                55: "requires_review", 56: "pushed_not_verified", 57: "not_yet_expected",
                3: "expected_not_observed", 168: "not_yet_expected"}
    assert {r: audited["rounds"][r]["state"] for r in expected} == expected


@req("N02")
def test_n02_unreleased_never_missing(audited):
    for rid in range(57, 169):
        assert audited["rounds"][rid]["state"] == "not_yet_expected"
    assert not any(r >= 57 for r in audited["needs_attention"])


@req("N03", "V20", "V06", "V07")
def test_n03_v20_naming_padding_and_content_checks(audited):
    assert set(audited["unexpected_answer_files"]) == {"answers/round-57.json", "answers/round-0170.json", "answers/notes.txt"}
    assert audited["rounds"][53]["state"] == "validation_failed" and audited["rounds"][53]["file_errors"]
    assert any("unexpected files" in p for p in audited["problems"])


@req("N04")
def test_n04_repo_identity_ok_when_correct(audited):
    assert audited["repo_public"] and audited["default_branch"] == "main" and audited["full_name"] == config.EXPECTED_REPO
    assert not any("public" in p or "registered" in p or "default branch" in p for p in audited["problems"])


@req("N05")
def test_n05_secrets_and_stray_files_detected(audited):
    assert audited["secret_scan_hits"] == ["config/leak.env"]
    assert any("possible secrets" in p for p in audited["problems"])


@req("N06")
def test_n06_practice_accuracy_from_real_keys(audited, practice_keys):
    pa = audited["practice_accuracy_on_remote"]
    expected_round2 = sum(practice_keys[(2, q)] == "A" for q in range(1, 26))   # independently counted
    assert pa == {"correct": 25 + expected_round2, "total": 50}


@req("N06")
def test_n06_keys_unavailable_is_not_evaluated(remote_and_clone):
    _, clone = remote_and_clone
    rep = audit.run_audit(Repo(clone()), None, None, now=NOW, keys=None)
    assert rep["practice_accuracy_on_remote"] == "keys unavailable: NOT EVALUATED"


@req("N07")
def test_n07_latest_cloud_run_recorded(audited):
    assert audited["latest_successful_workflow_run"]["html_url"] == "https://x/run/1"
    assert audited["latest_successful_workflow_run"]["event"] == "schedule"


@req("N08")
def test_n08_problems_listed_explicitly(audited):
    assert audited["needs_attention"] == {50: "observed_incomplete", 51: "ready_for_processing",
                                          52: "solving_failed", 53: "validation_failed", 54: "push_failed",
                                          55: "requires_review", 56: "pushed_not_verified"}
    text = audit.summarize(audited)
    assert "Needs attention" in text and "PROBLEMS" in text and "Usage report" in text
