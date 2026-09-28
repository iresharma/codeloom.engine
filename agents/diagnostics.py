"""Classify get_diagnostics output and find edited files still missing a clean check."""

from __future__ import annotations

import re

from agents.profile import is_code_path

CLEAN = "clean"
ERRORS = "errors"
FAILED = "failed"

_ERROR_LINE = re.compile(r":\d+:\d+\s+Error:")
_OTHER_LINE = re.compile(r":\d+:\d+\s+(Warning|Info|Hint|Diagnostic):")


def normalize_rel_path(path: str) -> str:
    text = (path or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text


def classify_diagnostics_output(text: str) -> str:
    """Map a get_diagnostics tool string to clean / errors / failed.

    Error severity blocks finish. Warnings, Info, and Hint do not.
    Tool failures (``error:`` prefix, empty output, unknown shape) are failed.
    """
    raw = (text or "").strip()
    if not raw:
        return FAILED
    low = raw.lower()
    if low.startswith("error:"):
        return FAILED
    if _ERROR_LINE.search(raw):
        return ERRORS
    if "no diagnostics" in low and "clean" in low:
        return CLEAN
    if _OTHER_LINE.search(raw) or "clean" in low:
        return CLEAN
    return FAILED


def diagnostics_gaps(
    files_touched: list[str],
    diag_by_path: dict[str, str],
) -> list[tuple[str, str]]:
    """Return (path, reason) for edited code files that are not clean post-edit."""
    seen: set[str] = set()
    gaps: list[tuple[str, str]] = []
    for raw in files_touched:
        path = normalize_rel_path(raw)
        if not path or path in seen or not is_code_path(path):
            continue
        seen.add(path)
        status = diag_by_path.get(path)
        if status == CLEAN:
            continue
        if status == ERRORS:
            gaps.append((path, "Error diagnostics"))
        elif status == FAILED:
            gaps.append((path, "get_diagnostics failed"))
        else:
            gaps.append((path, "not checked after last edit"))
    return gaps


def format_diagnostics_gaps(gaps: list[tuple[str, str]]) -> str:
    return ", ".join(f"{path} ({reason})" for path, reason in gaps)
