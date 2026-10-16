"""Final assignment audit for rounds 1-168, read entirely from remote/public state."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path

from . import answers, config, http, state
from .gitops import Repo
from .source import Snapshot

log = logging.getLogger(__name__)

STATES = ("submitted", "invalid_file", "missing", "incomplete_in_source", "not_yet_observed", "unreleased")


def _api(path: str) -> dict | list | None:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    try:
        return json.loads(http.get(f"https://api.github.com{path}", headers=headers, attempts=3).body)
    except (http.FetchError, json.JSONDecodeError) as exc:
        log.warning("GitHub API %s failed: %s", path, exc)
        return None


def push_times(repo: Repo, slug: str) -> list[tuple[dt.datetime, str]]:
    """(received_at, after_sha) for pushes to main, from GitHub's repository activity API."""
    out, page = [], 1
    while page <= 10:
        data = _api(f"/repos/{slug}/activity?ref=refs/heads/{config.BRANCH}&per_page=100&page={page}")
        if not data:
            break
        for item in data:
            if item.get("activity_type") in ("push", "force_push", "merge_queue_merge", "pr_merge"):
                out.append((dt.datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00")), item["after"]))
        if len(data) < 100:
            break
        page += 1
    return sorted(out)


def first_received(repo: Repo, path: str, pushes: list[tuple[dt.datetime, str]]) -> dt.datetime | None:
    proc = repo.git("log", "--diff-filter=A", "--format=%H", repo.remote_ref, "--", path, check=False)
    shas = proc.stdout.decode().split()
    if not shas:
        return None
    added = shas[-1]
    for when, after in pushes:
        if repo.git("merge-base", "--is-ancestor", added, after, check=False).returncode == 0:
            return when
    return None


def run_audit(repo: Repo, snap: Snapshot | None, slug: str | None, now: dt.datetime | None = None) -> dict:
    now = now or config.now_utc()
    repo.fetch()
    report: dict = {"generated_at": now.isoformat(timespec="seconds"), "repository": slug, "rounds": {}}

    if slug:
        meta = _api(f"/repos/{slug}")
        report["repo_reachable"] = bool(meta)
        report["repo_public"] = (meta or {}).get("private") is False
        report["default_branch"] = (meta or {}).get("default_branch")
        runs = _api(f"/repos/{slug}/actions/runs?status=success&per_page=1") or {}
        latest = (runs.get("workflow_runs") or [{}])[0]
        report["latest_successful_workflow_run"] = {k: latest.get(k) for k in ("name", "html_url", "updated_at")} if latest else None
    pushes = push_times(repo, slug) if slug else []

    manifest = state.load(repo.remote_blob(state.MANIFEST))
    report["manifest_errors"] = manifest.get("errors", [])[-10:]
    report["last_source_check"] = manifest.get("last_source_check")
    counts = {s: 0 for s in STATES}
    graded_scores = []
    for rid in config.ALL_ROUNDS:
        path = config.answer_path(rid)
        raw = repo.remote_blob(path)
        rnd = snap.rounds.get(rid) if snap else None
        sched = config.scheduled_release(rid)
        info = {"kind": "graded" if config.is_graded(rid) else "practice"}
        if raw is not None:
            errs = answers.validate_bytes(raw, rid)
            info["state"] = "invalid_file" if errs else "submitted"
            if errs:
                info["file_errors"] = errs
        elif rnd is not None:
            info["state"] = "missing" if rnd.complete else "incomplete_in_source"
        elif sched is not None and sched > now:
            info["state"] = "unreleased"
        else:
            info["state"] = "not_yet_observed"
        entry = manifest["rounds"].get(str(rid), {})
        if entry.get("needs_review"):
            info["needs_review"] = entry["needs_review"]
        if entry.get("verified_at"):
            info["verified_at"] = entry["verified_at"]
        released = rnd.released_at if rnd else None
        if raw is not None and pushes and released and info["state"] == "submitted":
            received = first_received(repo, path, pushes)
            if received:
                info["first_push_received"] = received.isoformat(timespec="seconds")
                info["hours_after_release"] = round((received - released).total_seconds() / 3600, 2)
                info["timing_credit"] = config.timing_credit(released, received)
                if config.is_graded(rid):
                    graded_scores.append(info["timing_credit"])
        counts[info["state"]] += 1
        report["rounds"][rid] = info
    report["counts"] = counts
    report["graded_rounds_with_full_timing_credit"] = sum(1 for s in graded_scores if s == 1.0)
    report["usage_report_due"] = now >= config.ASSIGNMENT_DUE
    report["usage_report_note"] = (
        "Run `python3 .claude/weekly_report.py --days-back 7` from the course root after "
        "2:00 PM PT Oct 22 and upload usage_reports/<date>.json to Brightspace 'Homework 3'.")
    return report


def local_usage_report_present(course_root: Path) -> list[str]:
    d = course_root / "usage_reports"
    return sorted(p.name for p in d.glob("2026-10-2*.json")) if d.exists() else []


def summarize(report: dict) -> str:
    c = report["counts"]
    bad = {rid: i for rid, i in report["rounds"].items() if i["state"] in ("missing", "invalid_file", "incomplete_in_source")}
    late = {rid: i.get("timing_credit") for rid, i in report["rounds"].items()
            if i["kind"] == "graded" and i.get("timing_credit") is not None and i["timing_credit"] < 1}
    review = {rid: i["needs_review"] for rid, i in report["rounds"].items() if i.get("needs_review")}
    lines = [f"Repository: {report.get('repository')}  reachable={report.get('repo_reachable')} "
             f"public={report.get('repo_public')} default_branch={report.get('default_branch')}",
             f"Counts: {c}",
             f"Graded rounds with full timing credit: {report['graded_rounds_with_full_timing_credit']}",
             f"Last source check: {report.get('last_source_check')}",
             f"Latest successful workflow run: {report.get('latest_successful_workflow_run')}",
             f"Missing/invalid/incomplete: {bad or 'none'}",
             f"Graded rounds received after 3h: {late or 'none'}",
             f"Rounds with flagged answers: {review or 'none'}",
             f"Unresolved manifest errors (last 10): {len(report.get('manifest_errors', []))}",
             f"Usage report due now: {report['usage_report_due']}. {report['usage_report_note']}"]
    return "\n".join(lines)
