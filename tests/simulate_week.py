"""Full graded-week rehearsal: rounds 49-168 against a local bare git remote with a simulated clock.

Chaos injected: dropped and delayed scheduled runs, a multi-hour Actions outage, source publication
lag, rounds that first appear incomplete, a round published hours late, and random push failures.
Graded rounds reuse practice questions (round r <- practice ((r-49) % 48) + 1) so the true answers
are known. The final grade is computed with the assignment's exact rules from recorded push times.

    python -m tests.simulate_week [--hours 120] [--seed 7]
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

from quizbot import answers, config, source
from quizbot.gitops import GitError, Repo
from quizbot.pipeline import Pipeline
from tests.conftest import FIX, as_round, practice_rows, to_csv

UTC = dt.timezone.utc


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True).stdout.decode()


def setup(tmp: Path) -> Path:
    bare = tmp / "remote.git"
    git(tmp, "init", "--quiet", "--bare", "-b", "main", str(bare))
    work = tmp / "work"
    git(tmp, "clone", "--quiet", str(bare), str(work))
    git(work, "config", "user.email", "bot@t")
    git(work, "config", "user.name", "bot")
    (work / "README.md").write_text("x\n")
    git(work, "add", "README.md")
    git(work, "commit", "--quiet", "-m", "init")
    git(work, "push", "--quiet", "origin", "HEAD:main")
    return work


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=120)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)
    rng = random.Random(args.seed)
    keys = source.parse_keys((FIX / "practice_keys.csv").read_bytes())
    last_round = 48 + args.hours

    # --- the instructor's (simulated) publication behaviour ---------------------------------
    release, visible, complete = {}, {}, {}
    for r in range(49, last_round + 1):
        sched = config.scheduled_release(r)
        late = dt.timedelta(hours=2) if r == 60 else dt.timedelta(0)   # one round published 2h late
        release[r] = sched + late                                       # released_at = actual release
        visible[r] = release[r] + dt.timedelta(minutes=rng.uniform(0, 6))  # CSV lag up to ~5 min
        complete[r] = visible[r] + (dt.timedelta(minutes=35) if rng.random() < 0.05 else dt.timedelta(0))
    practice = practice_rows()

    def rows_at(now):
        out = list(practice)
        for r in range(49, last_round + 1):
            if now < visible[r]:
                continue
            src = [x for x in practice if int(x["round_id"]) == (r - 49) % 48 + 1]
            rows = as_round(src, r, release[r].isoformat().replace("+00:00", "Z"))
            if now < complete[r]:
                rows = [x for x in rows if x["q_index"] != "25"]   # published incomplete at first
            out += rows
        return out

    # --- GitHub Actions behaviour --------------------------------------------------------------
    start = config.scheduled_release(49) - dt.timedelta(minutes=30)
    end = config.scheduled_release(last_round) + dt.timedelta(hours=4)
    outage = (config.scheduled_release(49) + dt.timedelta(hours=37),
              config.scheduled_release(49) + dt.timedelta(hours=39))   # 2h: no runs at all
    runs = []
    t = start
    while t <= end:
        for cron_minute in (3, 13, 23, 33, 43, 53):
            fire = t.replace(minute=cron_minute)
            if rng.random() < 0.25:            # GitHub drops ~25% of scheduled events
                continue
            fire += dt.timedelta(minutes=rng.uniform(0, 25))   # and delays the rest up to 25 min
            if not (outage[0] <= fire < outage[1]):
                runs.append(fire)
        t += dt.timedelta(hours=1)
    runs.sort()

    with tempfile.TemporaryDirectory() as tmp:
        work = setup(Path(tmp))
        pushes: list[tuple[dt.datetime, str]] = []
        real_push = Repo.push
        sim_now = {"t": start}

        def flaky_push(self, attempts=5):
            if rng.random() < 0.10:
                raise GitError("simulated: HTTP 500 from GitHub")
            real_push(self, attempts)
            sha = self.git("rev-parse", "HEAD").stdout.decode().strip()
            pushes.append((sim_now["t"], sha))

        Repo.push = flaky_push
        failures = 0
        try:
            for n, when in enumerate(runs):
                sim_now["t"] = when
                os.environ["QUIZ_NOW"] = when.isoformat()
                raw = to_csv(rows_at(when))
                rep = Pipeline(work, "production", repo=Repo(work, sleep=lambda s: None),
                               loader=lambda raw=raw: source.parse_questions(raw, origin="sim")).run()
                failures += bool(rep.errors)
                if n % 100 == 0:
                    print(f"  run {n}/{len(runs)} at {when:%a %H:%M}Z  submitted so far: "
                          f"{len(Repo(work).remote_files('answers'))}", flush=True)
        finally:
            Repo.push = real_push
            os.environ.pop("QUIZ_NOW", None)

        # --- grade exactly as the assignment describes ---------------------------------------
        repo = Repo(work)
        scores, details = {}, {}
        for r in range(49, last_round + 1):
            path = config.answer_path(r)
            versions = []
            for when, sha in pushes:
                proc = repo.git("show", f"{sha}:{path}", check=False)
                if proc.returncode == 0 and not answers.validate_bytes(proc.stdout, r):
                    if not versions or versions[-1][1] != proc.stdout:
                        versions.append((when, proc.stdout))
            cutoff = release[r] + dt.timedelta(hours=3)
            within = [v for v in versions if v[0] <= cutoff]
            chosen = within[-1] if within else next((v for v in versions if v[0] > cutoff), None)
            if chosen is None:
                scores[r], details[r] = 0.0, "missing"
                continue
            letters = answers.letters_of(chosen[1])
            pr = (r - 49) % 48 + 1
            frac = sum(letters[q] == keys[(pr, q)] for q in range(1, 26)) / 25
            credit = config.timing_credit(release[r], chosen[0])
            scores[r] = frac * credit
            details[r] = f"{frac:.2f} x {credit:.1f} ({(chosen[0] - release[r]).total_seconds() / 60:.0f} min)"

    vals = sorted(scores.values())
    dropped = vals[6:] if len(vals) > 6 else vals
    grade = sum(dropped) / len(dropped)
    worst = sorted(scores.items(), key=lambda kv: kv[1])[:5]
    lat = [float(details[r].split("(")[1].split()[0]) for r in scores if "min" in details[r]]
    print(f"\nScheduled runs simulated: {len(runs)} (dropped/delayed/2h outage), runs with errors: {failures}")
    print(f"Pushes recorded: {len(pushes)}; graded rounds: {len(scores)}")
    print(f"Rounds with perfect score: {sum(v == 1.0 for v in vals)}/{len(vals)}")
    print(f"Submission latency after release (min): median {sorted(lat)[len(lat) // 2]:.0f}, max {max(lat):.0f}")
    print(f"Worst rounds: {[(r, details[r]) for r, _ in worst]}")
    print(f"FINAL GRADE (lowest 6 dropped): {grade:.4%}")
    return 0 if grade == 1.0 else 1


if __name__ == "__main__":
    sys.exit(main())
