"""Answer-file construction, strict validation and atomic writes."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from . import config

VALID = ("A", "B", "C", "D")
QKEYS = [f"q{i}" for i in range(1, config.QUESTIONS_PER_ROUND + 1)]
FILENAME_RE = re.compile(r"^round-(\d{4})\.json$")


def build(round_id: int, letters: dict[int, str]) -> dict:
    return {"round_id": round_id, "answers": {f"q{i}": letters[i] for i in range(1, config.QUESTIONS_PER_ROUND + 1)}}


def serialize(payload: dict) -> bytes:
    return (json.dumps(payload, indent=2) + "\n").encode()


def validate(payload, expected_round: int) -> list[str]:
    errs = []
    if not isinstance(payload, dict):
        return ["top level is not an object"]
    if set(payload) != {"round_id", "answers"}:
        errs.append(f"top-level keys {sorted(payload)} != ['answers', 'round_id']")
    rid = payload.get("round_id")
    if not isinstance(rid, int) or isinstance(rid, bool) or rid != expected_round:
        errs.append(f"round_id {rid!r} != {expected_round}")
    ans = payload.get("answers")
    if not isinstance(ans, dict):
        return errs + ["answers is not an object"]
    if set(ans) != set(QKEYS):
        extra, missing = sorted(set(ans) - set(QKEYS)), sorted(set(QKEYS) - set(ans), key=lambda k: int(k[1:]))
        errs.append(f"answers keys wrong (missing={missing}, extra={extra})")
    bad = {k: v for k, v in ans.items() if v not in VALID}
    if bad:
        errs.append(f"invalid letters {bad}")
    return errs


def validate_bytes(raw: bytes | None, expected_round: int) -> list[str]:
    if raw is None:
        return ["missing"]
    dupes: list[str] = []

    def no_dupes(pairs):
        seen = {}
        for k, v in pairs:
            if k in seen:
                dupes.append(k)
            seen[k] = v
        return seen

    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=no_dupes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"invalid JSON: {exc}"]
    return ([f"duplicate keys {dupes}"] if dupes else []) + validate(payload, expected_round)


def letters_of(raw: bytes) -> dict[int, str]:
    return {int(k[1:]): v for k, v in json.loads(raw)["answers"].items()}


def round_from_filename(name: str) -> int | None:
    m = FILENAME_RE.match(Path(name).name)
    return int(m.group(1)) if m else None


def write_atomic(repo_dir: Path, round_id: int, payload: dict) -> Path:
    errs = validate(payload, round_id)
    if errs:
        raise ValueError(f"refusing to write invalid round {round_id}: {errs}")
    data = serialize(payload)
    path = repo_dir / config.answer_path(round_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    reread = validate_bytes(path.read_bytes(), round_id)
    if reread:
        raise ValueError(f"round {round_id} failed re-validation after write: {reread}")
    return path
