"""Final assignment audit for rounds 1-168, read entirely from remote/public state.

Every round is classified into exactly one state (see STATES). Unreleased rounds are never
reported as missing, and source-publication delays are distinguished from pipeline failures.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
from pathlib import Path

from . import answers, config, http, state
from .gitops import Repo
from .redact import _PATTERNS as SECRET_PATTERNS
from .source import Snapshot

log = logging.getLogger(__name__)

STATES = (
    "not_yet_expected",        # graded round scheduled in the future, not in source
    "expected_not_observed",   # should be out by schedule but the source has not published it
    "observed_incomplete",     # in source but not 25 valid questions; nothing submitted
    "ready_for_processing",    # complete in source, no file yet, no failure recorded
    "solving_failed",          # pipeline recorded a solve/build failure, no file
    "validation_failed",       # a file exists on remote main but is not valid
    "push_failed",             # manifest says it was submitted but the remote file is absent
    "pushed_not_verified",     # valid file, but not the content the pipeline recorded pushing
    "submitted_verified",      # valid file on remote main (re-read and validated by this audit)
    "requires_review",         # submitted & valid, but some answers were flagged guesses
)
PROBLEM_STATES = {"ready_for_processing", "solving_failed", "validation_failed", "push_failed",
                  "pushed_not_verified", "observed_incomplete"}
_ANSWER_NAME = re.compile(r"^answers/round-(\d{4})\.json$")


def _api(path: str) -> dict | list | None:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    try:
        return json.loads(http.get(f"https://api.github.com{path}", headers=headers, attempts=3).body)
    except (http.FetchError, json.JSONDecodeError) as exc:
        log.warning("GitHub API %s failed: %s", path, exc)
        return None


def push_times(slug: str, api=_api) -> list[tuple[dt.datetime, str]]:
    """(received_at, after_sha) for pushes to main, from GitHub's repository activity API
    (GitHub's own receive time, never commit dates, which can be forged)."""
    out, page = [], 1
    while page <= 10:
        data = api(f"/repos/{slug}/activity?ref=refs/heads/{config.BRANCH}&per_page=100&page={page}")
        if not data:
            break
        for item in data:
            if item.get("activity_type") in ("push", "force_push", "merge_queue_merge", "pr_merge"):
                out.append((dt.datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00")), item["after"]))
        if len(data) < 100:
            break
        page += 1
    return sorted(out)


def file_versions(repo: Repo, path: str, pushes) -> list[tuple[dt.datetime, bytes]]:
    """Each distinct content of `path` as of each push, with GitHub's receive time."""
    versions = []
    for when, sha in pushes:
        proc = repo.git("show", f"{sha}:{path}", check=False)
        if proc.returncode == 0 and (not versions or versions[-1][1] != proc.stdout):
            versions.append((when, proc.stdout))
    return versions


def scan_for_secrets(repo: Repo) -> list[str]:
    hits = []
    for path in repo.remote_files(""):
        blob = repo.remote_blob(path) or b""
        text = blob.decode("utf-8", errors="ignore")
        if any(p.search(text) for p in SECRET_PATTERNS):
            hits.append(path)
    return hits


def classify(rid: int, raw: bytes | None, rnd, entry: dict, now: dt.datetime) -> dict:
    info = {"kind": "graded" if config.is_graded(rid) else "practice"}
    sched = config.scheduled_release(rid)
    if raw is not None:
        errs = answers.validate_bytes(raw, rid)
        if errs:
            info.update(state="validation_failed", file_errors=errs)
        elif entry.get("answers_sha256") and entry["answers_sha256"] != hashlib.sha256(raw).hexdigest():
            info["state"] = "pushed_not_verified"
            info["note"] = "remote content differs from what the pipeline recorded (hand edit?)"
        elif entry.get("needs_review"):
            info.update(state="requires_review", needs_review=entry["needs_review"])
        else:
            info["state"] = "submitted_verified"
    elif entry.get("status") == "submitted":
        info["state"] = "push_failed"
    elif entry.get("status") == "solve_failed":
        info["state"] = "solving_failed"
    elif rnd is not None:
        info["state"] = "ready_for_processing" if rnd.complete else "observed_incomplete"
    elif sched is not None and sched > now:
        info["state"] = "not_yet_expected"
    else:
        info["state"] = "expected_not_observed"
    if entry.get("verified_at"):
        info["pipeline_verified_at"] = entry["verified_at"]
    return info


def run_audit(repo: Repo, snap: Snapshot | None, slug: str | None, now: dt.datetime | None = None,
              keys: dict | None = None, api=_api) -> dict:
    now = now or config.now_utc()
    repo.fetch()
    report: dict = {"generated_at": now.isoformat(timespec="seconds"), "repository": slug,
                    "expected_repository": config.EXPECTED_REPO, "rounds": {}, "problems": []}

    if slug:
        meta = api(f"/repos/{slug}")
        report["repo_reachable"] = bool(meta)
        report["repo_public"] = (meta or {}).get("private") is False
        report["full_name"] = (meta or {}).get("full_name")
        report["default_branch"] = (meta or {}).get("default_branch")
        runs = api(f"/repos/{slug}/actions/runs?status=success&per_page=1") or {}
        latest = (runs.get("workflow_runs") or [None])[0]
        report["latest_successful_workflow_run"] = (
            {k: latest.get(k) for k in ("name", "event", "html_url", "updated_at")} if latest else None)
        if not report["repo_reachable"]:
            report["problems"].append("repository not reachable")
        elif not report["repo_public"]:
            report["problems"].append("repository is NOT public: graders cannot read it")
        if report.get("full_name") and report["full_name"].lower() != config.EXPECTED_REPO.lower():
            report["problems"].append(f"repository is {report['full_name']}, registered as {config.EXPECTED_REPO}")
        if report.get("default_branch") and report["default_branch"] != config.BRANCH:
            report["problems"].append(f"default branch is {report['default_branch']}; grading reads {config.BRANCH}")
    pushes = push_times(slug, api) if slug else []

    raw_manifest = repo.remote_blob(state.MANIFEST)
    manifest = state.load(raw_manifest)
    if manifest.get("_corrupt"):
        report["problems"].append("state/manifest.json is corrupt (answers/ remains authoritative)")
    report["manifest_errors"] = manifest.get("errors", [])[-10:]
    report["last_source_check"] = manifest.get("last_source_check")

    # Files under answers/ that are not exactly answers/round-NNNN.json for 1..168.
    expected_names = {config.answer_path(r) for r in config.ALL_ROUNDS}
    unexpected = [p for p in repo.remote_files(config.ANSWERS_DIR) if p not in expected_names]
    report["unexpected_answer_files"] = unexpected
    if unexpected:
        report["problems"].append(f"unexpected files under answers/ (misnamed/wrong padding/out of range): {unexpected}")
    report["secret_scan_hits"] = scan_for_secrets(repo)
    if report["secret_scan_hits"]:
        report["problems"].append(f"possible secrets committed in {report['secret_scan_hits']}")

    counts = {s: 0 for s in STATES}
    practice_correct = practice_total = 0
    for rid in config.ALL_ROUNDS:
        path = config.answer_path(rid)
        raw = repo.remote_blob(path)
        rnd = snap.rounds.get(rid) if snap else None
        entry = manifest["rounds"].get(str(rid), {})
        info = classify(rid, raw, rnd, entry, now)
        released = rnd.released_at if rnd else None
        if raw is not None and pushes and released and info["state"] != "validation_failed":
            chosen = config.graded_version(file_versions(repo, path, pushes), released)
            if chosen:
                info["graded_version_received"] = chosen[0].isoformat(timespec="seconds")
                info["hours_after_release"] = round((chosen[0] - released).total_seconds() / 3600, 2)
                info["timing_credit"] = config.timing_credit(released, chosen[0])
        if keys and raw is not None and rid in config.PRACTICE_ROUNDS and info["state"] != "validation_failed":
            got = answers.letters_of(raw)
            practice_correct += sum(got[q] == keys.get((rid, q)) for q in got)
            practice_total += len(got)
        counts[info["state"]] += 1
        report["rounds"][rid] = info
    report["counts"] = counts
    report["practice_accuracy_on_remote"] = (
        {"correct": practice_correct, "total": practice_total} if keys else "keys unavailable: NOT EVALUATED")
    report["needs_attention"] = {rid: i["state"] for rid, i in report["rounds"].items()
                                 if i["state"] in PROBLEM_STATES or i["state"] == "requires_review"}
    report["graded_rounds_with_full_timing_credit"] = sum(
        1 for i in report["rounds"].values() if i["kind"] == "graded" and i.get("timing_credit") == 1.0)
    report["usage_report_due"] = now >= config.ASSIGNMENT_DUE
    report["usage_report_note"] = (
        "Separate requirement: after 2:00 PM PT Oct 22 run `python3 .claude/weekly_report.py --days-back 7` "
        "from the course root and upload ONLY usage_reports/<date>.json to Brightspace 'Homework 3'.")
    return report


def local_usage_report_present(course_root: Path) -> list[str]:
    d = course_root / "usage_reports"
    return sorted(p.name for p in d.glob("2026-10-2*.json")) if d.exists() else []


def summarize(report: dict) -> str:
    late = {rid: i.get("timing_credit") for rid, i in report["rounds"].items()
            if i["kind"] == "graded" and i.get("timing_credit") is not None and i["timing_credit"] < 1}
    lines = [f"Repository: {report.get('repository')}  reachable={report.get('repo_reachable')} "
             f"public={report.get('repo_public')} default_branch={report.get('default_branch')}",
             f"Counts: { {k: v for k, v in report['counts'].items() if v} }",
             f"Needs attention: {report['needs_attention'] or 'none'}",
             f"Graded rounds with full timing credit: {report['graded_rounds_with_full_timing_credit']}",
             f"Graded rounds received after 3h: {late or 'none'}",
             f"Practice accuracy of files on remote main: {report['practice_accuracy_on_remote']}",
             f"Unexpected answer files: {report['unexpected_answer_files'] or 'none'}; "
             f"secret-scan hits: {report['secret_scan_hits'] or 'none'}",
             f"Last source check: {report.get('last_source_check')}",
             f"Latest successful workflow run: {report.get('latest_successful_workflow_run')}",
             f"PROBLEMS: {report['problems'] or 'none'}",
             f"Usage report due now: {report['usage_report_due']}. {report['usage_report_note']}"]
    return "\n".join(lines)
