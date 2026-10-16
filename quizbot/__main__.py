"""Command line: python -m quizbot {run,evaluate,audit,recover}."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import audit, config, evaluate, source
from .gitops import Repo
from .pipeline import EXIT_FAILURE, Pipeline
from .solver.llm import LLMSolver

ROOT = Path(__file__).resolve().parent.parent


def parse_rounds(spec: str) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        elif part:
            out.add(int(part))
    bad = [r for r in out if r not in config.ALL_ROUNDS]
    if bad:
        raise argparse.ArgumentTypeError(f"rounds out of range 1-168: {bad}")
    return out


def emit_summary(text: str) -> None:
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as fh:
            fh.write(text + "\n")


def guard_production(args) -> None:
    """Production pushes real submissions: only from CI or with an explicit local override."""
    if os.environ.get("GITHUB_ACTIONS") != "true" and not args.allow_local_production:
        sys.exit("refusing production mode outside GitHub Actions (pass --allow-local-production to override)")
    if os.environ.get("QUIZ_NOW"):
        sys.exit("refusing production mode with a simulated clock (QUIZ_NOW is set)")


def cmd_run(args, only_rounds=None, allow_revise=True) -> int:
    if args.mode == "production":
        guard_production(args)
    llm = None if args.no_llm else LLMSolver.from_env()
    logging.info("LLM fallback %s", "enabled" if llm else "disabled (no ANTHROPIC_API_KEY or --no-llm)")
    repo = Repo(ROOT) if args.mode == "production" else None
    out = Path(args.out) if args.out else None
    pipe = Pipeline(ROOT, args.mode, repo=repo, llm=llm, only_rounds=only_rounds,
                    allow_revise=allow_revise, out_dir=out)
    report = pipe.run()
    emit_summary(report.summary_markdown())
    if args.mode == "practice" and out:
        keys = source.fetch_keys()
        wrong = []
        for rid in report.solved:
            got = json.loads((out / config.answer_path(rid)).read_text())["answers"]
            wrong += [(rid, q) for q, v in got.items() if keys.get((rid, int(q[1:]))) != v]
        emit_summary(f"Practice files checked against official keys: {len(report.solved)} rounds, "
                     f"{len(wrong)} wrong answers {wrong[:20]}")
        if wrong:
            return EXIT_FAILURE
    return report.exit_code


def cmd_evaluate(args) -> int:
    snap = source.fetch_questions()
    keys = source.fetch_keys()
    llm = LLMSolver.from_env() if args.with_llm else None
    rep = evaluate.evaluate(snap, keys, llm)
    evaluate.write_report(rep, Path(args.report))
    print(json.dumps({k: rep[k] for k in ("questions_scored", "correct", "accuracy", "passes_threshold",
                                           "rounds_scored", "by_status")}, indent=2))
    for cat, v in rep["by_category"].items():
        print(f"  {cat:22s} {v['correct']}/{v['total']}")
    for e in rep["errors"][:30]:
        print("  WRONG", e)
    return 0 if rep["passes_threshold"] else EXIT_FAILURE


def cmd_audit(args) -> int:
    repo = Repo(ROOT)
    try:
        snap = source.fetch_questions()
    except source.SourceError as exc:
        logging.error("source unavailable: %s", exc)
        snap = None
    slug = args.repo or os.environ.get("GITHUB_REPOSITORY")
    rep = audit.run_audit(repo, snap, slug)
    if args.course_root:
        rep["local_usage_reports"] = audit.local_usage_report_present(Path(args.course_root))
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(rep, indent=2, default=str) + "\n")
    emit_summary("```\n" + audit.summarize(rep) + "\n```")
    bad = rep["counts"]["missing"] + rep["counts"]["invalid_file"]
    return EXIT_FAILURE if bad or (slug and not rep.get("repo_public")) else 0


def cmd_watchdog(args) -> int:
    """Fail if the poller has not succeeded recently (GitHub can delay or drop cron events)."""
    import datetime as dt
    slug = args.repo or os.environ.get("GITHUB_REPOSITORY")
    data = audit._api(f"/repos/{slug}/actions/workflows/{args.workflow}/runs?status=success&per_page=1")
    if data is None:
        emit_summary("Watchdog: GitHub API unavailable; cannot judge poller freshness.")
        return 0
    runs = data.get("workflow_runs") or []
    if not runs:
        emit_summary(f"**Watchdog: no successful `{args.workflow}` run yet.**")
        return EXIT_FAILURE if config.now_utc() >= config.ROUND_49_RELEASE else 0
    last = dt.datetime.fromisoformat(runs[0]["updated_at"].replace("Z", "+00:00"))
    age = config.now_utc() - last
    emit_summary(f"Watchdog: last successful `{args.workflow}` run {runs[0]['html_url']} at {last.isoformat()} "
                 f"({age.total_seconds() / 60:.0f} min ago)")
    return EXIT_FAILURE if age > dt.timedelta(minutes=args.max_age_minutes) else 0


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="quizbot")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="reconcile all released rounds")
    r.add_argument("--mode", choices=["production", "dry-run", "practice"], default="dry-run")
    r.add_argument("--out", help="dry-run/practice: directory to write answer files into")
    r.add_argument("--no-llm", action="store_true")
    r.add_argument("--allow-local-production", action="store_true")

    rc = sub.add_parser("recover", help="production re-run limited to specific rounds, e.g. 49-52,60")
    rc.add_argument("--rounds", required=True, type=parse_rounds)
    rc.add_argument("--no-revise", action="store_true",
                    help="only fill missing/invalid files; never revise valid ones")
    rc.add_argument("--no-llm", action="store_true")
    rc.add_argument("--allow-local-production", action="store_true")

    e = sub.add_parser("evaluate", help="score the solver on practice rounds 1-48")
    e.add_argument("--report", default="reports/practice_eval.json")
    e.add_argument("--with-llm", action="store_true")

    a = sub.add_parser("audit", help="final audit of rounds 1-168 on remote main")
    a.add_argument("--repo", help="owner/name (default: $GITHUB_REPOSITORY)")
    a.add_argument("--report", default="reports/audit.json")
    a.add_argument("--course-root", help="local course folder, to check for the usage report")

    w = sub.add_parser("watchdog", help="fail if the poller workflow has not succeeded recently")
    w.add_argument("--repo")
    w.add_argument("--workflow", default="poll.yml")
    w.add_argument("--max-age-minutes", type=int, default=45)

    args = p.parse_args(argv)
    if args.cmd == "watchdog":
        return cmd_watchdog(args)
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "recover":
        args.mode, args.out = "production", None
        return cmd_run(args, only_rounds=args.rounds, allow_revise=not args.no_revise)
    if args.cmd == "evaluate":
        return cmd_evaluate(args)
    return cmd_audit(args)


if __name__ == "__main__":
    sys.exit(main())
