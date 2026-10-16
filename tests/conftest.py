import csv
import io
import os
import subprocess
from pathlib import Path

import pytest

from quizbot import source

os.environ.setdefault("QUIZ_LLM_RETRY_DELAY", "0")  # no real sleeping in tests

FIX = Path(__file__).parent / "fixtures"
HEADER = ["round_id", "q_index", "released_at", "question_text", "option_a", "option_b", "option_c", "option_d"]


@pytest.fixture(scope="session")
def practice_raw() -> bytes:
    return (FIX / "questions.csv").read_bytes()


@pytest.fixture(scope="session")
def practice_snapshot(practice_raw):
    return source.parse_questions(practice_raw, origin="fixture")


@pytest.fixture(scope="session")
def practice_keys():
    return source.parse_keys((FIX / "practice_keys.csv").read_bytes())


def practice_rows(rounds=None) -> list[dict]:
    with open(FIX / "questions.csv", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    return [r for r in rows if rounds is None or int(r["round_id"]) in rounds]


def to_csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=HEADER, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode()


def as_round(rows: list[dict], new_id: int, released_at: str) -> list[dict]:
    """Relabel practice rows as another round (e.g. a simulated graded round)."""
    return [{**r, "round_id": str(new_id), "released_at": released_at} for r in rows]


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True).stdout.decode()


@pytest.fixture
def remote_and_clone(tmp_path):
    """A bare 'GitHub' remote with an initial main commit, plus a working clone."""
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "--quiet", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "--quiet", str(bare), str(seed))
    for c in (["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        git(seed, *c)
    (seed / "README.md").write_text("x\n")
    git(seed, "add", "README.md")
    git(seed, "commit", "--quiet", "-m", "init")
    git(seed, "push", "--quiet", "origin", "HEAD:main")

    def make_clone(name="work"):
        d = tmp_path / name
        git(tmp_path, "clone", "--quiet", str(bare), str(d))
        git(d, "config", "user.email", "bot@t")
        git(d, "config", "user.name", "bot")
        return d

    return bare, make_clone


# --- requirement traceability ------------------------------------------------------------
# Tests declare the IDs they prove with @pytest.mark.req("C01", ...). At session end the outcome of
# every registered ID is written to reports/requirements.json and reports/TEST_REPORT.md.
# An ID with no executed test is NOT RUN, never PASS. Live-cloud IDs take their status only from
# tests/live_evidence.json (real GitHub run URLs), never from local tests.

import json as _json  # noqa: E402
from collections import defaultdict as _dd  # noqa: E402

_REQ_RESULTS = _dd(list)


def pytest_configure(config):
    config.addinivalue_line("markers", "req(*ids): QA requirement IDs proven by this test")


def pytest_runtest_logreport(report):
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        ids = getattr(report, "_req_ids", ())
        for rid in ids:
            _REQ_RESULTS[rid].append((report.nodeid, "skipped" if report.skipped else report.outcome))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    rep._req_ids = tuple(i for m in item.iter_markers("req") for i in m.args)


def pytest_sessionfinish(session, exitstatus):
    from tests.requirements import R
    if not _REQ_RESULTS:  # e.g. a single-file run without tagged tests: don't overwrite the report
        return
    unknown = sorted(set(_REQ_RESULTS) - set(R))
    live_path = Path(__file__).parent / "live_evidence.json"
    live = _json.loads(live_path.read_text()) if live_path.exists() else {}
    rows = {}
    for rid, meta in R.items():
        res = _REQ_RESULTS.get(rid, [])
        if meta["level"] == "live":
            ev = live.get(rid)
            status = ev["status"] if ev else "NOT RUN"
            rows[rid] = {**meta, "status": status, "evidence": ev, "tests": []}
            continue
        if not res:
            status = "NOT RUN"
        elif any(o == "failed" for _, o in res):
            status = "FAIL"
        elif all(o == "skipped" for _, o in res):
            status = "SKIPPED"
        else:
            status = "PASS"
        rows[rid] = {**meta, "status": status, "tests": [n for n, _ in res]}
    totals = _dd(int)
    for r in rows.values():
        totals[r["status"]] += 1
    out = Path(__file__).parent.parent / "reports"
    out.mkdir(exist_ok=True)
    (out / "requirements.json").write_text(_json.dumps(
        {"totals": dict(totals), "unknown_ids_used": unknown, "requirements": rows}, indent=2) + "\n")
    lines = ["# QA requirement matrix (generated by pytest; do not edit)", "",
             f"Totals: {dict(totals)}", "", "| ID | Level | Status | Requirement | Evidence |", "|---|---|---|---|---|"]
    for rid, r in rows.items():
        ev = (r.get("evidence") or {}).get("url") or (f"{len(r['tests'])} test(s)" if r["tests"] else "")
        lines.append(f"| {rid} | {r['level']} | **{r['status']}** | {r['title']} | {ev} |")
    (out / "TEST_REPORT.md").write_text("\n".join(lines) + "\n")
