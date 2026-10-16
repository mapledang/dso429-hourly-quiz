"""Reconcile every released round against remote main, solve what is missing, push, verify."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import answers, config, state
from .gitops import GitError, RebaseConflict, Repo
from .solver import CONFIDENT, QuestionResult, solve_round
from .solver.llm import LLMSolver
from .source import Round, Snapshot, SourceError, fetch_questions

log = logging.getLogger(__name__)

EXIT_OK, EXIT_FAILURE, EXIT_NEEDS_REVIEW = 0, 1, 3
MODES = ("production", "dry-run", "practice")
INTERMEDIATE_PUSH_AFTER = 90  # seconds; don't hold a finished round behind slow LLM work


@dataclass
class RunReport:
    mode: str
    started_at: str
    source: str = ""
    rounds_in_source: list[int] = field(default_factory=list)
    already_submitted: list[int] = field(default_factory=list)
    solved: list[int] = field(default_factory=list)
    revised: list[int] = field(default_factory=list)
    pushed: list[int] = field(default_factory=list)
    verified: list[int] = field(default_factory=list)
    incomplete: dict[int, str] = field(default_factory=dict)
    needs_review: dict[int, list[int]] = field(default_factory=dict)
    source_lag: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    round_errors: dict[int, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    llm_calls: int = 0

    @property
    def exit_code(self) -> int:
        if self.errors:
            return EXIT_FAILURE
        if self.needs_review or any(r in config.GRADED_ROUNDS for r in self.incomplete):
            return EXIT_NEEDS_REVIEW
        return EXIT_OK

    def summary_markdown(self) -> str:
        def fmt(xs):
            return ", ".join(map(str, xs)) or "none"
        status = {EXIT_OK: "OK", EXIT_FAILURE: "FAILED", EXIT_NEEDS_REVIEW: "NEEDS REVIEW"}[self.exit_code]
        lines = [f"## Quiz pipeline run: {status}", "",
                 f"- Mode: `{self.mode}`  started {self.started_at}",
                 f"- Source: {self.source}",
                 f"- Rounds in source: {len(self.rounds_in_source)} (max {max(self.rounds_in_source, default=0)})",
                 f"- Already submitted & valid on main: {len(self.already_submitted)}",
                 f"- Solved this run: {fmt(self.solved)}",
                 f"- Revised (within 3h window): {fmt(self.revised)}",
                 f"- Pushed: {fmt(self.pushed)}",
                 f"- Verified on remote main: {fmt(self.verified)}",
                 f"- LLM calls: {self.llm_calls}"]
        if self.incomplete:
            lines.append(f"- Incomplete in source (retrying): {self.incomplete}")
        if self.needs_review:
            lines.append(f"- **Needs review** (round: questions answered by guess/unverified LLM): {self.needs_review}")
        if self.source_lag:
            lines.append(f"- Scheduled but not yet in source (>30 min late): {fmt(self.source_lag)}")
        for w in self.warnings:
            lines.append(f"- WARNING: {w}")
        for e in self.errors:
            lines.append(f"- **ERROR**: {e}")
        return "\n".join(lines) + "\n"


def _iso(t: dt.datetime) -> str:
    return t.astimezone(config.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


FLAGGED_STATUSES = {"guess", "llm_unverified", "llm_expression_override"}


def _rank(status: str) -> int:
    """Reliability order used when deciding whether a re-solve may replace a submitted answer."""
    if status in CONFIDENT:
        return 2
    if status in ("llm_unverified", "llm_expression_override"):
        return 1
    return 0


def _question_hash(q: dict) -> str:
    return hashlib.sha256(json.dumps(q, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def _round_log(rnd: Round, results: list[QuestionResult], now: dt.datetime) -> bytes:
    return (json.dumps({"round_id": rnd.round_id, "released_at": _iso(rnd.released_at) if rnd.released_at else None,
                        "source_hash": rnd.content_hash(), "solved_at": _iso(now),
                        "question_hashes": {str(i): _question_hash(q) for i, q in rnd.questions.items()},
                        "questions": [r.to_dict() for r in results]}, indent=2) + "\n").encode()


def _solve_with_gaps(rnd: Round, llm) -> list[QuestionResult]:
    """Solve available questions; missing indices (only used past the grace period) get flagged guesses."""
    results = {r.q_index: r for r in solve_round(rnd.question_list(), llm)}
    for i in rnd.missing_indices:
        results[i] = QuestionResult(i, "A", "guess", "missing_in_source", "question absent from source", "")
    return [results[i] for i in sorted(results)]


class Pipeline:
    def __init__(self, repo_dir: Path, mode: str, *, repo: Repo | None = None, llm: LLMSolver | None = None,
                 loader=fetch_questions, only_rounds: set[int] | None = None, allow_revise: bool = True,
                 out_dir: Path | None = None, clock=time.monotonic):
        if mode not in MODES:
            raise ValueError(mode)
        self.repo_dir, self.mode, self.repo, self.llm = Path(repo_dir), mode, repo, llm
        self.loader, self.only_rounds, self.allow_revise = loader, only_rounds, allow_revise
        self.out_dir = Path(out_dir) if out_dir else None
        self.clock = clock

    # -- helpers -----------------------------------------------------------------
    def _existing(self, round_id: int) -> bytes | None:
        return self._read(config.answer_path(round_id))

    def _revision(self, rnd: Round, old_raw: bytes, entry: dict, now: dt.datetime) -> list[QuestionResult] | None:
        """Return results to re-submit, or None to leave the submitted file alone.

        Only revises files this pipeline wrote (hash recorded in the manifest), only inside the
        free-revision window, and only (a) questions whose source text changed, which always take
        the fresh answer, or (b) previously flagged questions, which are replaced only by a
        strictly more reliable answer. Nothing else is re-solved."""
        if not self.allow_revise or rnd.released_at is None or not rnd.complete:
            return None
        if config.is_graded(rnd.round_id) and not config.can_revise(rnd.released_at, now):
            return None
        if entry.get("answers_sha256") != hashlib.sha256(old_raw).hexdigest():
            return None  # written by hand or by something else: never overwrite
        flagged = set(entry.get("needs_review", []))
        source_changed = entry.get("source_hash") != rnd.content_hash()
        if not flagged and not source_changed:
            return None

        prev_log = self._read(state.round_log_path(rnd.round_id))
        prev = json.loads(prev_log) if prev_log else {}
        prev_status = {q["q_index"]: q["status"] for q in prev.get("questions", [])}
        prev_hashes = {int(k): v for k, v in prev.get("question_hashes", {}).items()}
        text_changed = set()
        if source_changed:
            text_changed = {i for i, q in rnd.questions.items()
                            if prev_hashes.get(i) != _question_hash(q)}  # no stored hash -> assume changed
        targets = sorted(text_changed | flagged)
        if not targets:
            return None

        old = answers.letters_of(old_raw)
        fresh = {r.q_index: r for r in solve_round([rnd.questions[i] for i in targets], self.llm)}
        merged, changed = [], False
        for i in range(1, config.QUESTIONS_PER_ROUND + 1):
            r, was = fresh.get(i), prev_status.get(i, "guess" if i in flagged else "kept")
            if r is not None and (i in text_changed or _rank(r.status) > _rank(was)):
                merged.append(r)
                # A changed question always records its new hash/log (even when the letter is the
                # same), otherwise every later run would re-solve it again.
                changed = changed or i in text_changed or r.letter != old[i] or _rank(r.status) != _rank(was)
            else:
                keep = was if was in FLAGGED_STATUSES else "kept"
                merged.append(QuestionResult(i, old[i], keep, "kept", "kept previously submitted answer", ""))
        return merged if changed else None

    def _read(self, rel: str) -> bytes | None:
        if self.mode == "production":
            return self.repo.remote_blob(rel)
        p = self.repo_dir / rel
        return p.read_bytes() if p.exists() else None

    # -- main --------------------------------------------------------------------
    def run(self) -> RunReport:
        now = config.now_utc()
        report = RunReport(self.mode, _iso(now))
        try:
            snap: Snapshot = self.loader()
        except SourceError as exc:
            report.errors.append(f"source unavailable/invalid: {exc}")
            return report
        report.source = f"{snap.origin} sha256={snap.sha256[:12]}"
        report.rounds_in_source = sorted(snap.rounds)
        for w in snap.warnings:
            log.warning(w)
            report.warnings.append(f"source: {w}")
        out_of_range = sorted(r for r in snap.rounds if r not in config.ALL_ROUNDS)
        if out_of_range:
            report.warnings.append(f"source contains rounds outside 1-168 (ignored, investigate): {out_of_range}")

        for r in config.GRADED_ROUNDS:
            sched = config.scheduled_release(r)
            if r not in snap.rounds and sched + config.SOURCE_LAG_ALERT < now:
                report.source_lag.append(r)

        for restart in range(3):
            try:
                self._reconcile(snap, now, report)
                break
            except GitError as exc:
                if not isinstance(exc, RebaseConflict):
                    report.errors.append(f"GitHub unavailable or git failed: {exc}")
                    break
                log.warning("remote main moved underneath us; rebuilding from remote state (restart %d)", restart + 1)
                try:
                    self.repo.sync_to_remote()
                except GitError as sync_exc:
                    report.errors.append(f"GitHub unavailable while re-syncing: {sync_exc}")
                    break
                report.solved.clear(), report.revised.clear(), report.pushed.clear()
                report.already_submitted.clear(), report.needs_review.clear()
        else:
            report.errors.append("could not reconcile with remote main after 3 restarts")
        if self.llm is not None:
            report.llm_calls = self.llm.calls
        return report

    def _reconcile(self, snap: Snapshot, now: dt.datetime, report: RunReport) -> None:
        production = self.mode == "production"
        if production:
            self.repo.sync_to_remote()
        raw_manifest = self.repo.remote_blob(state.MANIFEST) if production else None
        manifest = state.load(raw_manifest)
        if raw_manifest and manifest.get("_corrupt"):
            report.warnings.append("state/manifest.json was corrupt; rebuilt from remote answers/ (authoritative)")
            manifest.pop("_corrupt", None)
        manifest_before = json.dumps(manifest["rounds"], sort_keys=True)
        if production:
            self._check_consistency(snap, manifest, report)

        targets = [r for r in snap.rounds.values() if r.round_id in config.ALL_ROUNDS
                   and (self.only_rounds is None or r.round_id in self.only_rounds)]
        if self.mode == "practice":
            targets = [r for r in targets if r.round_id in config.PRACTICE_ROUNDS]
        # Oldest release first: those are closest to losing timing credit.
        targets.sort(key=lambda r: (r.released_at or now, r.round_id))

        pending: dict[int, list[str]] = {}
        expected: dict[str, bytes] = {}
        last_push = self.clock()

        for rnd in targets:
            rid, path = rnd.round_id, config.answer_path(rnd.round_id)
            entry = state.round_entry(manifest, rid)
            entry["released_at"] = _iso(rnd.released_at) if rnd.released_at else None
            old_raw = self._existing(rid)
            old_valid = old_raw is not None and not answers.validate_bytes(old_raw, rid)

            if not rnd.complete:
                past_grace = (rnd.released_at is not None and config.is_graded(rid)
                              and now - rnd.released_at >= config.INCOMPLETE_ROUND_GRACE)
                detail = f"missing q{rnd.missing_indices}; problems={rnd.problems[:3]}"
                if old_valid or not past_grace:
                    report.incomplete[rid] = detail
                    entry["retries"] = entry.get("retries", 0) + 1
                    entry["status"] = "incomplete_in_source"
                    if old_valid:
                        report.already_submitted.append(rid)
                    continue
                log.error("round %d still incomplete %s after release; submitting with flagged guesses", rid,
                          now - rnd.released_at)
                report.incomplete[rid] = detail + " (submitted with guesses past grace period)"

            try:
                if old_valid:
                    results = self._revision(rnd, old_raw, entry, now)
                    if results is None:
                        report.already_submitted.append(rid)
                        entry["status"] = "submitted"
                        continue
                    report.revised.append(rid)
                else:
                    if old_raw is not None:
                        report.warnings.append(f"round {rid}: existing file invalid "
                                               f"({answers.validate_bytes(old_raw, rid)[:2]}); replacing")
                    results = _solve_with_gaps(rnd, self.llm)
                    report.solved.append(rid)
                    if rnd.released_at and config.is_graded(rid):
                        late = now - rnd.released_at
                        if late > dt.timedelta(hours=1):
                            report.warnings.append(f"round {rid}: first submission {late.total_seconds() / 60:.0f} "
                                                   f"min after release (timing credit at risk)")
                payload = answers.build(rid, {r.q_index: r.letter for r in results})
                if answers.validate(payload, rid):
                    raise ValueError(f"generated payload invalid: {answers.validate(payload, rid)}")
            except Exception as exc:  # isolate: one bad round must not block the others
                log.exception("round %d failed", rid)
                report.round_errors[rid] = f"{type(exc).__name__}: {exc}"[:300]
                report.errors.append(f"round {rid} not submitted: {type(exc).__name__}: {exc}"[:300])
                for lst in (report.solved, report.revised):
                    if rid in lst:
                        lst.remove(rid)
                entry["status"] = "solve_failed"
                continue

            flagged = [r.q_index for r in results if not r.confident and r.status != "kept"]
            if flagged:
                report.needs_review[rid] = flagged

            if not production:
                if self.out_dir:
                    answers.write_atomic(self.out_dir, rid, payload)
                    state.write(self.out_dir, state.round_log_path(rid), _round_log(rnd, results, now))
                continue

            answers.write_atomic(self.repo_dir, rid, payload)
            data = answers.serialize(payload)
            state.write(self.repo_dir, state.round_log_path(rid), _round_log(rnd, results, now))
            entry.update({"status": "submitted", "source_hash": rnd.content_hash(),
                          "answers_sha256": hashlib.sha256(data).hexdigest(),
                          "solved_at": _iso(now), "needs_review": flagged,
                          "methods": sorted({r.status for r in results})})
            if rid in report.revised:
                entry.setdefault("revisions", []).append(_iso(now))
            pending[rid] = [path, state.round_log_path(rid)]
            expected[path] = data

            if self.clock() - last_push > INTERMEDIATE_PUSH_AFTER:
                self._push(pending, expected, manifest, report, now)
                pending, expected, last_push = {}, {}, self.clock()

        if production:
            manifest["last_source_check"] = _iso(now)
            manifest["last_source_sha256"] = snap.sha256
            if pending:
                self._push(pending, expected, manifest, report, now)
            elif json.dumps(manifest["rounds"], sort_keys=True) != manifest_before:
                # Status-only change (e.g. a round became incomplete): record it durably.
                self._commit_state(manifest, "state: update round status")

    def _check_consistency(self, snap: Snapshot, manifest: dict, report: RunReport) -> None:
        remote = set(self.repo.remote_files(config.ANSWERS_DIR))
        for key, entry in manifest["rounds"].items():
            rid = int(key)
            if entry.get("status") == "submitted" and config.answer_path(rid) not in remote:
                report.warnings.append(f"round {rid}: manifest says submitted but remote file is missing; resubmitting")
            if rid not in snap.rounds and config.answer_path(rid) in remote:
                report.warnings.append(f"round {rid}: submitted earlier but no longer in source CSV "
                                       f"(source regression/stale fetch); submission preserved")

    def _push(self, pending, expected, manifest, report: RunReport, now) -> None:
        rounds = sorted(pending)
        manifest["last_submission"] = _iso(now)
        state.write(self.repo_dir, state.MANIFEST, state.dump(manifest))
        paths = [p for ps in pending.values() for p in ps] + [state.MANIFEST]
        label = f"{rounds[0]}" if len(rounds) == 1 else f"{rounds[0]}-{rounds[-1]} ({len(rounds)} rounds)"
        try:
            committed = self.repo.commit(paths, f"answers: round {label}")
            if committed:
                # Re-check what was actually committed against what was validated (catches any
                # modification between validation and commit) before anything leaves the runner.
                bad = [p for p, data in expected.items() if self.repo.committed_blob(p) != data]
                if bad:
                    raise GitError(f"committed content differs from validated content for {bad}; not pushing")
                self.repo.push()
            report.pushed.extend(rounds)
            ok = self.repo.verify_remote(expected)
        except RebaseConflict:
            raise
        except GitError as exc:
            report.errors.append(f"push/verify failed for rounds {rounds}: {exc}")
            state.add_error(manifest, _iso(now), str(exc))
            return
        verified = sorted(int(answers.round_from_filename(p)) for p, good in ok.items() if good)
        failed = sorted(int(answers.round_from_filename(p)) for p, good in ok.items() if not good)
        report.verified.extend(verified)
        if failed:
            report.errors.append(f"remote verification failed for rounds {failed}")
        stamp = _iso(config.now_utc())
        for rid in verified:
            manifest["rounds"][str(rid)]["verified_at"] = stamp
        manifest["last_remote_verification"] = stamp
        self._commit_state(manifest, f"state: verified round {label}")

    def _commit_state(self, manifest, message: str) -> None:
        state.write(self.repo_dir, state.MANIFEST, state.dump(manifest))
        try:
            if self.repo.commit([state.MANIFEST], message):
                self.repo.push()
        except RebaseConflict:
            self.repo.sync_to_remote()  # bookkeeping only; next run recomputes it
        except GitError as exc:
            log.warning("could not push manifest update: %s", exc)
