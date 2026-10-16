"""Git-backed manifest (state/manifest.json) plus per-round solve logs (state/rounds/).

The manifest is bookkeeping only; remote main's answers/ directory is authoritative.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import config

MANIFEST = f"{config.STATE_DIR}/manifest.json"
MAX_ERRORS = 50


def round_log_path(round_id: int) -> str:
    return f"{config.STATE_DIR}/rounds/round-{round_id:04d}.json"


def empty() -> dict:
    return {"schema": 1, "last_source_check": None, "last_source_sha256": None,
            "last_submission": None, "last_remote_verification": None,
            "rounds": {}, "errors": []}


def load(raw: bytes | None) -> dict:
    if not raw:
        return empty()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return empty()
    base = empty()
    base.update(data if isinstance(data, dict) else {})
    return base


def dump(manifest: dict) -> bytes:
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()


def round_entry(manifest: dict, round_id: int) -> dict:
    return manifest["rounds"].setdefault(str(round_id), {"retries": 0})


def add_error(manifest: dict, when: str, message: str, round_id: int | None = None) -> None:
    manifest["errors"].append({"at": when, "round": round_id, "error": message[:500]})
    manifest["errors"] = manifest["errors"][-MAX_ERRORS:]


def write(repo_dir: Path, rel: str, data: bytes) -> None:
    p = repo_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(p)
