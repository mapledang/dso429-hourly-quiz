"""Fetch, parse and validate the official questions.csv (and practice keys)."""

from __future__ import annotations

import base64
import csv
import datetime as dt
import hashlib
import io
import json
import logging
import os
from dataclasses import dataclass, field

from . import config, http

log = logging.getLogger(__name__)

REQUIRED_COLUMNS = ["round_id", "q_index", "released_at", "question_text",
                    "option_a", "option_b", "option_c", "option_d"]


class SourceError(Exception):
    """The source could not be trusted. Never interpret this as 'no new rounds'."""


@dataclass
class Round:
    round_id: int
    released_at: dt.datetime | None
    questions: dict[int, dict] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return (not self.problems and self.released_at is not None
                and sorted(self.questions) == list(range(1, config.QUESTIONS_PER_ROUND + 1)))

    @property
    def missing_indices(self) -> list[int]:
        return [i for i in range(1, config.QUESTIONS_PER_ROUND + 1) if i not in self.questions]

    def content_hash(self) -> str:
        canon = json.dumps([self.questions[k] for k in sorted(self.questions)], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canon.encode()).hexdigest()[:16]

    def question_list(self) -> list[dict]:
        return [self.questions[k] for k in sorted(self.questions)]


@dataclass
class Snapshot:
    rounds: dict[int, Round]
    fetched_at: dt.datetime
    origin: str
    sha256: str
    warnings: list[str] = field(default_factory=list)


def _parse_time(s: str) -> dt.datetime:
    t = dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=config.UTC)  # assignment: released_at is UTC
    return t.astimezone(config.UTC)


def parse_questions(raw: bytes, *, origin: str = "", fetched_at: dt.datetime | None = None) -> Snapshot:
    if not raw or not raw.strip():
        raise SourceError("questions.csv is empty")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceError(f"questions.csv is not UTF-8: {exc}") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    header = [h.strip() for h in (reader.fieldnames or [])]
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise SourceError(f"questions.csv missing columns {missing}; header={header}")
    reader.fieldnames = header

    rounds: dict[int, Round] = {}
    warnings: list[str] = []
    rows = 0
    try:
        parsed = list(reader)
    except csv.Error as exc:
        raise SourceError(f"malformed CSV (truncated?): {exc}") from exc
    for line_no, row in enumerate(parsed, start=2):
        rows += 1
        if None in row or any(row.get(c) is None for c in REQUIRED_COLUMNS):
            raise SourceError(f"line {line_no}: wrong number of fields (truncated file?)")
        try:
            rid, qi = int(row["round_id"].strip()), int(row["q_index"].strip())
        except ValueError:
            raise SourceError(f"line {line_no}: non-integer round_id/q_index")
        if rid < 1:
            raise SourceError(f"line {line_no}: invalid round_id {rid}")
        rnd = rounds.setdefault(rid, Round(rid, None))
        try:
            released = _parse_time(row["released_at"])
        except ValueError:
            rnd.problems.append(f"q{qi}: bad released_at {row['released_at']!r}")
            continue
        if rnd.released_at is None:
            rnd.released_at = released
        elif rnd.released_at != released:
            rnd.problems.append(f"q{qi}: released_at {released.isoformat()} differs from {rnd.released_at.isoformat()}")
        if not 1 <= qi <= config.QUESTIONS_PER_ROUND:
            rnd.problems.append(f"q_index {qi} out of range 1-25")
            continue
        q = {"q_index": qi, **{c: row[c] for c in REQUIRED_COLUMNS[3:]}}
        if not q["question_text"].strip() or any(not q[f"option_{c}"].strip() for c in "abcd"):
            rnd.problems.append(f"q{qi}: empty question or option")
            continue
        if qi in rnd.questions:
            if rnd.questions[qi] == q:
                warnings.append(f"round {rid} q{qi}: exact duplicate row ignored")
            else:
                rnd.problems.append(f"q{qi}: conflicting duplicate rows")
            continue
        rnd.questions[qi] = q
    if rows == 0:
        raise SourceError("questions.csv has a header but no rows")
    return Snapshot(rounds, fetched_at or config.now_utc(), origin, hashlib.sha256(raw).hexdigest(), warnings)


def fetch_questions(fetch=http.get) -> Snapshot:
    """Prefer the GitHub contents API (fresher than the raw CDN), fall back to raw URL.
    Both are tried; the snapshot with more complete rounds wins."""
    snaps, errors = [], []
    token = os.environ.get("GITHUB_TOKEN")
    api_headers = {"Accept": "application/vnd.github.raw+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        api_headers["Authorization"] = f"Bearer {token}"
    for url, headers in ((config.QUESTIONS_API_URL, api_headers), (config.QUESTIONS_RAW_URL, {"Cache-Control": "no-cache"})):
        try:
            res = fetch(url, headers=headers)
            body = res.body
            if body[:1] == b"{":  # API returned JSON metadata instead of raw (e.g. proxy dropped Accept)
                meta = json.loads(body)
                body = base64.b64decode(meta.get("content", ""))
            snap = parse_questions(body, origin=url)
            log.info("fetched %s: %d bytes, %d rounds, sha256=%s", url, len(body), len(snap.rounds), snap.sha256[:12])
            snaps.append(snap)
        except (http.FetchError, SourceError, ValueError) as exc:
            errors.append(f"{url}: {exc}")
            log.warning("source fetch failed: %s", exc)
    if not snaps:
        raise SourceError("; ".join(errors))
    best = max(snaps, key=lambda s: (sum(r.complete for r in s.rounds.values()), len(s.rounds)))
    best.warnings.extend(errors)
    return best


def parse_keys(raw: bytes) -> dict[tuple[int, int], str]:
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    keys = {}
    for row in reader:
        ans = row["answer"].strip().upper()
        if ans not in ("A", "B", "C", "D"):
            raise SourceError(f"bad key {row}")
        keys[(int(row["round_id"]), int(row["q_index"]))] = ans
    return keys


def fetch_keys(fetch=http.get) -> dict[tuple[int, int], str]:
    return parse_keys(fetch(config.KEYS_RAW_URL).body)
