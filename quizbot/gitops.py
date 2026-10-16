"""Git plumbing: read remote main, commit, push with bounded retries, verify remotely.

Never force-pushes. On a rejected push it fetches, rebases onto the remote and retries;
if the rebase conflicts it aborts and raises so the caller can rebuild from remote state.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

from . import config
from .http import backoff_delay

log = logging.getLogger(__name__)


class GitError(Exception):
    pass


class RebaseConflict(GitError):
    pass


class Repo:
    def __init__(self, path: Path, remote: str = "origin", branch: str = config.BRANCH, sleep=time.sleep):
        self.path, self.remote, self.branch, self.sleep = Path(path), remote, branch, sleep

    @property
    def remote_ref(self) -> str:
        return f"{self.remote}/{self.branch}"

    def git(self, *args: str, check: bool = True, input: bytes | None = None) -> subprocess.CompletedProcess:
        proc = subprocess.run(["git", *args], cwd=self.path, capture_output=True, input=input, timeout=120)
        if check and proc.returncode != 0:
            err = proc.stderr.decode(errors="replace").strip()
            raise GitError(f"git {args[0]} failed ({proc.returncode}): {err[-500:]}")
        return proc

    # -- remote state ------------------------------------------------------------
    def fetch(self, attempts: int = 4) -> None:
        for attempt in range(attempts):
            proc = self.git("fetch", "--quiet", self.remote, self.branch, check=False)
            if proc.returncode == 0:
                return
            if attempt < attempts - 1:
                self.sleep(backoff_delay(attempt, base=2))
        raise GitError(f"git fetch failed: {proc.stderr.decode(errors='replace')[-300:]}")

    def remote_files(self, prefix: str) -> list[str]:
        proc = self.git("ls-tree", "-r", "--name-only", self.remote_ref, "--", prefix, check=False)
        return proc.stdout.decode().split() if proc.returncode == 0 else []

    def remote_blob(self, path: str) -> bytes | None:
        proc = self.git("show", f"{self.remote_ref}:{path}", check=False)
        return proc.stdout if proc.returncode == 0 else None

    def current_branch(self) -> str:
        return self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.decode().strip()

    def sync_to_remote(self) -> None:
        """Make the local branch identical to remote main (local-only reset, never a force push)."""
        self.fetch()
        self.git("checkout", "--quiet", "-f", "-B", self.branch, self.remote_ref)

    # -- writing -----------------------------------------------------------------
    def commit(self, paths: list[str], message: str) -> bool:
        if not paths:
            return False
        self.git("add", "--", *paths)
        if self.git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return False  # nothing changed: no empty commits
        self.git("commit", "--quiet", "-m", message)
        return True

    def push(self, attempts: int = 5) -> None:
        last = ""
        for attempt in range(attempts):
            proc = self.git("push", "--quiet", self.remote, f"HEAD:refs/heads/{self.branch}", check=False)
            if proc.returncode == 0:
                return
            last = proc.stderr.decode(errors="replace").strip()
            log.warning("push attempt %d failed: %s", attempt + 1, last[-300:])
            if any(s in last for s in ("rejected", "non-fast-forward", "fetch first")):
                self.fetch()
                if self.git("rebase", "--quiet", self.remote_ref, check=False).returncode != 0:
                    self.git("rebase", "--abort", check=False)
                    raise RebaseConflict("rebase onto remote main conflicted")
            elif any(s in last for s in ("403", "Permission", "denied", "Authentication")):
                raise GitError(f"push not authorised: {last[-300:]}")
            if attempt < attempts - 1:
                self.sleep(backoff_delay(attempt, base=2))
        raise GitError(f"push failed after {attempts} attempts: {last[-300:]}")

    def verify_remote(self, expected: dict[str, bytes]) -> dict[str, bool]:
        """Re-fetch and compare each path's blob on remote main byte-for-byte."""
        self.fetch()
        return {p: self.remote_blob(p) == data for p, data in expected.items()}
