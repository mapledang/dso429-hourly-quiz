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
    problems: list[str] = field(default_factory=list)   # blocking: round not ready
    notes: list[str] = field(default_factory=list)      # non-blocking data-quality warnings
    quarantined: set[int] = field(default_factory=set)  # q_index with conflicting duplicate rows

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
    """Parse and validate questions.csv.

    File-level problems (empty, not UTF-8, malformed/truncated CSV, missing or duplicated
    required columns, HTML instead of CSV) reject the whole snapshot. Row-level problems are
    isolated: a row without a usable round_id is dropped with a warning; any other bad row only
    makes its own round not-ready, so one typo never blocks the other rounds."""
    if not raw or not raw.strip():
        raise SourceError("questions.csv is empty")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceError(f"questions.csv is not UTF-8: {exc}") from exc
    if text.lstrip()[:1] == "<":
        raise SourceError("questions.csv looks like HTML/XML, not CSV (error page?)")
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        all_rows = list(reader)
    except csv.Error as exc:
        raise SourceError(f"malformed CSV (truncated?): {exc}") from exc
    if not all_rows:
        raise SourceError("questions.csv is empty")
    header = [h.strip() for h in all_rows[0]]
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise SourceError(f"questions.csv missing columns {missing}; header={header[:12]}")
    dupes = sorted({h for h in header if header.count(h) > 1})
    if dupes:
        raise SourceError(f"questions.csv has duplicate/ambiguous columns {dupes}")
    col = {c: header.index(c) for c in REQUIRED_COLUMNS}

    rounds: dict[int, Round] = {}
    warnings: list[str] = []
    body = all_rows[1:]
    if not body:
        raise SourceError("questions.csv has a header but no rows")
    for line_no, cells in enumerate(body, start=2):
        if not any(c.strip() for c in cells):
            continue  # blank line
        if len(cells) != len(header):
            raise SourceError(f"line {line_no}: {len(cells)} fields, expected {len(header)} (truncated file?)")
        row = {c: cells[i] for c, i in col.items()}
        try:
            rid = int(row["round_id"].strip())
            if rid < 1:
                raise ValueError
        except ValueError:
            warnings.append(f"line {line_no}: invalid round_id {row['round_id']!r}; row rejected")
            continue
        rnd = rounds.setdefault(rid, Round(rid, None))
        try:
            qi = int(row["q_index"].strip())
        except ValueError:
            rnd.problems.append(f"line {line_no}: invalid q_index {row['q_index']!r}")
            continue
        try:
            released = _parse_time(row["released_at"])
        except ValueError:
            rnd.problems.append(f"q{qi}: bad released_at {row['released_at']!r}")
            continue
        if rnd.released_at is None:
            rnd.released_at = released
        elif rnd.released_at != released:
            # Conflicting timestamps: keep the EARLIEST (shortest, i.e. safest, revision window) and flag.
            note = f"q{qi}: released_at {released.isoformat()} conflicts with {rnd.released_at.isoformat()}"
            rnd.notes.append(note)
            warnings.append(f"round {rid} {note}")
            rnd.released_at = min(rnd.released_at, released)
        if not 1 <= qi <= config.QUESTIONS_PER_ROUND:
            rnd.problems.append(f"q_index {qi} out of range 1-25")
            continue
        q = {"q_index": qi, **{c: row[c] for c in REQUIRED_COLUMNS[3:]}}
        if not q["question_text"].strip() or any(not q[f"option_{c}"].strip() for c in "abcd"):
            rnd.problems.append(f"q{qi}: empty question or option")
            continue
        if qi in rnd.quarantined:
            continue
        if qi in rnd.questions:
            if rnd.questions[qi] == q:
                warnings.append(f"round {rid} q{qi}: exact duplicate row ignored")
            else:  # never pick one of two different versions: quarantine the question
                del rnd.questions[qi]
                rnd.quarantined.add(qi)
                rnd.problems.append(f"q{qi}: conflicting duplicate rows (quarantined)")
            continue
        rnd.questions[qi] = q
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
    """Parse practice_keys.csv strictly: any invalid letter, id or conflicting duplicate is an error
    (an evaluator must never silently count a bad key)."""
    if not raw or not raw.strip():
        raise SourceError("practice_keys.csv is empty")
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    if not {"round_id", "q_index", "answer"} <= set(reader.fieldnames or []):
        raise SourceError(f"practice_keys.csv bad header {reader.fieldnames}")
    keys = {}
    for line_no, row in enumerate(reader, start=2):
        ans = (row["answer"] or "").strip()
        if ans not in ("A", "B", "C", "D"):
            raise SourceError(f"practice key line {line_no}: invalid answer {row['answer']!r}")
        try:
            k = (int(row["round_id"]), int(row["q_index"]))
        except (TypeError, ValueError):
            raise SourceError(f"practice key line {line_no}: invalid ids")
        if k in keys and keys[k] != ans:
            raise SourceError(f"practice key {k}: conflicting answers")
        keys[k] = ans
    return keys


def fetch_keys(fetch=http.get) -> dict[tuple[int, int], str]:
    return parse_keys(fetch(config.KEYS_RAW_URL).body)
