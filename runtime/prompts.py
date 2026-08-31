from __future__ import annotations

import asyncio
from uuid import uuid4

from protocol.events import UserPromptRequested
from protocol.snapshot import PendingPrompt


class PromptTimeout(TimeoutError):
    pass


class PromptBroker:
    def __init__(self, emit, on_state=None, on_pending=None):
        self._emit = emit
        self._on_state = on_state
        self._on_pending = on_pending
        self._pending: dict[str, asyncio.Future] = {}
        self._current: PendingPrompt | None = None

    @property
    def current(self) -> PendingPrompt | None:
        return self._current

    def pending(self) -> PendingPrompt | None:
        return self._current

    async def ask(
        self,
        question: str,
        *,
        kind: str = "text",
        choices: list[str] | tuple = (),
        default: str | None = None,
        timeout: float = 300.0,
    ) -> str:
        prompt_id = uuid4().hex
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[prompt_id] = future
        record = PendingPrompt(
            prompt_id=prompt_id,
            question=question,
            kind=kind,
            choices=list(choices),
            default=default,
        )
        self._current = record
        if self._on_pending is not None:
            self._on_pending(record)
        if self._on_state is not None:
            self._on_state("waiting_for_user")
        self._emit(
            UserPromptRequested(
                prompt_id=prompt_id,
                question=question,
                kind=kind,
                choices=list(choices),
                default=default,
            )
        )
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            if kind == "confirm":
                return default if default is not None else "no"
            if default is not None:
                return default
            raise PromptTimeout(f"prompt {prompt_id} timed out") from None
        finally:
            self._pending.pop(prompt_id, None)
            if self._current is not None and self._current.prompt_id == prompt_id:
                self._current = None
            if self._on_pending is not None:
                self._on_pending(self._current)

    def answer(self, prompt_id: str, text: str) -> bool:
        future = self._pending.get(prompt_id)
        if future is None or future.done():
            return False
        future.set_result(text)
        return True

    def cancel_all(self) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.cancel()
        self._pending.clear()
        self._current = None
