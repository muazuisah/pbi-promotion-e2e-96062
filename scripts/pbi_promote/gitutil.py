"""Thin wrappers around git used to work out which files a change touched."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ZERO_SHA_RE = re.compile(r"^0{40}$")
REF_RE = re.compile(r"^[A-Za-z0-9._/@^~-]+$")


class GitError(Exception):
    pass


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _check_ref(ref: str, name: str) -> None:
    if not ref or not REF_RE.match(ref) or ref.startswith("-"):
        raise GitError(f"Invalid git ref for {name}: {ref!r}")
    if ZERO_SHA_RE.match(ref):
        raise GitError(
            f"{name} is the all-zero SHA (new branch or first push). "
            "There is nothing to compare against; run the workflow manually with an explicit unit selection."
        )


def changed_files(repo: Path, base: str, head: str, mode: str = "two-dot") -> list[str]:
    """Return repo-relative POSIX paths changed between base and head.

    two-dot compares the two commits directly (used after a merge to main).
    three-dot compares head with the merge-base (used for PRs and feature branches).
    Renames are reported as delete + add so both the old and new unit are detected.
    """
    _check_ref(base, "base")
    _check_ref(head, "head")
    if mode not in ("two-dot", "three-dot"):
        raise GitError(f"Unknown diff mode {mode!r}")
    spec = f"{base}...{head}" if mode == "three-dot" else f"{base}..{head}"
    out = _git(repo, "-c", "core.quotepath=false", "diff", "--name-only", "--no-renames", "-z", spec, "--")
    return sorted({p for p in out.split("\0") if p})


def rev_parse(repo: Path, ref: str) -> str:
    _check_ref(ref, "ref")
    return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
