from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import fields
from typing import Any

from protocol.events import (
    EVENTS,
    AgentStateChanged,
    ChatHistoryAdded,
    ChatMessageAdded,
    ChatMessageDelta,
    CommandOutputChunk,
    ContextCompacted,
    ErrorOccurred,
    Event,
    FileContent,
    FileEdited,
    FileTreeUpdated,
    GitStateUpdated,
    ToolCallFinished,
    ToolCallStarted,
    WarningOccurred,
)

CAPACITY = 4096
RECOVERY_MARK = CAPACITY // 4
EVENT_SOFT_LIMIT = 512 * 1024
FLAT_SIZE = 256

# Every unbounded payload field must be listed here. The flat-256 fallback
# is only for structurally short events (ids, enums, flags). Adding a new
# event with a large string and not extending this table is a test failure.
SIZE_FIELDS: dict[type, tuple[str, ...]] = {
    ChatMessageDelta: ("text",),
    ChatMessageAdded: ("text",),
    ChatHistoryAdded: ("text",),
    CommandOutputChunk: ("text",),
    FileContent: ("content",),
    FileEdited: ("diff",),
    FileTreeUpdated: (),
    GitStateUpdated: (),
    ContextCompacted: ("summary",),
    ErrorOccurred: ("message",),
    WarningOccurred: ("message",),
    ToolCallStarted: ("arguments_json",),
    ToolCallFinished: ("preview",),
}

SMALL_STRING_FIELDS = frozenset(
    {
        "id",
        "role",
        "ts",
        "path",
        "tool",
        "edit_id",
        "call_id",
        "name",
        "state",
        "channel",
        "kind",
        "prompt_id",
        "question",
        "default",
        "strategy",
        "stream",
        "reason",
        "session_id",
        "workspace",
    }
)

DROPPABLE = (ChatMessageDelta, CommandOutputChunk)


class Subscriber:
    def __init__(self, capacity: int = CAPACITY, max_bytes: int = 1 << 20) -> None:
        self._items: deque[Event] = deque()
        self._wakeup = asyncio.Event()
        self._capacity = capacity
        self._max_bytes = max_bytes
        self._bytes = 0
        self._overflowed = False
        self.dropped = 0

    def put(self, event: Event) -> None:
        size = approx_size(event)
        droppable = isinstance(event, DROPPABLE)
        if self._over_ceiling(0) and droppable:
            self.dropped += 1
            return
        if self._over_ceiling(size) and not droppable:
            self._evict_droppable(size)
        if self._over_ceiling(size) and droppable:
            self.dropped += 1
            return
        if len(self._items) >= self._capacity and not droppable:
            if not self._evict_droppable(size):
                self._collapse_overflow()
                return
        self._items.append(event)
        self._bytes += size
        self._wakeup.set()

    def _over_ceiling(self, extra: int) -> bool:
        return (
            len(self._items) >= self._capacity
            or (self._bytes + extra) > self._max_bytes
        )

    def _evict_droppable(self, needed: int) -> bool:
        evicted = False
        for index in range(len(self._items) - 1, -1, -1):
            if not self._over_ceiling(needed):
                return True
            item = self._items[index]
            if not isinstance(item, DROPPABLE):
                continue
            del self._items[index]
            self._bytes = max(0, self._bytes - approx_size(item))
            self.dropped += 1
            evicted = True
        return evicted and not self._over_ceiling(needed)

    def _collapse_overflow(self) -> None:
        self.dropped += 1
        notice = ErrorOccurred(
            message=(
                f"event stream overflowed; {self.dropped} events dropped, "
                "send RequestSnapshot to resync"
            )
        )
        if self._overflowed and self._items:
            old = self._items[-1]
            self._bytes = max(0, self._bytes - approx_size(old))
            self._items[-1] = notice
            self._bytes += approx_size(notice)
        elif self._items:
            old = self._items[-1]
            self._bytes = max(0, self._bytes - approx_size(old))
            self._items[-1] = notice
            self._bytes += approx_size(notice)
        else:
            self._items.append(notice)
            self._bytes += approx_size(notice)
        self._overflowed = True
        self._wakeup.set()

    async def get(self) -> Event:
        while True:
            if self._items:
                event = self._items.popleft()
                self._bytes = max(0, self._bytes - approx_size(event))
                self._maybe_note_recovery()
                return event
            self._wakeup.clear()
            if self._items:
                continue
            await self._wakeup.wait()

    def get_nowait(self) -> Event:
        if not self._items:
            raise asyncio.QueueEmpty
        event = self._items.popleft()
        self._bytes = max(0, self._bytes - approx_size(event))
        self._maybe_note_recovery()
        return event

    def empty(self) -> bool:
        return not self._items

    def qsize(self) -> int:
        return len(self._items)

    def _maybe_note_recovery(self) -> None:
        if self.dropped <= 0:
            return
        if len(self._items) > RECOVERY_MARK:
            return
        n = self.dropped
        self.dropped = 0
        self._overflowed = False
        warning = WarningOccurred(
            message=(
                f"dropped {n} streaming events while the client was behind; "
                "full message text was not affected"
            )
        )
        self._items.append(warning)
        self._bytes += approx_size(warning)


def approx_size(event: Event) -> int:
    cls = type(event)
    if cls is FileTreeUpdated:
        return _tree_size(getattr(event, "file_tree", None) or [])
    if cls is GitStateUpdated:
        git = getattr(event, "git", None)
        if git is None:
            return FLAT_SIZE
        return FLAT_SIZE + len(getattr(git, "staged_diff", "") or "") + len(
            getattr(git, "unstaged_diff", "") or ""
        )
    names = SIZE_FIELDS.get(cls)
    if names is None:
        return FLAT_SIZE
    total = FLAT_SIZE
    for name in names:
        total += len(getattr(event, name, None) or "")
    return total


def _tree_size(nodes) -> int:
    total = 0
    for node in nodes:
        total += len(getattr(node, "name", "") or "")
        total += len(getattr(node, "path", "") or "")
        children = getattr(node, "children", None)
        if children:
            total += _tree_size(children)
    return total or FLAT_SIZE


def unbounded_str_fields(cls: type) -> list[str]:
    found: list[str] = []
    try:
        hints = {item.name: item.type for item in fields(cls)}
    except TypeError:
        return found
    for name, annotation in hints.items():
        if name in SMALL_STRING_FIELDS:
            continue
        text = str(annotation)
        if annotation is str or text in {"str", "str | None"}:
            found.append(name)
    return found


def missing_size_fields() -> list[str]:
    missing: list[str] = []
    for name, cls in EVENTS.items():
        listed = SIZE_FIELDS.get(cls)
        for field_name in unbounded_str_fields(cls):
            if listed is None or (listed and field_name not in listed):
                if listed == ():
                    continue
                missing.append(f"{name}.{field_name}")
        if cls is GitStateUpdated:
            continue
        if cls is FileTreeUpdated:
            continue
    return missing


def clip_text(text: str, limit: int = EVENT_SOFT_LIMIT) -> tuple[str, int]:
    if len(text) <= limit:
        return text, 0
    omitted = len(text) - limit
    return text[:limit] + f"\n... (truncated; {omitted} bytes omitted)", omitted
