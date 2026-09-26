"""The reviewer's requirements table, and the verdict the engine derives
from it.

A task required that "a write never leaves a stale cached read behind". The
coder left two write paths as TODOs. The reviewer approved, saying the TODOs
were expected "per brief item 6" -- the orchestrator's brief had already
downgraded a hard requirement to an optional one, and the reviewer checked
the paraphrase instead of the task. Separately, a soft instruction ("if easy,
sanity-check with a local run") was skipped with no explanation at all.

So: the reviewer's brief carries the original user prompt verbatim, clearly
labelled as the authority, with the orchestrator's brief labelled as an
interpretation of it. And the reviewer's verdict is not taken at its word --
it is derived here from the table it must emit. A hard requirement marked
`not met` is `request_changes` even when the reviewer's prose says approve
and even when the gap is documented with a TODO.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "REQUIREMENTS_END",
    "REQUIREMENTS_START",
    "RequirementRow",
    "ReviewVerdict",
    "build_reviewer_brief",
    "enforce_verdict",
    "parse_requirements_table",
    "stated_verdict",
]

REQUIREMENTS_START = "=== REQUIREMENTS ==="
REQUIREMENTS_END = "=== END REQUIREMENTS ==="

TASK_LABEL = "=== ORIGINAL USER TASK (verbatim — this is the requirement) ==="
TASK_END = "=== END ORIGINAL USER TASK ==="
BRIEF_LABEL = (
    "=== ORCHESTRATOR'S INTERPRETATION (a brief, not the requirement; it may "
    "have dropped or softened something) ==="
)
BRIEF_END = "=== END ORCHESTRATOR'S INTERPRETATION ==="

APPROVE = "approve"
REQUEST_CHANGES = "request_changes"
BLOCK = "block"

_HARD = "hard"
_CONDITIONAL = "conditional"
_MET = "met"
_NOT_MET = "not met"
_SKIPPED = "skipped"

# A disposition cell that says nothing. "skipped" with no reason is caught
# separately, by RequirementRow.reason.
_EMPTY_CELLS = {"", "-", "--", "n/a", "na", "tbd", "?", "todo", "unknown"}
_HEADER_CELLS = {"requirement", "kind", "status", "evidence", "disposition"}
_SEPARATOR = re.compile(r"^[\s|:-]+$")


@dataclass
class RequirementRow:
    requirement: str
    kind: str  # hard | conditional
    status: str  # met | not met | skipped
    reason: str  # the parenthesised reason on a skipped row
    evidence: str
    raw: str

    @property
    def is_hard(self) -> bool:
        return self.kind == _HARD

    @property
    def is_conditional(self) -> bool:
        return self.kind == _CONDITIONAL

    @property
    def undisposed(self) -> bool:
        """No disposition at all, or `skipped` with no reason given."""
        if self.status == "":
            return True
        return self.status == _SKIPPED and not self.reason


@dataclass
class ReviewVerdict:
    verdict: str
    reason: str
    stated: str
    rows: list[RequirementRow]
    overridden: bool = False

    @property
    def approved(self) -> bool:
        return self.verdict == APPROVE


def build_reviewer_brief(
    user_task: str, orchestrator_brief: str, verify_block: str = ""
) -> str:
    """The reviewer's task string: the original prompt verbatim first, the
    orchestrator's interpretation second, the harness verify result third."""
    parts = []
    task = (user_task or "").strip()
    if task:
        parts.append(f"{TASK_LABEL}\n{task}\n{TASK_END}")
    brief = (orchestrator_brief or "").strip()
    if brief:
        parts.append(f"{BRIEF_LABEL}\n{brief}\n{BRIEF_END}")
    block = (verify_block or "").strip()
    if block:
        parts.append(block)
    parts.append(
        "Check the change against the ORIGINAL USER TASK. Where the "
        "interpretation above says less than the task does, the task wins. "
        f"Emit a requirements table between {REQUIREMENTS_START} and "
        f"{REQUIREMENTS_END}, one row per explicit requirement and per "
        "conditional instruction in the original task:\n"
        "| requirement | kind | status | evidence |\n"
        "kind is hard or conditional. status is met, not met, or "
        "skipped (reason). evidence is file:line or the verify output. "
        "Every row needs a disposition; a conditional instruction you did not "
        "follow must say skipped and why. A documented TODO is not met, not "
        "met-with-a-note."
    )
    return "\n\n".join(parts)


def parse_requirements_table(text: str) -> list[RequirementRow]:
    """Rows between the REQUIREMENTS markers, or from a bare pipe table.

    Tolerant of the usual model formatting drift: a markdown header row, a
    `|---|` separator, missing outer pipes, and three- or four-column rows.
    """
    body = _table_body(text)
    rows: list[RequirementRow] = []
    for line in body.splitlines():
        raw = line.strip()
        if "|" not in raw or _SEPARATOR.match(raw):
            continue
        cells = [cell.strip() for cell in raw.strip("|").split("|")]
        cells = [cell for cell in cells]
        if len(cells) < 3:
            continue
        lowered = {cell.lower() for cell in cells[:4]}
        if lowered <= _HEADER_CELLS:
            continue
        requirement = cells[0]
        if not requirement:
            continue
        kind = _normalize_kind(cells[1])
        status, reason = _normalize_status(cells[2])
        evidence = cells[3] if len(cells) > 3 else ""
        if evidence.lower() in _EMPTY_CELLS:
            evidence = ""
        rows.append(
            RequirementRow(
                requirement=requirement,
                kind=kind,
                status=status,
                reason=reason,
                evidence=evidence,
                raw=raw,
            )
        )
    return rows


def _table_body(text: str) -> str:
    body = text or ""
    if REQUIREMENTS_START in body:
        body = body.split(REQUIREMENTS_START, 1)[1]
        if REQUIREMENTS_END in body:
            body = body.split(REQUIREMENTS_END, 1)[0]
    return body


def _normalize_kind(cell: str) -> str:
    value = (cell or "").strip().lower()
    if value.startswith("hard") or value in {"explicit", "required", "must"}:
        return _HARD
    if value.startswith("cond") or value in {"soft", "optional", "if easy", "nice"}:
        return _CONDITIONAL
    # An unrecognized kind is treated as hard: the safe direction is to keep
    # a requirement blocking rather than to quietly demote it.
    return _HARD


def _normalize_status(cell: str) -> tuple[str, str]:
    value = " ".join((cell or "").strip().split())
    if value.lower() in _EMPTY_CELLS:
        return "", ""
    reason = ""
    match = re.search(r"\(([^)]*)\)", value)
    if match:
        reason = match.group(1).strip()
        value = (value[: match.start()] + value[match.end() :]).strip()
    lowered = value.lower().strip(" .:;-")
    if lowered.startswith("skip"):
        return _SKIPPED, reason
    if lowered in {"not met", "unmet", "notmet", "no", "missing", "fail", "failed"}:
        return _NOT_MET, reason
    if lowered.startswith("not met") or lowered.startswith("partially"):
        return _NOT_MET, reason or value
    if lowered in {"met", "yes", "done", "ok", "pass", "passed"}:
        return _MET, reason
    if lowered.startswith("met"):
        return _MET, reason
    if not lowered:
        return ("", "") if not reason else (_SKIPPED, reason)
    # Anything else is not a disposition we can act on.
    return "", reason


_VERDICT_PATTERNS = (
    (BLOCK, re.compile(r"\b(block(?:ed|ing)?)\b", re.IGNORECASE)),
    (
        REQUEST_CHANGES,
        re.compile(
            r"\b(request[_\s-]?changes|changes[_\s-]?requested|needs?\s+changes)\b",
            re.IGNORECASE,
        ),
    ),
    (APPROVE, re.compile(r"\b(approve[ds]?|lgtm|looks good)\b", re.IGNORECASE)),
)


def stated_verdict(text: str) -> str:
    """The verdict the reviewer's own prose claims, or "" when it claims none.

    Prefers an explicit `verdict:` line; otherwise scans the whole text with
    block beating request_changes beating approve, so a reviewer that says
    both does not get read as the weaker one.
    """
    body = text or ""
    line = ""
    for candidate in body.splitlines():
        stripped = candidate.strip().lower()
        if stripped.startswith("verdict:") or stripped.startswith("verdict ="):
            line = candidate
            break
    haystack = line or body
    for name, pattern in _VERDICT_PATTERNS:
        if pattern.search(haystack):
            return name
    return ""


def enforce_verdict(text: str) -> ReviewVerdict:
    """Derive the verdict from the table, not from the reviewer's prose.

    - a hard requirement `not met` -> request_changes, TODO or not
    - a conditional instruction with no disposition -> request_changes
    - no table at all -> request_changes (nothing was checked against the
      original task, which is the failure mode this exists to catch)
    `block` from the reviewer is never downgraded.
    """
    stated = stated_verdict(text)
    rows = parse_requirements_table(text)
    if stated == BLOCK:
        return ReviewVerdict(BLOCK, "", stated, rows)
    if not rows:
        return ReviewVerdict(
            REQUEST_CHANGES,
            "no requirements table: the change was never checked against the "
            "original task, requirement by requirement",
            stated,
            rows,
            overridden=stated == APPROVE,
        )
    unmet = [row for row in rows if row.is_hard and row.status == _NOT_MET]
    if unmet:
        listed = "; ".join(row.requirement for row in unmet[:5])
        return ReviewVerdict(
            REQUEST_CHANGES,
            f"hard requirement(s) not met: {listed}",
            stated,
            rows,
            overridden=stated == APPROVE,
        )
    skipped_hard = [row for row in rows if row.is_hard and row.status == _SKIPPED]
    if skipped_hard:
        listed = "; ".join(row.requirement for row in skipped_hard[:5])
        return ReviewVerdict(
            REQUEST_CHANGES,
            f"hard requirement(s) skipped rather than met: {listed}",
            stated,
            rows,
            overridden=stated == APPROVE,
        )
    undisposed = [row for row in rows if row.undisposed]
    if undisposed:
        listed = "; ".join(row.requirement for row in undisposed[:5])
        return ReviewVerdict(
            REQUEST_CHANGES,
            f"instruction(s) with no disposition: {listed}",
            stated,
            rows,
            overridden=stated == APPROVE,
        )
    return ReviewVerdict(stated or APPROVE, "", stated, rows)
