from __future__ import annotations

from dataclasses import dataclass, field

from llm.provider import LLMProvider, Message


@dataclass
class CompressedTranscript:
    summary: str
    outcome: str
    files_touched: list[str] = field(default_factory=list)
    leftover_questions: list[str] = field(default_factory=list)


class ConversationCompressor:
    def __init__(self, llm: LLMProvider | None = None) -> None:
        self.llm = llm

    async def compress(self, messages: list[Message]) -> CompressedTranscript:
        if self.llm is not None:
            try:
                return await self._llm_compress(messages)
            except Exception:
                pass
        return self._extractive(messages)

    async def _llm_compress(self, messages: list[Message]) -> CompressedTranscript:
        blob = "\n".join(f"{item.role}: {item.content}" for item in messages if item.content)
        prompt = (
            "Compress this subagent transcript into:\n"
            "OUTCOME: one line\n"
            "SUMMARY: a short paragraph\n"
            "FILES: comma-separated paths\n"
            "QUESTIONS: leftover questions, or none\n\n"
            f"{blob[-12000:]}"
        )
        response = await self.llm.complete(
            [Message(role="user", content=prompt)],
            tools=[],
            temperature=0.1,
        )
        return self._parse_llm_text(response.text)

    def _parse_llm_text(self, text: str) -> CompressedTranscript:
        outcome = ""
        summary = text.strip()
        files: list[str] = []
        questions: list[str] = []
        for line in text.splitlines():
            lower = line.lower()
            if lower.startswith("outcome:"):
                outcome = line.split(":", 1)[1].strip()
            elif lower.startswith("summary:"):
                summary = line.split(":", 1)[1].strip()
            elif lower.startswith("files:"):
                files = [part.strip() for part in line.split(":", 1)[1].split(",") if part.strip()]
            elif lower.startswith("questions:"):
                rest = line.split(":", 1)[1].strip()
                if rest and rest.lower() != "none":
                    questions = [rest]
        return CompressedTranscript(
            summary=summary or text.strip(),
            outcome=outcome or "completed",
            files_touched=files,
            leftover_questions=questions,
        )

    def _extractive(self, messages: list[Message]) -> CompressedTranscript:
        assistant = [item.content for item in messages if item.role == "assistant" and item.content]
        files: list[str] = []
        for item in messages:
            if item.role != "tool" or not item.content:
                continue
            for line in item.content.splitlines():
                if line.startswith("wrote ") or line.startswith("edited "):
                    files.append(line.split(" ", 1)[1].strip())
        final = assistant[-1] if assistant else "no assistant output"
        return CompressedTranscript(
            summary=final[:2000],
            outcome="completed" if assistant else "empty",
            files_touched=list(dict.fromkeys(files)),
            leftover_questions=[],
        )
