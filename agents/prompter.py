"""Pre-turn prompter: clarify a vague user message into a BRIEF with Done.

Not a discover_profiles profile — those become orchestrator tools. This runs
once in EngineSession._run_turn before Orchestrator.run, and only when
ENGINE_INTERVIEW=on and classify_brief (or the cheap-model fallback) says so.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from uuid import uuid4

from agents.agent_loop import AgentLoop
from agents.hooks import AgentHooks
from agents.profile import EXTRACTOR_MODEL
from llm.provider import Usage
from runtime.config import EngineConfig
from runtime.judge_decisions import (
    brief_questions,
    brief_signals,
    classify_brief,
)
from runtime.prompts import PromptTimeout
from tools.base import Tool
from tools.registry import ToolRegistry

PROMPTER_SYSTEM = """You clarify the user's request before the orchestrator plans.

You have one tool: ask_user. Ask at most three questions. Each question must
change the implementation plan — skip polish, preferences that do not matter,
and anything you can state as an assumption.

If the user says "just do it", "no more questions", or similar, stop asking.
State remaining gaps as assumptions and emit the brief.

Your final reply MUST contain a BRIEF block whose last line is Done: ... —

BRIEF
Goal: ...
Assumptions: ...
Done: <observable completion condition>

Done must be something an engineer can check (behavior, test, file, or
user-visible result). Do not call ask_user after you have enough to write
the brief.
"""

BRIEF_TRANSCRIPT_PREFIX = "Completed brief:\n"
MAX_PROMPTER_QUESTIONS = 3
_END_INTERVIEW = frozenset(
    {
        "just do it",
        "just do it.",
        "no more questions",
        "no more questions.",
        "no questions",
        "stop asking",
        "stop asking.",
    }
)


def _normalize_answer(text: str) -> str:
    return " ".join(str(text).lower().split()).rstrip(".!")


def wants_end_interview(answer: str) -> bool:
    normalized = _normalize_answer(answer)
    if normalized in _END_INTERVIEW:
        return True
    return normalized.startswith("just do it") or normalized.startswith(
        "no more questions"
    )


def extract_brief(text: str) -> str | None:
    """Return the BRIEF…Done: block, or None if missing / malformed."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    done_idx = None
    for index in range(len(lines) - 1, -1, -1):
        if re.match(r"(?i)^Done:\s+\S", lines[index].strip()):
            done_idx = index
            break
    if done_idx is None:
        return None
    brief_idx = None
    for index in range(done_idx, -1, -1):
        stripped = lines[index].strip().strip("`").rstrip(":").strip()
        if stripped.upper() == "BRIEF":
            brief_idx = index
            break
    if brief_idx is None:
        return None
    block = "\n".join(lines[brief_idx : done_idx + 1]).strip()
    last = block.split("\n")[-1].strip()
    if not re.match(r"(?i)^Done:\s+\S", last):
        return None
    return block


def _parse_interview_json(text: str) -> bool | None:
    raw = (text or "").strip()
    if not raw:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL | re.IGNORECASE)
    if fence:
        raw = fence.group(1)
    else:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start : end + 1]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "interview" not in data:
        return None
    return bool(data["interview"])


class Prompter(AgentLoop):
    """Short AgentLoop with one tool. Not registered by discover_profiles()."""

    def __init__(self, llm, *, ask_user, config, hooks=None, model=None, agent_id=""):
        registry = ToolRegistry()
        registry.register(_ask_user_tool(self))
        base = config
        if base is None:
            base = getattr(llm, "_config", None) or EngineConfig()
        child_config = replace(
            base,
            max_turns=4,
            max_continues=0,
            turn_continue="never",
            max_tool_calls=8,
        )
        super().__init__(
            llm,
            tools=registry,
            system_prompt=PROMPTER_SYSTEM,
            config=child_config,
            hooks=hooks or AgentHooks(),
            ask_user=ask_user,
            agent_id=agent_id,
            role="prompter",
            profile="prompter",
            model=model,
            freeze_system=True,
            concurrent_tools=False,
            write_globs=[],
        )
        self._question_count = 0
        self._timed_out = False

    async def _offer_continue(self, continues: int) -> str:
        return "handoff"


def _ask_user_tool(prompter: Prompter) -> Tool:
    async def ask_user(ctx, question: str) -> str:
        if prompter._question_count >= MAX_PROMPTER_QUESTIONS:
            return (
                "error: question limit reached (3). State remaining gaps as "
                "assumptions and emit the BRIEF block now."
            )
        ask = getattr(ctx, "ask_user", None)
        if ask is None:
            return "error: no prompt broker"
        prompter._question_count += 1
        try:
            answer = await ask(
                question,
                kind="interview",
                agent_id=ctx.agent_id or prompter.agent_id,
                profile="prompter",
            )
        except PromptTimeout:
            prompter._timed_out = True
            return (
                "error: user did not answer (timed out). Stop now; do not "
                "invent a brief."
            )
        text = str(answer or "")
        if wants_end_interview(text):
            return (
                f"{text}\n(User wants no more questions. State remaining gaps "
                "as assumptions and emit the BRIEF block now.)"
            )
        return text

    return Tool(
        name="ask_user",
        description=(
            "Ask the user one clarifying question that would change the plan. "
            "At most three questions total."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "One concrete question for the user.",
                }
            },
            "required": ["question"],
        },
        fn=ask_user,
    )


async def model_says_interview(llm, text: str, *, model_cheap: str = "") -> bool | None:
    """Cheap-model fallback when judge.ask returns None. None = failed."""
    if llm is None:
        return None
    model = (model_cheap or "").strip() or EXTRACTOR_MODEL
    prompt = [
        {
            "role": "system",
            "content": (
                "Decide whether this user message needs a short clarifying "
                "interview before an engineer plans the work. Interview only "
                "when it is not conversational and either a done condition is "
                "missing or a plan-changing gap is likely. Reply with JSON "
                'only: {"interview": true} or {"interview": false}.'
            ),
        },
        {"role": "user", "content": text},
    ]
    try:
        result = await llm.complete(prompt, model=model, temperature=0)
    except Exception:  # noqa: BLE001 — fallback must never break the turn
        return None
    return _parse_interview_json(getattr(result, "text", "") or "")


async def needs_interview(
    text: str,
    *,
    judge,
    config,
    llm,
    on_judgement=None,
) -> bool:
    """True when the prompter should run. Judge first; model only if None."""
    verdict = None
    if judge is not None:
        verdict = await judge.ask(
            {"text": text},
            brief_questions(),
            tag="brief",
        )
    if verdict is not None:
        interview = classify_brief(verdict)
        if on_judgement is not None:
            on_judgement(
                tag="brief",
                subject=(text or "")[:200],
                outcome="interview" if interview else "pass",
                signals=brief_signals(verdict),
                # ENGINE_INTERVIEW is the act switch; always enforced when on.
                enforced=True,
                latency_ms=verdict.latency_ms,
                agent_id="",
            )
        return interview
    fallback = await model_says_interview(
        llm, text, model_cheap=getattr(config, "model_cheap", "") or ""
    )
    if fallback is None:
        return False
    return fallback


async def run_prompter(
    *,
    llm,
    text: str,
    ask_user,
    config,
    hooks: AgentHooks | None = None,
    make_hooks=None,
    model: str | None = None,
    on_started=None,
    on_finished=None,
) -> str | None:
    """Run the prompter. Returns the BRIEF block, or None to keep original text."""
    agent_id = uuid4().hex
    if on_started is not None:
        on_started(agent_id, "prompter", "", text[:200])
    if make_hooks is not None:
        hooks = make_hooks(agent_id)
    status = "ok"
    summary = ""
    usage = Usage()
    brief = None
    try:
        agent = Prompter(
            llm,
            ask_user=ask_user,
            config=config,
            hooks=hooks,
            model=model,
            agent_id=agent_id,
        )
        reply = await agent.run(text)
        usage = agent._usage
        if agent._timed_out:
            status = "timeout"
            summary = "prompt timed out"
            brief = None
        else:
            brief = extract_brief(reply)
            if brief is None:
                status = "incomplete"
                summary = "no BRIEF block"
            else:
                summary = brief.split("\n")[-1][:200]
    except PromptTimeout:
        status = "timeout"
        summary = "prompt timed out"
        brief = None
    except Exception as exc:  # noqa: BLE001
        status = "failed"
        summary = f"error: {exc}"
        brief = None
    finally:
        if on_finished is not None:
            on_finished(agent_id, "prompter", status, summary, usage=usage)
    return brief
