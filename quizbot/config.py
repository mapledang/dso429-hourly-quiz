"""Assignment constants and time policy (all timezone-aware)."""

from __future__ import annotations

import datetime as dt
import os
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")
UTC = dt.timezone.utc

SOURCE_REPO = "FaisalXL/dso429-hourly-quiz"
QUESTIONS_RAW_URL = f"https://raw.githubusercontent.com/{SOURCE_REPO}/main/questions.csv"
KEYS_RAW_URL = f"https://raw.githubusercontent.com/{SOURCE_REPO}/main/practice_keys.csv"
QUESTIONS_API_URL = f"https://api.github.com/repos/{SOURCE_REPO}/contents/questions.csv?ref=main"

QUESTIONS_PER_ROUND = 25
PRACTICE_ROUNDS = range(1, 49)
GRADED_ROUNDS = range(49, 169)
ALL_ROUNDS = range(1, 169)

ROUND_49_RELEASE = dt.datetime(2026, 10, 17, 14, 0, tzinfo=PACIFIC)
ROUND_168_RELEASE = dt.datetime(2026, 10, 22, 13, 0, tzinfo=PACIFIC)
ASSIGNMENT_DUE = dt.datetime(2026, 10, 22, 14, 0, tzinfo=PACIFIC)
REPO_LINK_DEADLINE = dt.datetime(2026, 10, 16, 23, 59, tzinfo=PACIFIC)

FREE_REVISION_WINDOW = dt.timedelta(hours=3)
# Do not start a revision this close to the 3-hour cutoff: GitHub's push-received time,
# not our clock, decides which version counts.
REVISION_SAFETY_MARGIN = dt.timedelta(minutes=20)
# An incomplete graded round is held back (and retried) until this long after release, then
# submitted with flagged guesses for missing questions rather than scoring zero.
INCOMPLETE_ROUND_GRACE = dt.timedelta(hours=2)
# A graded round still absent from the source this long after its scheduled release is reported.
SOURCE_LAG_ALERT = dt.timedelta(minutes=30)

# The repository registered on Brightspace. Production refuses to run anywhere else, so a
# rename/transfer/fork cannot silently submit into the wrong place.
EXPECTED_REPO = os.environ.get("QUIZ_EXPECTED_REPO", "mapledang/dso429-hourly-quiz")

ANSWERS_DIR = "answers"
STATE_DIR = "state"
BRANCH = os.environ.get("QUIZ_BRANCH", "main")


def is_graded(round_id: int) -> bool:
    return round_id in GRADED_ROUNDS


def scheduled_release(round_id: int) -> dt.datetime | None:
    """Scheduled release for graded rounds (top of each hour, DST-aware via zoneinfo)."""
    if not is_graded(round_id):
        return None
    utc = ROUND_49_RELEASE.astimezone(UTC) + dt.timedelta(hours=round_id - 49)
    return utc


def timing_credit(released_at: dt.datetime, received_at: dt.datetime) -> float:
    hours = (received_at - released_at).total_seconds() / 3600
    if hours < 3:
        return 1.0
    if hours >= 12:
        return 0.0
    return round(1.0 - 0.1 * (int(hours) - 2), 2)


def graded_version(versions: list[tuple[dt.datetime, bytes]], released_at: dt.datetime):
    """Apply the assignment's rule to (received_at, content) pushes of one round file:
    the last version received within 3 hours of release; otherwise the first one after.
    Exactly 3:00:00 is treated as outside the window (conservative reading of "within")."""
    ordered = sorted(versions, key=lambda v: v[0])
    cutoff = released_at + FREE_REVISION_WINDOW
    within = [v for v in ordered if v[0] < cutoff]
    if within:
        return within[-1]
    return next(iter(ordered), None)


def can_revise(released_at: dt.datetime, now: dt.datetime) -> bool:
    """True while a re-push would still replace the graded version (minus a safety margin)."""
    return now < released_at + FREE_REVISION_WINDOW - REVISION_SAFETY_MARGIN


def answer_path(round_id: int) -> str:
    return f"{ANSWERS_DIR}/round-{round_id:04d}.json"


def now_utc() -> dt.datetime:
    override = os.environ.get("QUIZ_NOW")  # tests / simulations only
    if override:
        return dt.datetime.fromisoformat(override.replace("Z", "+00:00")).astimezone(UTC)
    return dt.datetime.now(UTC)
