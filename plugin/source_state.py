from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _git(worktree: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            ("git", "-C", str(worktree), *args),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("source_fingerprint_unavailable") from error
    if completed.returncode != 0:
        raise ValueError("source_fingerprint_unavailable")
    return completed.stdout


def source_fingerprint(worktree: Path) -> dict[str, str]:
    worktree = worktree.expanduser().resolve(strict=True)
    head = _git(worktree, "rev-parse", "HEAD").strip().decode("ascii", "strict")
    staged = _git(worktree, "diff", "--cached", "--binary")
    unstaged = _git(worktree, "diff", "--binary")
    untracked_names = _git(worktree, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
    untracked = hashlib.sha256()
    for raw_name in sorted(name for name in untracked_names if name):
        relative = raw_name.decode("utf-8", "strict")
        path = (worktree / relative).resolve(strict=True)
        try:
            path.relative_to(worktree)
        except ValueError:
            raise ValueError("source_fingerprint_path_escape") from None
        untracked.update(len(raw_name).to_bytes(4, "big"))
        untracked.update(raw_name)
        if path.is_file():
            untracked.update(path.read_bytes())
    return {
        "head": head,
        "stagedHash": _sha256(staged),
        "unstagedHash": _sha256(unstaged),
        "untrackedHash": f"sha256:{untracked.hexdigest()}",
    }


def source_fingerprint_digest(worktree: Path) -> str:
    payload = json.dumps(source_fingerprint(worktree), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256(payload)
