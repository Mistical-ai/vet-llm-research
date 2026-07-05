"""
scripts/check_requirements_lock.py — direct dependency lock guard
=================================================================

WHY THIS SCRIPT EXISTS
----------------------
Research reruns should not silently install a different top-level dependency
set. This lightweight guard checks that every direct requirement listed in
requirements.in and requirements-dev.in has an exact ``==`` pin in the matching
lock file.

This is not a full transitive hash lock. It is a pragmatic baseline that is easy
to understand and can later be tightened with ``pip-compile --generate-hashes``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REQ_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)(?:\[.*\])?\s*([<>=!~].*)?$")
PIN_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)(?:\[.*\])?\s*==\s*([^#\s]+)")


def _normalize(name: str) -> str:
    """Normalize package names the way pip does for simple comparisons."""
    return name.lower().replace("_", "-")


def _direct_requirements(path: Path) -> set[str]:
    """Return non-comment direct package names from a requirements input file."""
    requirements: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-r "):
            continue
        match = REQ_RE.match(line)
        if match:
            requirements.add(_normalize(match.group(1)))
    return requirements


def _locked_pins(path: Path) -> set[str]:
    """Return package names with exact pins from a lock file."""
    pins: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-r "):
            continue
        match = PIN_RE.match(line)
        if match:
            pins.add(_normalize(match.group(1)))
    return pins


def _check_pair(input_path: Path, lock_path: Path) -> list[str]:
    expected = _direct_requirements(input_path)
    locked = _locked_pins(lock_path)
    missing = sorted(expected - locked)
    return [f"{lock_path.name} is missing exact pins for: {', '.join(missing)}"] if missing else []


def main() -> int:
    checks = [
        (REPO_ROOT / "requirements.in", REPO_ROOT / "requirements-lock.txt"),
        (REPO_ROOT / "requirements-dev.in", REPO_ROOT / "requirements-dev-lock.txt"),
    ]
    errors: list[str] = []
    for input_path, lock_path in checks:
        if not input_path.exists() or not lock_path.exists():
            errors.append(f"Missing {input_path.name} or {lock_path.name}")
            continue
        errors.extend(_check_pair(input_path, lock_path))

    if errors:
        for error in errors:
            print(f"[requirements-lock] ERROR: {error}")
        return 1
    print("[requirements-lock] OK: direct dependencies are pinned.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
