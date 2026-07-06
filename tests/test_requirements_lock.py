from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "check_requirements_lock",
        Path("scripts/check_requirements_lock.py"),
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_requirements_lock_checker_accepts_repo_locks() -> None:
    checker = _load_checker()

    assert checker.main() == 0


def test_requirements_lock_checker_reports_missing_pin(tmp_path: Path) -> None:
    checker = _load_checker()
    input_path = tmp_path / "requirements.in"
    lock_path = tmp_path / "requirements-lock.txt"
    input_path.write_text("requests\npydantic\n", encoding="utf-8")
    lock_path.write_text("requests==2.32.0\n", encoding="utf-8")

    errors = checker._check_pair(input_path, lock_path)

    assert errors == ["requirements-lock.txt is missing exact pins for: pydantic"]
