#!/usr/bin/env python3
"""Turn a completed bench_ab.py run into a chart-first flow report.

Reads the artifacts scripts/bench_ab.py already writes to its --workdir
(summary.txt, logs/run.log, logs/client-{baseline,judge}.log,
logs/transcript-{baseline,judge}.txt) plus, when the run set
ENGINE_TRACE_CALLS=1 (bench_ab.py does this automatically), each side's
<ws-baseline|ws-judge>/.engine/trace.jsonl -- a full-fidelity record of every
judge and tool call's request/response, and the model's stated reasoning for
each tool call.

Renders one self-contained HTML file: a side-by-side Mermaid flowchart, small
bar charts for cost/token/gate-verdict comparisons, and a synthesized "why"
section up front. Per-call detail (judge request/response, tool
arguments/result/reasoning) lives in collapsed <details> so the page opens
chart-first, not text-first.

    python scripts/bench_flow.py --workdir /tmp/bench-empty

Writes <workdir>/flow-report.html by default.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

SIDES = ("baseline", "judge")

# ---------------------------------------------------------------------------
# text normalization
# ---------------------------------------------------------------------------

_LIST_LINE = re.compile(r"^\s*(-|\*|\d+\.)\s+")
_UL_MARKER_RE = re.compile(r"^\s*-\s+")
_OL_MARKER_RE = re.compile(r"^\s*\d+\.\s+")


def dewrap(text: str) -> str:
    """Collapse the mid-token line breaks streamed LLM output leaves behind.

    Event/summary text in these logs is captured chunk-by-chunk as it streams
    off the model, and whoever wrote it joined chunks with a bare newline --
    so a word or sentence can be split exactly at a token boundary (e.g.
    "...visibly broken, b" / "roken code..."). Blank lines are real paragraph
    breaks and list lines are real list items; a single newline elsewhere is
    a streaming artifact, not intentional formatting.
    """
    text = (text or "").replace("\r", "")
    paragraphs: list[list[str]] = []
    buf: list[str] = []
    for line in text.split("\n"):
        if line.strip() == "":
            if buf:
                paragraphs.append(buf)
                buf = []
            continue
        buf.append(line)
    if buf:
        paragraphs.append(buf)

    out_paragraphs = []
    for lines in paragraphs:
        merged = [lines[0]]
        for line in lines[1:]:
            if _LIST_LINE.match(line):
                merged.append(line.strip())
                continue
            prev = merged[-1]
            prev_char = prev[-1] if prev else ""
            next_char = line[0] if line else ""
            if prev_char.isalpha() and prev_char.islower() and next_char.isalpha() and next_char.islower():
                merged[-1] = prev + line
            else:
                merged[-1] = prev + " " + line
        out_paragraphs.append("\n".join(merged))
    return "\n\n".join(out_paragraphs)


def md_lite(text: str) -> str:
    """Very small markdown-ish -> HTML converter for narrative blocks."""
    text = dewrap(text)
    if not text.strip():
        return "<p><em>(none)</em></p>"
    esc = html.escape(text)
    esc = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc)
    esc = re.sub(r"`([^`]+)`", r"<code>\1</code>", esc)
    parts = []
    for para in esc.split("\n\n"):
        lines = [ln for ln in para.split("\n") if ln.strip()]
        if not lines:
            continue
        if all(re.match(r"^\s*-\s+", ln) for ln in lines):
            stripped = [_UL_MARKER_RE.sub("", ln) for ln in lines]
            items = "".join(f"<li>{ln}</li>" for ln in stripped)
            parts.append(f"<ul>{items}</ul>")
        elif all(re.match(r"^\s*\d+\.\s+", ln) for ln in lines):
            stripped = [_OL_MARKER_RE.sub("", ln) for ln in lines]
            items = "".join(f"<li>{ln}</li>" for ln in stripped)
            parts.append(f"<ol>{items}</ol>")
        else:
            parts.append("<p>" + "<br>".join(lines) + "</p>")
    return "\n".join(parts) or "<p><em>(none)</em></p>"


def short(text: str, limit: int = 160) -> str:
    text = " ".join(dewrap(text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def mm_escape(text: str, limit: int = 260) -> str:
    text = dewrap(text or "").replace('"', "'").replace("\n", "<br/>")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def mm_id(*parts: str) -> str:
    raw = "_".join(parts)
    return re.sub(r"[^A-Za-z0-9_]", "_", raw)


def json_block(value: Any, limit: int = 4000) -> str:
    try:
        text = json.dumps(value, indent=2, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) > limit:
        text = text[:limit] + f"\n…[{len(text) - limit} more chars]"
    return f"<pre class='mono req'>{html.escape(text)}</pre>"


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------


@dataclass
class JudgeCall:
    kind: str
    verdict: str
    mode: str
    ms: int
    agent_id: str | None
    subject: str
    scores: dict[str, float]
    line_no: int
    request: Any = None
    response: dict[str, Any] | None = None


@dataclass
class ToolCall:
    name: str
    agent_id: str | None
    state: str
    ms: int | None
    line_no: int


@dataclass
class AgentPhase:
    profile: str
    agent_id: str
    batch: str
    task_preview: str
    worktree: str = ""
    branch: str = ""
    start_line: int = 0
    end_line: int | None = None
    status: str = ""
    cost: float = 0.0
    tokens: int = 0
    cached: int = 0
    summary_preview: str = ""
    judge_calls: list[JudgeCall] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    trace_tools: list[dict] = field(default_factory=list)
    fields: dict[str, list[str]] = field(default_factory=dict)

    def field(self, *names: str) -> str:
        for name in names:
            values = self.fields.get(name)
            if values:
                return values[-1]
        return ""

    def judge_tally(self) -> Counter:
        return Counter((c.kind, c.verdict) for c in self.judge_calls)

    def tool_tally(self) -> Counter:
        return Counter(c.name for c in self.tool_calls if c.state != "started")


@dataclass
class NarrativeItem:
    kind: str  # "phase" | "message" | "settle"
    line_no: int
    phase: AgentPhase | None = None
    text: str = ""
    settle_action: str = ""
    settle_profile: str = ""
    settle_detail: str = ""
    pr_url: str = ""


@dataclass
class SideFlow:
    name: str
    session_id: str = ""
    prompt: str = ""
    orchestrator_judge: list[JudgeCall] = field(default_factory=list)
    other_tool_calls: list[ToolCall] = field(default_factory=list)
    orchestrator_trace_tools: list[dict] = field(default_factory=list)
    phases: list[AgentPhase] = field(default_factory=list)
    narrative: list[NarrativeItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    aborted: bool = False
    pr_url: str = ""
    final_reply: str = ""
    has_trace: bool = False

    def orchestrator_tally(self) -> Counter:
        return Counter((c.kind, c.verdict) for c in self.orchestrator_judge)

    def all_judge_calls(self) -> list[JudgeCall]:
        calls = list(self.orchestrator_judge)
        for phase in self.phases:
            calls.extend(phase.judge_calls)
        return calls

    def all_trace_tools(self) -> list[dict]:
        records = list(self.orchestrator_trace_tools)
        for phase in self.phases:
            records.extend(phase.trace_tools)
        return records


# ---------------------------------------------------------------------------
# parsing logs/client-<side>.log
# ---------------------------------------------------------------------------

_JUDGE_RE = re.compile(
    r"^judge (?P<kind>\S+) -> (?P<verdict>\S+) \((?P<mode>enforced|advisory), (?P<ms>\d+)ms\)"
    r"(?: \[(?P<agent>[0-9a-f]{6,40})\])?: (?P<subject>.*)$"
)
_SCORE_RE = re.compile(r"^\s*([A-Za-z0-9_]+=-?[0-9.]+(?:\s+[A-Za-z0-9_]+=-?[0-9.]+)*)\s*$")
_KV_RE = re.compile(r"([A-Za-z0-9_]+)=(-?[0-9.]+)")
_TOOL_RE = re.compile(
    r"^tool (?P<name>\S+) (?P<state>started|ok|error)(?: \((?P<ms>\d+)ms\))?"
    r"(?: \[(?P<agent>[0-9a-f]{6,40})\])?"
)
_AGENT_STARTED_RE = re.compile(
    r"^agent started (?P<profile>\S+) (?P<id>[0-9a-f]{8,40}) batch=(?P<batch>.*?) task=(?P<task>.*?)"
    r"(?: worktree=(?P<worktree>\S+) branch=(?P<branch>\S+))?$"
)
_AGENT_FINISHED_RE = re.compile(
    r"^agent finished (?P<profile>\S+) (?P<id>[0-9a-f]{8,40}) (?P<status>[a-zA-Z_]+):"
    r"(?: \$(?P<cost>[0-9.]+) (?P<tokens>\d+) tok (?P<cached>\d+) cached)? ?(?P<rest>.*)$"
)
_STOP_LOOKAHEAD = re.compile(r"^(judge |tool |agent |tokens |session |user:|warning:|\{)")
_BOARD_LINE = re.compile(r"^agents: \d+ running$")


def _find_scores(lines: list[str], start: int) -> dict[str, float]:
    for j in range(start, min(start + 15, len(lines))):
        cand = lines[j]
        if j > start and (_STOP_LOOKAHEAD.match(cand) or _BOARD_LINE.match(cand)):
            return {}
        m = _SCORE_RE.match(cand)
        if m:
            return {k: float(v) for k, v in _KV_RE.findall(cand)}
    return {}


def parse_client_log(path: Path) -> tuple[
    str, str, list[JudgeCall], list[ToolCall], list[AgentPhase], dict[str, AgentPhase], list[str]
]:
    text = path.read_text(errors="replace") if path.exists() else ""
    lines = text.split("\n")
    session_id = ""
    prompt = ""
    orchestrator_judge: list[JudgeCall] = []
    other_tools: list[ToolCall] = []
    phases: list[AgentPhase] = []
    by_id: dict[str, AgentPhase] = {}
    warnings: list[str] = []

    for i, line in enumerate(lines):
        if not session_id and line.startswith("session "):
            session_id = line.split(" ", 1)[1].strip()
            continue
        if not prompt and line.startswith("user: "):
            prompt = line[len("user: "):].strip()
            continue
        if line.startswith("warning: "):
            warnings.append(line[len("warning: "):].strip())
            continue

        m = _JUDGE_RE.match(line)
        if m:
            call = JudgeCall(
                kind=m.group("kind"),
                verdict=m.group("verdict"),
                mode=m.group("mode"),
                ms=int(m.group("ms")),
                agent_id=m.group("agent"),
                subject=m.group("subject"),
                scores=_find_scores(lines, i + 1),
                line_no=i,
            )
            target = by_id.get(call.agent_id) if call.agent_id else None
            (target.judge_calls if target else orchestrator_judge).append(call)
            continue

        m = _TOOL_RE.match(line)
        if m:
            call = ToolCall(
                name=m.group("name"),
                agent_id=m.group("agent"),
                state=m.group("state"),
                ms=int(m.group("ms")) if m.group("ms") else None,
                line_no=i,
            )
            target = by_id.get(call.agent_id) if call.agent_id else None
            (target.tool_calls if target else other_tools).append(call)
            continue

        m = _AGENT_STARTED_RE.match(line)
        if m:
            phase = AgentPhase(
                profile=m.group("profile"),
                agent_id=m.group("id"),
                batch=m.group("batch"),
                task_preview=m.group("task"),
                worktree=m.group("worktree") or "",
                branch=m.group("branch") or "",
                start_line=i,
            )
            phases.append(phase)
            by_id[phase.agent_id] = phase
            continue

        m = _AGENT_FINISHED_RE.match(line)
        if m:
            phase = by_id.get(m.group("id"))
            if phase is not None:
                phase.end_line = i
                phase.status = m.group("status")
                phase.cost = float(m.group("cost") or 0.0)
                phase.tokens = int(m.group("tokens") or 0)
                phase.cached = int(m.group("cached") or 0)
                phase.summary_preview = m.group("rest") or ""
            continue

    return session_id, prompt, orchestrator_judge, other_tools, phases, by_id, warnings


# ---------------------------------------------------------------------------
# parsing logs/transcript-<side>.txt
# ---------------------------------------------------------------------------

_BLOCK_RE = re.compile(r"^## (\w+)\s*$", re.M)
_TAG_RE = re.compile(r"^\[(.*?)\]\n?(.*)", re.S)
_FIELD_LABELS = (
    "status",
    "summary",
    "what",
    "paths",
    "facts",
    "outcome",
    "verdict",
    "leftover_questions",
    "leftover",
    "files_touched",
)
_FIELD_RE = re.compile(
    r"(?m)^(" + "|".join(_FIELD_LABELS) + r"):[ \t]*"
)
_URL_RE = re.compile(r"https?://\S+")


def _split_fields(body: str) -> dict[str, list[str]]:
    matches = list(_FIELD_RE.finditer(body))
    out: dict[str, list[str]] = defaultdict(list)
    for idx, m in enumerate(matches):
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        out[m.group(1)].append(body[start:end].strip())
    return dict(out)


def parse_transcript(path: Path) -> list[tuple[str, str]]:
    """Return ordered (role, raw_block_text) pairs, role in user/assistant/engine."""
    text = path.read_text(errors="replace") if path.exists() else ""
    if not text.strip():
        return []
    matches = list(_BLOCK_RE.finditer(text))
    out = []
    for idx, m in enumerate(matches):
        role = m.group(1)
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        out.append((role, text[start:end].strip()))
    return out


# ---------------------------------------------------------------------------
# parsing <ws-side>/.engine/trace.jsonl (optional; ENGINE_TRACE_CALLS=1)
# ---------------------------------------------------------------------------


def parse_trace(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def correlate_judge_trace(side: SideFlow, trace_records: list[dict]) -> None:
    """Attach full request/response onto each parsed JudgeCall.

    The trace file has no agent_id for judge calls (kept deliberately thin --
    see runtime/judge.py), so calls are matched to trace records by (tag,
    file order) only. That's exact whenever a tag's call count matches
    between the two sources (the common case: calls of one tag fire
    strictly in sequence); if concurrent agents interleaved calls of the
    same tag and the counts still happen to match, correlation could pair
    the wrong record with the wrong call, so this stays a best-effort
    enrichment, never load-bearing for the verdicts/scores already parsed
    from the text log.
    """
    calls = sorted(side.all_judge_calls(), key=lambda c: c.line_no)
    calls_by_tag: dict[str, list[JudgeCall]] = defaultdict(list)
    for c in calls:
        calls_by_tag[c.kind].append(c)

    records_by_tag: dict[str, list[dict]] = defaultdict(list)
    for r in trace_records:
        if r.get("kind") == "judge":
            records_by_tag[r.get("tag", "")].append(r)

    for tag, tagged_calls in calls_by_tag.items():
        records = records_by_tag.get(tag, [])
        if len(records) != len(tagged_calls):
            continue
        for call, rec in zip(tagged_calls, records):
            call.request = rec.get("request")
            call.response = rec.get("response")


def correlate_tool_trace(side: SideFlow, trace_records: list[dict]) -> None:
    """Full tool call records (args/result/reasoning) keyed by agent_id --
    an exact match, since the trace carries the same agent_id the text log
    does."""
    by_agent: dict[str, list[dict]] = defaultdict(list)
    for r in trace_records:
        if r.get("kind") == "tool":
            by_agent[r.get("agent_id") or ""].append(r)
    for phase in side.phases:
        phase.trace_tools = by_agent.get(phase.agent_id, [])
    side.orchestrator_trace_tools = by_agent.get("", [])


# ---------------------------------------------------------------------------
# assembling a SideFlow
# ---------------------------------------------------------------------------

_ABORT_TEXT = "(aborted by the user)"


def build_side(name: str, workdir: Path) -> SideFlow:
    logs_dir = workdir / "logs"
    client_log = logs_dir / f"client-{name}.log"
    transcript = logs_dir / f"transcript-{name}.txt"
    trace_path = workdir / f"ws-{name}" / ".engine" / "trace.jsonl"

    session_id, prompt, orch_judge, other_tools, phases, by_id, warnings = parse_client_log(client_log)
    blocks = parse_transcript(transcript)

    side = SideFlow(
        name=name,
        session_id=session_id,
        prompt=prompt,
        orchestrator_judge=orch_judge,
        other_tool_calls=other_tools,
        phases=phases,
        warnings=warnings,
    )

    phase_by_id8: dict[str, AgentPhase] = {}
    for phase in phases:
        phase_by_id8[phase.agent_id[:8]] = phase

    last_role_text = ""
    for line_no, (role, body) in enumerate(blocks):
        if role == "user":
            if not side.prompt:
                side.prompt = body.strip()
            continue
        if role == "assistant":
            last_role_text = body.strip()
            side.narrative.append(NarrativeItem(kind="message", line_no=line_no, text=body.strip()))
            continue
        if role != "engine":
            continue
        m = _TAG_RE.match(body)
        if not m:
            continue
        tag, rest = m.group(1), m.group(2)
        tokens = tag.split()
        if tokens[0] == "agent" and len(tokens) >= 3:
            profile, id8 = tokens[1], tokens[2]
            phase = phase_by_id8.get(id8)
            if phase is None:
                phase = AgentPhase(profile=profile, agent_id=id8, batch="", task_preview="")
                phases.append(phase)
                phase_by_id8[id8] = phase
            phase.fields = _split_fields(rest)
            side.narrative.append(NarrativeItem(kind="phase", line_no=line_no, phase=phase))
            continue
        if tokens[0] == "worktree" and len(tokens) >= 4:
            profile, id8, action = tokens[1], tokens[2], tokens[3]
            url_match = _URL_RE.search(rest)
            pr_url = url_match.group(0).rstrip(".,;:!?*") if url_match else ""
            if pr_url:
                side.pr_url = pr_url
            side.narrative.append(
                NarrativeItem(
                    kind="settle",
                    line_no=line_no,
                    settle_action=action,
                    settle_profile=profile,
                    settle_detail=dewrap(rest.strip()),
                    pr_url=pr_url,
                )
            )
            continue

    side.phases = phases
    if last_role_text == _ABORT_TEXT:
        side.aborted = True
        side.final_reply = _ABORT_TEXT
    else:
        side.final_reply = last_role_text

    trace_records = parse_trace(trace_path)
    side.has_trace = bool(trace_records)
    if trace_records:
        correlate_judge_trace(side, trace_records)
        correlate_tool_trace(side, trace_records)

    return side


# ---------------------------------------------------------------------------
# color tokens (validated categorical + status palette -- see the dataviz
# skill; dark-surface steps only, this report is dark-only)
# ---------------------------------------------------------------------------

_C_BASE = "#3987e5"    # categorical slot 1 (blue) -- baseline series
_C_JUDGE = "#d95926"   # categorical slot 2 (orange) -- judge series
_C_SURFACE = "#1a1a19"
_C_INK = "#ffffff"
_C_INK2 = "#c3c2b7"
_C_MUTED = "#898781"
_C_GRID = "#2c2c2a"
_C_GOOD = "#0ca30c"
_C_WARN = "#fab219"
_C_CRIT = "#d03b3b"

_KIND_ABBR = {
    "intent_route": "intent",
    "loop_control": "loop",
    "merge_gate": "merge",
    "search_rerank": "search",
    "call_verify": "verify",
    "write_gate": "write",
    "exec_approval": "exec",
    "result_screen": "result",
}

_BAD_VERDICTS = {"block", "deny", "reject"}
_WARN_VERDICTS = {"flag", "prompt", "needs_input", "ambiguous", "extend"}


def _verdict_class(verdict: str) -> str:
    if verdict in _BAD_VERDICTS:
        return "bad"
    if verdict in _WARN_VERDICTS:
        return "warn"
    return "ok"


def _tally_label(tally: Counter, limit: int = 4) -> str:
    parts = []
    for (kind, verdict), count in tally.most_common(limit):
        abbr = _KIND_ABBR.get(kind, kind)
        parts.append(f"{abbr}:{verdict}×{count}")
    return " · ".join(parts)


def _phase_class(phase: AgentPhase) -> str:
    tally = phase.judge_tally()
    if any(v in _BAD_VERDICTS for (_, v) in tally):
        return "bad"
    verdict_text = (phase.field("verdict") or "").lower()
    if "request changes" in verdict_text or "block" in verdict_text:
        return "warn"
    if any(v in _WARN_VERDICTS for (_, v) in tally):
        return "warn"
    return "ok"


# ---------------------------------------------------------------------------
# svg mini-charts (no library -- plain inline SVG, dataviz mark specs:
# rounded data-end / square baseline, 2px surface gaps, hairline gridlines)
# ---------------------------------------------------------------------------


def _hbar(x: float, y: float, w: float, h: float, fill: str, r: float = 4) -> str:
    w = max(w, 0.0)
    r = min(r, h / 2)
    if w <= r:
        rr = min(r, w / 2) if w > 0 else 0
        return f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rr:.1f}" fill="{fill}"/>'
    return (
        f'<path d="M{x:.1f},{y:.1f} H{x + w - r:.1f} A{r:.1f},{r:.1f} 0 0 1 {x + w:.1f},{y + r:.1f} '
        f'V{y + h - r:.1f} A{r:.1f},{r:.1f} 0 0 1 {x + w - r:.1f},{y + h:.1f} H{x:.1f} Z" fill="{fill}"/>'
    )


def svg_metric_grid(pairs: list[tuple[str, float, float, str]]) -> str:
    """One paired-bar small multiple per (label, baseline_val, judge_val, unit)."""
    cards = []
    for label, base_v, judge_v, unit in pairs:
        maxv = max(base_v, judge_v, 1e-9)
        track = 150.0
        x0 = 4.0
        bar_h = 13.0
        gap = 5.0
        y1, y2 = 2.0, 2.0 + bar_h + gap
        w1 = max(1.5, base_v / maxv * track) if maxv > 0 else 0
        w2 = max(1.5, judge_v / maxv * track) if maxv > 0 else 0
        h = y2 + bar_h + 4
        fmt = (lambda v: f"${v:,.2f}") if unit == "$" else (lambda v: f"{v:,.0f}{('' if unit=='' else unit)}")
        svg = (
            f'<svg viewBox="0 0 240 {h:.0f}" width="100%" height="{h:.0f}" '
            f'role="img" aria-label="{html.escape(label)}: baseline {html.escape(fmt(base_v))}, judge {html.escape(fmt(judge_v))}">'
            + _hbar(x0, y1, w1, bar_h, _C_BASE)
            + f'<text x="{x0 + w1 + 6:.1f}" y="{y1 + bar_h - 2.5:.1f}" fill="{_C_INK2}" font-size="10" font-family="ui-monospace,monospace">{html.escape(fmt(base_v))}</text>'
            + _hbar(x0, y2, w2, bar_h, _C_JUDGE)
            + f'<text x="{x0 + w2 + 6:.1f}" y="{y2 + bar_h - 2.5:.1f}" fill="{_C_INK2}" font-size="10" font-family="ui-monospace,monospace">{html.escape(fmt(judge_v))}</text>'
            + "</svg>"
        )
        cards.append(f'<div class="metric-card"><div class="metric-label">{html.escape(label)}</div>{svg}</div>')
    legend = (
        '<div class="legend">'
        f'<span class="legend-item"><i style="background:{_C_BASE}"></i>baseline</span>'
        f'<span class="legend-item"><i style="background:{_C_JUDGE}"></i>judge</span>'
        "</div>"
    )
    return legend + '<div class="metric-grid">' + "".join(cards) + "</div>"


def svg_verdict_grid(calls: list[JudgeCall]) -> str:
    """One stacked bar per judge tag: segment width = verdict count, colored
    by status (good/warn/crit)."""
    if not calls:
        return "<p class='hint'>no judge activity on this side.</p>"
    by_tag: dict[str, Counter] = defaultdict(Counter)
    for c in calls:
        by_tag[c.kind][c.verdict] += 1

    cards = []
    for tag in sorted(by_tag, key=lambda t: -sum(by_tag[t].values())):
        tally = by_tag[tag]
        total = sum(tally.values())
        track = 190.0
        x = 4.0
        y = 4.0
        bar_h = 16.0
        segs = []
        labels = []
        for verdict, count in tally.most_common():
            w = max(2.0, count / total * track)
            color = {"ok": _C_GOOD, "warn": _C_WARN, "bad": _C_CRIT}[_verdict_class(verdict)]
            segs.append(_hbar(x, y, max(w - 2, 1.5), bar_h, color))
            x += w
            labels.append(f"{html.escape(verdict)}×{count}")
        svg = (
            f'<svg viewBox="0 0 {track + 8:.0f} {y + bar_h + 4:.0f}" width="100%" height="{y + bar_h + 4:.0f}" '
            f'role="img" aria-label="{html.escape(tag)}: {", ".join(labels)}">' + "".join(segs) + "</svg>"
        )
        cards.append(
            f'<div class="metric-card"><div class="metric-label">{html.escape(_KIND_ABBR.get(tag, tag))} '
            f'<span class="dim">({total})</span></div>{svg}'
            f'<div class="metric-sub">{" · ".join(labels)}</div></div>'
        )
    legend = (
        '<div class="legend">'
        f'<span class="legend-item"><i style="background:{_C_GOOD}"></i>allow/ok</span>'
        f'<span class="legend-item"><i style="background:{_C_WARN}"></i>flag/prompt/needs input</span>'
        f'<span class="legend-item"><i style="background:{_C_CRIT}"></i>block/deny</span>'
        "</div>"
    )
    return legend + '<div class="metric-grid">' + "".join(cards) + "</div>"


def svg_tool_bars(tally: Counter, color: str, limit: int = 8) -> str:
    if not tally:
        return "<p class='hint'>no tool calls.</p>"
    top = tally.most_common(limit)
    maxv = max(c for _, c in top) or 1
    row_h = 18.0
    track = 160.0
    x0 = 90.0
    rows = []
    y = 2.0
    for name, count in top:
        w = max(2.0, count / maxv * track)
        rows.append(
            f'<text x="{x0 - 6:.1f}" y="{y + 12:.1f}" fill="{_C_INK2}" font-size="11" '
            f'font-family="ui-monospace,monospace" text-anchor="end">{html.escape(name)}</text>'
            + _hbar(x0, y + 1, w, 13, color)
            + f'<text x="{x0 + w + 6:.1f}" y="{y + 12:.1f}" fill="{_C_MUTED}" font-size="10" '
            f'font-family="ui-monospace,monospace">{count}</text>'
        )
        y += row_h
    return (
        f'<svg viewBox="0 0 {x0 + track + 40:.0f} {y:.0f}" width="100%" height="{y:.0f}" role="img" '
        f'aria-label="tool call counts">' + "".join(rows) + "</svg>"
    )


# ---------------------------------------------------------------------------
# mermaid rendering
# ---------------------------------------------------------------------------


def render_comparison_mermaid(sides: dict[str, SideFlow]) -> str:
    lines = ["flowchart LR"]
    classdefs = [
        'classDef ok fill:#1f6f43,stroke:#0d3822,color:#eafff2;',
        'classDef warn fill:#8a6d1f,stroke:#4d3c0f,color:#fff7e0;',
        'classDef bad fill:#7a2020,stroke:#421010,color:#ffecec;',
        'classDef info fill:#2b3a55,stroke:#16202f,color:#e7ecf5;',
        'classDef pr fill:#155724,stroke:#0b2e14,color:#d4ffe0,stroke-width:2px;',
        'classDef abort fill:#6b1d1d,stroke:#3a0f0f,color:#ffe2e2,stroke-width:2px;',
    ]
    class_assignments: list[str] = []

    for name in SIDES:
        side = sides.get(name)
        if side is None:
            continue
        sub = mm_id("sub", name)
        title = "Baseline (judge off)" if name == "baseline" else "Judge (enforcing)"
        lines.append(f'  subgraph {sub}["{title}"]')
        lines.append("    direction TB")

        user_id = mm_id(name, "user")
        lines.append(f'    {user_id}["User prompt"]')
        class_assignments.append(f"class {user_id} info")
        prev_id = user_id

        gate_tally = side.orchestrator_tally()
        if gate_tally:
            gate_id = mm_id(name, "gate")
            label = "judge (orchestrator)<br/>" + _tally_label(gate_tally, limit=6)
            lines.append(f'    {gate_id}["{label}"]')
            lines.append(f"    {prev_id} --> {gate_id}")
            class_assignments.append(f"class {gate_id} info")
            prev_id = gate_id

        for phase in side.phases:
            node_id = mm_id(name, "phase", phase.agent_id[:8])
            tally = phase.judge_tally()
            gate_line = _tally_label(tally) if tally else ""
            cost_line = f"${phase.cost:.3f} · {phase.tokens:,} tok" if phase.tokens else ""
            label_lines = [f"<b>{phase.profile}</b>"]
            if cost_line:
                label_lines.append(cost_line)
            if gate_line:
                label_lines.append(gate_line)
            label = "<br/>".join(label_lines)
            lines.append(f'    {node_id}["{label}"]')
            lines.append(f"    {prev_id} --> {node_id}")
            class_assignments.append(f"class {node_id} {_phase_class(phase)}")
            prev_id = node_id

        end_id = mm_id(name, "end")
        if side.aborted:
            lines.append(f'    {end_id}["Aborted by user<br/>no PR"]')
            lines.append(f"    {prev_id} --> {end_id}")
            class_assignments.append(f"class {end_id} abort")
        elif side.pr_url:
            lines.append(f'    {end_id}["PR opened"]')
            lines.append(f"    {prev_id} --> {end_id}")
            class_assignments.append(f"class {end_id} pr")
        else:
            label = mm_escape(side.final_reply or "(no terminal reply)", limit=80)
            lines.append(f'    {end_id}["{label}"]')
            lines.append(f"    {prev_id} --> {end_id}")
            class_assignments.append(f"class {end_id} info")

        lines.append("  end")

    lines.extend(classdefs)
    lines.extend(class_assignments)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

_CSS = """
:root {
  --bg: #0f1115; --panel: #171a21; --panel2: #1d212a; --border: #2a2f3a;
  --text: #e6e9ef; --dim: #9aa3b2; --accent: #6ea8fe; --good: #3fb95f;
  --warn: #e0a63a; --bad: #e0533f;
}
* { box-sizing: border-box; }
body {
  background: var(--bg); color: var(--text); margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  line-height: 1.5;
}
.wrap { max-width: 1180px; margin: 0 auto; padding: 24px 20px 70px; }
h1 { font-size: 20px; margin-bottom: 4px; }
h2 { font-size: 15.5px; margin: 30px 0 10px; border-bottom: 1px solid var(--border); padding-bottom: 6px; }
h3 { font-size: 14px; margin: 0 0 6px; }
.sub { color: var(--dim); font-size: 12.5px; margin-bottom: 3px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.dim { color: var(--dim); }
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; margin-bottom: 12px; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
@media (max-width: 900px) { .grid2 { grid-template-columns: 1fr; } }
table { border-collapse: collapse; width: 100%; font-size: 12.5px; }
th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
th { color: var(--dim); font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 10.5px; font-weight: 600; }
.badge.ok { background: rgba(63,185,95,.18); color: var(--good); }
.badge.warn { background: rgba(224,166,58,.18); color: var(--warn); }
.badge.bad { background: rgba(224,83,63,.18); color: var(--bad); }

.hero { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-bottom: 14px; }
.tile { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }
.tile .label { color: var(--dim); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
.tile .value { font-size: 20px; font-weight: 600; margin-top: 2px; }
.tile .value.good { color: var(--good); }
.tile .value.bad { color: var(--bad); }

.metric-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; }
.metric-card { background: var(--panel2); border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; }
.metric-label { font-size: 11.5px; color: var(--dim); margin-bottom: 3px; }
.metric-sub { font-size: 10.5px; color: var(--dim); margin-top: 2px; }
.legend { display: flex; gap: 14px; font-size: 11.5px; color: var(--dim); margin: 4px 0 8px; }
.legend-item i { display: inline-block; width: 9px; height: 9px; border-radius: 2px; margin-right: 5px; vertical-align: middle; }

.phase-card { border: 1px solid var(--border); border-radius: 8px; margin: 8px 0; background: var(--panel2); overflow: hidden; }
.phase-card > summary { list-style: none; cursor: pointer; padding: 8px 12px; display: flex; justify-content: space-between; gap: 10px; flex-wrap: wrap; align-items: baseline; font-size: 13px; color: var(--text); }
.phase-card > summary::-webkit-details-marker { display: none; }
.phase-card > summary:hover { background: rgba(255,255,255,.03); }
.phase-body { padding: 4px 14px 12px; }
.call { border: 1px solid var(--border); border-radius: 6px; margin: 6px 0; background: var(--panel); }
.call > summary { list-style: none; cursor: pointer; padding: 5px 9px; font-size: 12px; color: var(--dim); }
.call > summary::-webkit-details-marker { display: none; }
.call > summary:hover { color: var(--text); }
.call > *:not(summary) { padding: 0 9px 8px; }
.req { max-height: 260px; overflow: auto; font-size: 11px; background: rgba(0,0,0,.25); padding: 8px; border-radius: 6px; white-space: pre-wrap; word-break: break-word; }
.sec-label { font-size: 10.5px; color: var(--dim); text-transform: uppercase; letter-spacing: .04em; margin: 8px 0 3px; }
.kv { display: grid; grid-template-columns: auto auto; gap: 2px 10px; font-size: 11px; }
.hint { color: var(--dim); font-size: 12px; font-style: italic; }

.msg-card { border-left: 3px solid var(--accent); padding: 6px 12px; margin: 8px 0; background: var(--panel2); border-radius: 0 8px 8px 0; font-size: 13px; }
.settle-card { border-left: 3px solid var(--good); padding: 8px 14px; margin: 8px 0; background: var(--panel2); border-radius: 0 8px 8px 0; }
details { margin-top: 6px; }
summary { cursor: pointer; color: var(--dim); font-size: 12px; }
summary:hover { color: var(--text); }
ul, ol { margin: 4px 0; padding-left: 20px; }
p { margin: 5px 0; }
code { background: rgba(255,255,255,.08); padding: 1px 5px; border-radius: 4px; font-size: 12px; }
.pill { display:inline-block; background: rgba(110,168,254,.15); color: var(--accent); border-radius:6px; padding:1px 7px; font-size:11px; margin-right:4px;}
a { color: var(--accent); }
.why { font-size: 13px; }
.why li { margin-bottom: 5px; }
footer { color: var(--dim); font-size: 11.5px; margin-top: 40px; border-top: 1px solid var(--border); padding-top: 12px; }
.mermaid { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 10px; }
"""


def _fmt_int(v) -> str:
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return str(v)


def render_judge_call_detail(call: JudgeCall) -> str:
    cls = _verdict_class(call.verdict)
    score_str = "  ".join(f"{k}={v:.2f}" for k, v in sorted(call.scores.items()))
    summary = (
        f"<span class='mono'>{html.escape(call.kind)}</span> → <b>{html.escape(call.verdict)}</b> "
        f"<span class='badge {cls}'>{cls}</span> "
        f"<span class='dim'>{html.escape(call.mode)} · {call.ms}ms</span>"
    )
    body = []
    if score_str:
        body.append(f"<div class='sec-label'>scores</div><div class='mono' style='font-size:11px'>{html.escape(score_str)}</div>")
    if call.request is not None:
        body.append(f"<div class='sec-label'>request (state sent to the judge)</div>{json_block(call.request)}")
    if call.response:
        body.append(f"<div class='sec-label'>response (every answer)</div>{json_block(call.response)}")
    if call.request is None and not call.response:
        body.append(
            "<p class='hint'>full request/response not captured for this run — "
            "set ENGINE_TRACE_CALLS=1 and re-run bench_ab.py to get it.</p>"
        )
    return f"<details class='call'><summary>{summary}</summary>{''.join(body)}</details>"


def render_tool_call_detail(rec: dict) -> str:
    ok = rec.get("ok", True)
    badge = "ok" if ok else "bad"
    summary = (
        f"<span class='mono'>{html.escape(str(rec.get('name', '')))}</span> "
        f"<span class='badge {badge}'>{'ok' if ok else 'error'}</span> "
        f"<span class='dim'>{rec.get('duration_ms', 0)}ms</span>"
    )
    body = []
    reasoning = (rec.get("reasoning") or "").strip()
    if reasoning:
        body.append(f"<div class='sec-label'>model's stated reasoning this turn</div>{md_lite(reasoning)}")
    if rec.get("arguments") is not None:
        body.append(f"<div class='sec-label'>request (arguments)</div>{json_block(rec['arguments'])}")
    if rec.get("result") is not None:
        body.append(f"<div class='sec-label'>response</div><pre class='mono req'>{html.escape(str(rec['result'])[:4000])}</pre>")
    return f"<details class='call'><summary>{summary}</summary>{''.join(body)}</details>"


def render_tool_table(calls: list[ToolCall]) -> str:
    tally: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for c in calls:
        if c.state == "started":
            continue
        row = tally[c.name]
        row[0 if c.state == "ok" else 1] += 1
        row[2] += c.ms or 0
    if not tally:
        return "<p class='hint'>none</p>"
    rows = []
    for name, (ok, err, ms) in sorted(tally.items(), key=lambda kv: -(kv[1][0] + kv[1][1])):
        rows.append(
            f"<tr><td class='mono'>{html.escape(name)}</td>"
            f"<td class='num'>{ok}</td><td class='num'>{err}</td>"
            f"<td class='num'>{ms/1000:.1f}s</td></tr>"
        )
    return (
        "<table><thead><tr><th>tool</th><th class='num'>ok</th>"
        "<th class='num'>error</th><th class='num'>time</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def render_phase_card(phase: AgentPhase) -> str:
    cls = _phase_class(phase)
    badge = f"<span class='badge {cls}'>{cls}</span>"
    outcome = phase.field("outcome", "summary", "what")
    verdict = phase.field("verdict")
    leftover = " ".join(x for x in [phase.field("leftover"), phase.field("leftover_questions")] if x)
    paths = phase.field("paths")
    facts = phase.field("facts")

    narrative = []
    narrative.append(md_lite(outcome) if outcome else "<p class='hint'>no summary captured</p>")
    if verdict:
        narrative.append(f"<div class='sec-label'>verdict</div>{md_lite(verdict)}")
    if facts:
        narrative.append(f"<details><summary>facts</summary>{md_lite(facts)}</details>")
    if paths:
        narrative.append(f"<details><summary>paths touched</summary>{md_lite(paths)}</details>")
    if leftover:
        narrative.append(f"<details><summary>leftover</summary>{md_lite(leftover)}</details>")

    if phase.trace_tools:
        tool_html = "".join(render_tool_call_detail(r) for r in phase.trace_tools)
        n_tools = len(phase.trace_tools)
    else:
        n_tools = sum(1 for c in phase.tool_calls if c.state != "started")
        tool_html = render_tool_table(phase.tool_calls)
        if n_tools:
            tool_html += "<p class='hint'>full request/response not captured — set ENGINE_TRACE_CALLS=1 and re-run.</p>"

    judge_html = "".join(render_judge_call_detail(c) for c in phase.judge_calls) if phase.judge_calls else "<p class='hint'>none</p>"

    meta = f"${phase.cost:.3f} · {_fmt_int(phase.tokens)} tok"
    return f"""
<details class="phase-card">
  <summary>
    <span>{badge} <b>{html.escape(phase.profile)}</b> <span class="mono dim">{html.escape(phase.agent_id[:8])}</span></span>
    <span class="dim">{meta}</span>
  </summary>
  <div class="phase-body">
    {"".join(narrative)}
    <div class="grid2">
      <div><div class="sec-label">judge calls ({len(phase.judge_calls)})</div>{judge_html}</div>
      <div><div class="sec-label">tool calls ({n_tools})</div>{tool_html}</div>
    </div>
  </div>
</details>
"""


def render_narrative(side: SideFlow) -> str:
    parts = []
    if side.orchestrator_judge:
        parts.append(
            "<details class='phase-card'><summary><span>orchestrator judge gates</span>"
            f"<span class='dim'>{len(side.orchestrator_judge)}</span></summary>"
            f"<div class='phase-body'>{''.join(render_judge_call_detail(c) for c in side.orchestrator_judge)}</div></details>"
        )
    for item in side.narrative:
        if item.kind == "message":
            preview = short(item.text, 220)
            full = md_lite(item.text)
            if preview == item.text.strip():
                parts.append(f"<div class='msg-card'><span class='pill'>orchestrator</span>{html.escape(preview)}</div>")
            else:
                parts.append(
                    f"<details class='msg-card' style='border-left:3px solid var(--accent);padding:6px 12px;background:var(--panel2);border-radius:0 8px 8px 0;'>"
                    f"<summary><span class='pill'>orchestrator</span>{html.escape(preview)}</summary>{full}</details>"
                )
        elif item.kind == "phase" and item.phase is not None:
            parts.append(render_phase_card(item.phase))
        elif item.kind == "settle":
            link = f" — <a href='{html.escape(item.pr_url)}' target='_blank'>{html.escape(item.pr_url)}</a>" if item.pr_url else ""
            parts.append(
                f"<div class='settle-card'><span class='pill'>settle · {html.escape(item.settle_action)}</span>"
                f"{md_lite(item.settle_detail)}{link}</div>"
            )
    return "\n".join(parts) or "<p class='hint'>no narrative captured</p>"


def render_why(sides: dict[str, SideFlow], summary_metrics: dict[str, dict[str, str]]) -> str:
    baseline, judge = sides.get("baseline"), sides.get("judge")
    if baseline is None or judge is None:
        return "<p class='hint'>need both baseline and judge sides to compare.</p>"

    bullets: list[str] = []

    def metric(name: str) -> tuple[str, str]:
        row = summary_metrics.get(name, {})
        return row.get("baseline", "—"), row.get("judge", "—")

    b_cost, j_cost = metric("cost usd")
    try:
        cost_delta = float(j_cost) - float(b_cost)
        cheaper = "baseline" if cost_delta > 0 else "judge" if cost_delta < 0 else None
        if cheaper:
            bullets.append(
                f"<strong>{cheaper}</strong> was cheaper by <strong>${abs(cost_delta):,.4f}</strong> "
                f"(baseline ${float(b_cost):,.4f} vs judge ${float(j_cost):,.4f})."
            )
    except ValueError:
        pass

    n_baseline_phases = len(baseline.phases)
    n_judge_phases = len(judge.phases)
    if n_judge_phases > n_baseline_phases:
        extra = [p.profile for p in judge.phases[n_baseline_phases:]]
        bullets.append(
            f"The judge run spawned {n_judge_phases} agents vs baseline's {n_baseline_phases} "
            f"(extra: {', '.join(extra) or '—'}) — the enforcing judge's <code>merge_gate</code> "
            "decisions routed work to an additional agent the baseline orchestrator never used."
        )

    gate_tally: Counter = Counter()
    for p in judge.phases:
        gate_tally.update(p.judge_tally())
    gate_tally.update(judge.orchestrator_tally())
    flagged = sum(c for (k, v), c in gate_tally.items() if k == "write_gate" and v == "flag")
    blocked = sum(c for (k, v), c in gate_tally.items() if v in _BAD_VERDICTS)
    if flagged:
        bullets.append(
            f"The judge flagged <strong>{flagged}</strong> diff(s) as advisory without blocking them — "
            "every one was allowed through, so it added review overhead, not a hard stop."
        )
    if blocked:
        bullets.append(f"The judge hard-blocked <strong>{blocked}</strong> action(s) baseline could not have been stopped from taking.")

    for p in judge.phases:
        if p.profile != "reviewer":
            continue
        verdict = p.field("verdict")
        if verdict:
            bullets.append(
                "The reviewer agent (judge-only) returned: "
                + short(verdict, 220)
            )

    if baseline.pr_url and judge.aborted:
        bullets.append(
            f"<strong>Outcome differs</strong>: baseline shipped a PR (<a href='{html.escape(baseline.pr_url)}' "
            "target='_blank'>opened</a>) while the judge run was aborted before reaching settle — despite "
            "surfacing real findings, it produced no PR to act on them in this run."
        )
    elif baseline.pr_url and judge.pr_url:
        bullets.append("Both sides opened a PR; compare the diffs directly to judge whether the extra judge/reviewer cost bought a better patch.")
    elif judge.aborted and not baseline.aborted:
        bullets.append("The judge run ended aborted with no PR; baseline finished normally.")

    if not bullets:
        bullets.append("Not enough signal in these logs to say more than what the headline metrics show above.")

    return "<ul class='why'>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>"


def parse_summary_table(summary_text: str) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    lines = summary_text.splitlines()
    header_seen = False
    for line in lines:
        if not line.strip():
            continue
        if line.strip() == "A/B result":
            continue
        parts = re.split(r"\s{2,}", line.strip())
        if not header_seen:
            header_seen = True
            continue
        if len(parts) == 3:
            label = parts[0].strip().lower()
            rows[label] = {"baseline": parts[1].strip(), "judge": parts[2].strip()}
        elif len(parts) == 2 and " " in parts[1]:
            # a value overflowed its fixed-width column and swallowed the
            # single space separating it from the next column (e.g. long
            # comma-joined agent-profile lists)
            label = parts[0].strip().lower()
            mid, right = parts[1].split(" ", 1)
            rows[label] = {"baseline": mid.strip(), "judge": right.strip()}
        # otherwise: a row we can't confidently split (e.g. the PR row,
        # whose values can themselves contain spaces) -- skip it, the raw
        # summary.txt is still shown verbatim further down the report.
    return rows


def _num(row: dict[str, str], key: str, side: str) -> float:
    try:
        return float(row.get(key, {}).get(side, "0") or 0)
    except (TypeError, ValueError):
        return 0.0


def render_hero(sides: dict[str, SideFlow], metrics: dict[str, dict[str, str]]) -> str:
    baseline, judge = sides.get("baseline"), sides.get("judge")
    tiles = []

    def tile(label: str, value: str, cls: str = "") -> str:
        return f"<div class='tile'><div class='label'>{html.escape(label)}</div><div class='value {cls}'>{value}</div></div>"

    b_cost = _num(metrics, "cost usd", "baseline")
    j_cost = _num(metrics, "cost usd", "judge")
    if b_cost or j_cost:
        delta = j_cost - b_cost
        cls = "bad" if delta > 0 else "good" if delta < 0 else ""
        tiles.append(tile("cost delta (judge − baseline)", f"{'+' if delta >= 0 else ''}${delta:,.2f}", cls))

    b_tok = _num(metrics, "tokens", "baseline")
    j_tok = _num(metrics, "tokens", "judge")
    if b_tok or j_tok:
        delta = j_tok - b_tok
        cls = "bad" if delta > 0 else "good" if delta < 0 else ""
        tiles.append(tile("token delta", f"{'+' if delta >= 0 else ''}{delta:,.0f}", cls))

    if baseline is not None:
        outcome = "Aborted" if baseline.aborted else ("PR opened" if baseline.pr_url else "No PR")
        cls = "bad" if baseline.aborted else ("good" if baseline.pr_url else "")
        tiles.append(tile("baseline outcome", outcome, cls))
    if judge is not None:
        outcome = "Aborted" if judge.aborted else ("PR opened" if judge.pr_url else "No PR")
        cls = "bad" if judge.aborted else ("good" if judge.pr_url else "")
        tiles.append(tile("judge outcome", outcome, cls))

    trace_note = ""
    if judge is not None and not judge.has_trace:
        trace_note = (
            "<p class='hint'>Per-call request/response and tool-call reasoning weren't captured for this run "
            "(no <span class='mono'>.engine/trace.jsonl</span> found) — set <span class='mono'>ENGINE_TRACE_CALLS=1</span> "
            "and re-run bench_ab.py to get them. bench_ab.py does this automatically as of this report's version.</p>"
        )

    return f"<div class='hero'>{''.join(tiles)}</div>{trace_note}"


def render_metrics_charts(metrics: dict[str, dict[str, str]]) -> str:
    specs: list[tuple[str, str, str]] = [
        ("cost usd", "Cost", "$"),
        ("tokens", "Tokens", ""),
        ("llm requests", "LLM requests", ""),
        ("turns", "Turns", ""),
        ("tool calls", "Tool calls", ""),
        ("elapsed s", "Elapsed", "s"),
    ]
    pairs = []
    for key, label, unit in specs:
        row = metrics.get(key)
        if not row:
            continue
        pairs.append((label, _num(metrics, key, "baseline"), _num(metrics, key, "judge"), unit))
    if not pairs:
        return "<p class='hint'>no summary.txt metrics found.</p>"
    return svg_metric_grid(pairs)


def render_html(workdir: Path, sides: dict[str, SideFlow], summary_text: str, run_log_text: str) -> str:
    repo_m = re.search(r"cloning target (\S+)", run_log_text)
    sha_m = re.search(r"target SHA (\S+)", run_log_text)
    job_m = re.search(r"Grafana job=(\S+)", run_log_text)
    settle_m = re.search(r"settle=(\S+)", run_log_text)
    prompt = ""
    for side in sides.values():
        if side.prompt:
            prompt = side.prompt
            break

    metrics = parse_summary_table(summary_text)
    mermaid = render_comparison_mermaid(sides)
    hero = render_hero(sides, metrics)
    metric_charts = render_metrics_charts(metrics)
    why = render_why(sides, metrics)

    verdict_sections = []
    tool_sections = []
    for name in SIDES:
        side = sides.get(name)
        if side is None:
            continue
        title = "Baseline" if name == "baseline" else "Judge"
        verdict_sections.append(
            f"<div><h3>{title}</h3>{svg_verdict_grid(side.all_judge_calls())}</div>"
        )
        tally: Counter = Counter()
        for phase in side.phases:
            tally.update(phase.tool_tally())
        color = _C_BASE if name == "baseline" else _C_JUDGE
        tool_sections.append(f"<div><h3>{title}</h3>{svg_tool_bars(tally, color)}</div>")

    side_sections = []
    for name in SIDES:
        side = sides.get(name)
        if side is None:
            continue
        title = "Baseline — judge off" if name == "baseline" else "Judge — enforcing"
        side_sections.append(f"""
<h2>{title}</h2>
<div class="sub">session <span class="mono">{html.escape(side.session_id or '—')}</span></div>
{render_narrative(side)}
""")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Bench flow report</title>
<style>{_CSS}</style>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
</head>
<body>
<div class="wrap">
  <h1>A/B bench flow report</h1>
  <div class="sub">workdir <span class="mono">{html.escape(str(workdir))}</span></div>
  {"<div class='sub'>target " + html.escape(repo_m.group(1)) + (" @ " + html.escape(sha_m.group(1)[:12]) if sha_m else "") + "</div>" if repo_m else ""}
  {"<div class='sub'>job " + html.escape(job_m.group(1)) + " · settle=" + html.escape(settle_m.group(1)) + "</div>" if job_m and settle_m else ""}
  <div class="sub">prompt: {html.escape(short(prompt, 180)) or '(not captured)'}</div>

  {hero}

  <h2>Flow comparison</h2>
  <div class="mermaid">{html.escape(mermaid)}</div>

  <h2>Cost / tokens / turns</h2>
  <div class="panel">{metric_charts}</div>

  <h2>Judge verdicts by gate</h2>
  <div class="panel grid2">{''.join(verdict_sections)}</div>

  <h2>Tool calls</h2>
  <div class="panel grid2">{''.join(tool_sections)}</div>

  <h2>Why the outcome differs</h2>
  <div class="panel">{why}</div>

  {''.join(side_sections)}

  <h2>Raw summary</h2>
  <details><summary>summary.txt</summary><pre class="mono" style="white-space:pre-wrap">{html.escape(summary_text)}</pre></details>

  <footer>Generated by scripts/bench_flow.py from {html.escape(str(workdir))}</footer>
</div>
<script>mermaid.initialize({{ startOnLoad: true, theme: "dark", securityLevel: "loose", flowchart: {{ htmlLabels: true }} }});</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workdir", required=True, help="a completed bench_ab.py --workdir")
    parser.add_argument("--out", default="", help="output HTML path (default: <workdir>/flow-report.html)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workdir = Path(args.workdir).expanduser().resolve()
    logs_dir = workdir / "logs"
    if not logs_dir.is_dir():
        print(f"{logs_dir} not found — pass the --workdir a bench_ab.py run wrote to", file=sys.stderr)
        return 2

    sides = {name: build_side(name, workdir) for name in SIDES if (logs_dir / f"client-{name}.log").exists()}
    if not sides:
        print(f"no logs/client-<side>.log found under {logs_dir}", file=sys.stderr)
        return 2

    summary_path = workdir / "summary.txt"
    summary_text = summary_path.read_text() if summary_path.exists() else ""
    run_log_path = logs_dir / "run.log"
    run_log_text = run_log_path.read_text() if run_log_path.exists() else ""

    out_path = Path(args.out).expanduser().resolve() if args.out else workdir / "flow-report.html"
    out_path.write_text(render_html(workdir, sides, summary_text, run_log_text))
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
