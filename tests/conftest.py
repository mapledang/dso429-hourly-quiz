import csv
import io
import subprocess
from pathlib import Path

import pytest

from quizbot import source

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
