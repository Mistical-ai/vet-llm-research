"""
Tests for llm-sum/phase3_mode.py — the single PHASE3_MODE knob.

Critical guarantees:
    test mode forces dry_run=True (last guardrail vs accidental live calls).
    Unknown PHASE3_MODE values fall back to test (no surprise spend).
    CLI override beats env.
    dev limit honours PHASE3_DEV_LIMIT.
"""

from __future__ import annotations

import pytest

from phase3_mode import resolve_mode, ModeProfile, VALID_MODES


# ---------------------------------------------------------------------------
# Per-mode shape assertions — one test per mode so a failure points right at
# the broken row in the mode table.
# ---------------------------------------------------------------------------

def test_test_mode_forces_dry_run() -> None:
    """The most important assertion in the file. Test mode must short-circuit
    to mocks even if a stray DRY_RUN=false sneaks into the environment."""
    profile = resolve_mode(env={"PHASE3_MODE": "test", "DRY_RUN": "false"})
    assert profile.name == "test"
    assert profile.dry_run is True
    assert profile.paper_limit is None
    assert profile.use_batch is False
    assert profile.requires_confirm is False


def test_single_mode_caps_at_one_paper() -> None:
    profile = resolve_mode(env={"PHASE3_MODE": "single"})
    assert profile.name == "single"
    assert profile.dry_run is False
    assert profile.paper_limit == 1
    assert profile.use_batch is False
    assert profile.requires_confirm is True


def test_dev_mode_honours_phase3_dev_limit() -> None:
    profile = resolve_mode(env={"PHASE3_MODE": "dev", "PHASE3_DEV_LIMIT": "7"})
    assert profile.name == "dev"
    assert profile.paper_limit == 7
    assert profile.use_batch is False
    assert profile.requires_confirm is True


def test_dev_mode_falls_back_to_default_five() -> None:
    # No PHASE3_DEV_LIMIT in env → default 5.
    profile = resolve_mode(env={"PHASE3_MODE": "dev"})
    assert profile.paper_limit == 5


def test_dev_mode_clamps_silly_values_to_at_least_one() -> None:
    profile = resolve_mode(env={"PHASE3_MODE": "dev", "PHASE3_DEV_LIMIT": "0"})
    assert profile.paper_limit == 1
    profile = resolve_mode(env={"PHASE3_MODE": "dev", "PHASE3_DEV_LIMIT": "-3"})
    assert profile.paper_limit == 1


def test_batch_mode_sets_use_batch() -> None:
    profile = resolve_mode(env={"PHASE3_MODE": "batch"})
    assert profile.name == "batch"
    assert profile.dry_run is False
    assert profile.paper_limit is None
    assert profile.use_batch is True
    assert profile.requires_confirm is True


# ---------------------------------------------------------------------------
# Defaults and fallback safety
# ---------------------------------------------------------------------------

def test_empty_env_resolves_to_test_mode() -> None:
    """No PHASE3_MODE set anywhere → safest mode."""
    profile = resolve_mode(env={})
    assert profile.name == "test"
    assert profile.dry_run is True


def test_unknown_mode_falls_back_to_test(capsys: pytest.CaptureFixture) -> None:
    """Typos must not accidentally unlock paid runs."""
    profile = resolve_mode(env={"PHASE3_MODE": "produciton"})  # typo
    assert profile.name == "test"
    assert profile.dry_run is True
    captured = capsys.readouterr()
    assert "WARNING" in captured.out
    assert "produciton" in captured.out


def test_case_insensitive() -> None:
    profile = resolve_mode(env={"PHASE3_MODE": "DEV", "PHASE3_DEV_LIMIT": "3"})
    assert profile.name == "dev"
    assert profile.paper_limit == 3


# ---------------------------------------------------------------------------
# CLI override
# ---------------------------------------------------------------------------

def test_cli_override_beats_env() -> None:
    profile = resolve_mode("single", env={"PHASE3_MODE": "batch"})
    assert profile.name == "single"
    assert profile.use_batch is False


def test_cli_override_unknown_falls_back_to_test() -> None:
    profile = resolve_mode("nope", env={"PHASE3_MODE": "dev"})
    assert profile.name == "test"


# ---------------------------------------------------------------------------
# Frozen / immutable
# ---------------------------------------------------------------------------

def test_profile_is_frozen() -> None:
    profile = resolve_mode(env={"PHASE3_MODE": "single"})
    with pytest.raises((AttributeError, TypeError)):
        profile.name = "batch"  # type: ignore[misc]


def test_banner_includes_mode_name() -> None:
    profile = resolve_mode(env={"PHASE3_MODE": "dev", "PHASE3_DEV_LIMIT": "3"})
    banner = profile.banner()
    assert "phase3" in banner
    assert "dev" in banner
    assert "limit=3" in banner


def test_valid_modes_is_the_complete_set() -> None:
    # If a fifth mode is added, the table tests above must grow too.
    assert set(VALID_MODES) == {"test", "single", "dev", "batch"}
