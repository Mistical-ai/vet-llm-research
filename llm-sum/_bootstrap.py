"""
llm-sum/_bootstrap.py — Hyphenated-folder import shim
=====================================================

The `llm-sum/` directory contains a hyphen, which is not a valid Python
identifier. That blocks the normal `from llm-sum.summarizer import ...`
import form. The conventional workaround is to add this directory (and the
sibling `src/` directory) to `sys.path` so every script in `llm-sum/` can
do flat imports like:

    from _bootstrap import *      # MUST be first line in every llm-sum script
    from summarizer import generate_summary
    from utils import log_error, BudgetGuard          # from src/
    from file_paths import doi_to_slug                # from src/
    from extract import extract_clean_text            # from src/

Why the wildcard import? It triggers this module's side effects (sys.path
mutation + load_dotenv) without exposing any names. The `__all__ = []`
guarantees no symbols leak into the caller's namespace.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

__all__: list[str] = []

_HERE = Path(__file__).resolve().parent          # .../vet-llm-research/llm-sum
_REPO_ROOT = _HERE.parent                        # .../vet-llm-research
_SRC = _REPO_ROOT / "src"

# Add llm-sum/ and src/ to the front of sys.path so flat imports work.
# We insert at index 0 so our local modules win over any same-named package
# that might be installed in the environment.
for _path in (_HERE, _SRC):
    _path_str = str(_path)
    if _path_str not in sys.path:
        sys.path.insert(0, _path_str)

# Load .env once at bootstrap time. utils.py also calls load_dotenv at its
# own import, but a Phase 3 script may import config before importing utils,
# so we duplicate the call here for safety. load_dotenv is idempotent.
load_dotenv(dotenv_path=_REPO_ROOT / ".env")

# Force UTF-8 on the console streams. Windows terminals default to a legacy
# code page (cp1252) that cannot encode the Unicode these scripts print — the
# U+2212 minus in the significance table headers, em dashes throughout the
# Markdown reports, Greek letters (alpha, chi-square) in the stats output, and
# accented author names in article titles. Without this, `print`-ing a report
# dies with UnicodeEncodeError even though the saved .md/.json/.csv files (which
# write encoding="utf-8" explicitly) are fine. Guarded because a captured stream
# (pytest) or an already-UTF-8 stream may not need — or expose — reconfigure().
for _stream in (sys.stdout, sys.stderr):
    try:
        if hasattr(_stream, "reconfigure") and (getattr(_stream, "encoding", "") or "").lower() not in ("utf-8", "utf8"):
            _stream.reconfigure(encoding="utf-8")
    except (ValueError, OSError):
        pass

# Expose repo paths as importable constants for convenience.
REPO_ROOT = _REPO_ROOT
LLM_SUM_DIR = _HERE
SRC_DIR = _SRC
DATA_DIR = _REPO_ROOT / "data"
RAW_TEXT_DIR = DATA_DIR / "raw_text"
PROCESSED_DIR = DATA_DIR / os.getenv("PROCESSED_DIR_NAME", "processed")
BATCH_DIR = DATA_DIR / "batch"
LOGS_DIR = DATA_DIR / "logs"

__all__ = [
    "REPO_ROOT",
    "LLM_SUM_DIR",
    "SRC_DIR",
    "DATA_DIR",
    "RAW_TEXT_DIR",
    "PROCESSED_DIR",
    "BATCH_DIR",
    "LOGS_DIR",
]
