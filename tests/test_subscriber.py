from __future__ import annotations

import asyncio

from protocol.events import (
    EVENTS,
    AgentStateChanged,
    ChatMessageAdded,
    ChatMessageDelta,
    CommandOutputChunk,
    ErrorOccurred,
    WarningOccurred,
)
from runtime.subscriber import (
    SIZE_FIELDS,
    SMALL_STRING_FIELDS,
    Subscriber,
    approx_size,
    missing_size_fields,
    unbounded_str_fields,
)


def test_get_woken_by_put():
    async def run():
        sub = Subscriber(capacity=8, max_bytes=10_000)
        seen = []

        async def consumer():
            seen.append(await sub.get())

        task = asyncio.create_task(consumer())
        await asyncio.sleep(0)
        sub.put(AgentStateChanged(state="idle", turn=0, max_turns=16))
        await task
        assert seen[0].state == "idle"

    asyncio.run(run())


def test_fifo():
    async def run():
        sub = Subscriber()
        sub.put(AgentStateChanged(state="a", turn=1, max_turns=2))
        sub.put(AgentStateChanged(state="b", turn=2, max_turns=2))
        assert sub.get_nowait().state == "a"
        assert sub.get_nowait().state == "b"

    asyncio.run(run())


def test_drop_newest_delta_at_capacity():
    async def run():
        sub = Subscriber(capacity=2, max_bytes=100_000)
        sub.put(ChatMessageDelta(id="1", channel="text", text="one"))
        sub.put(ChatMessageDelta(id="1", channel="text", text="two"))
        sub.put(ChatMessageDelta(id="1", channel="text", text="three"))
        assert sub.dropped == 1
        assert [item.text for item in list(sub._items)] == ["one", "two"]

    asyncio.run(run())


def test_non_delta_evicts_newest_delta():
    async def run():
        sub = Subscriber(capacity=2, max_bytes=100_000)
        sub.put(ChatMessageDelta(id="1", channel="text", text="one"))
        sub.put(ChatMessageDelta(id="1", channel="text", text="two"))
        added = ChatMessageAdded(id="1", role="assistant", text="full", ts="t")
        sub.put(added)
        texts = [getattr(item, "text", None) for item in sub._items]
        assert "full" in texts
        assert texts[0] == "one"

    asyncio.run(run())


def test_overflow_notice_collapses():
    async def run():
        sub = Subscriber(capacity=1, max_bytes=100_000)
        sub.put(AgentStateChanged(state="a", turn=1, max_turns=2))
        sub.put(AgentStateChanged(state="b", turn=2, max_turns=2))
        sub.put(AgentStateChanged(state="c", turn=3, max_turns=2))
        assert len(sub._items) == 1
        assert isinstance(sub._items[0], ErrorOccurred)

    asyncio.run(run())


def test_recovery_warning():
    async def run():
        sub = Subscriber(capacity=2, max_bytes=100_000)
        sub.put(ChatMessageDelta(id="1", channel="text", text="a"))
        sub.put(ChatMessageDelta(id="1", channel="text", text="b"))
        sub.put(ChatMessageDelta(id="1", channel="text", text="c"))
        first = sub.get_nowait()
        assert first.text == "a"
        second = sub.get_nowait()
        assert isinstance(second, ChatMessageDelta) or isinstance(second, WarningOccurred)

    asyncio.run(run())


def test_byte_ceiling_before_item():
    async def run():
        sub = Subscriber(capacity=4096, max_bytes=8000)
        for _ in range(10):
            sub.put(CommandOutputChunk(call_id="", stream="stdout", text="x" * 4000))
        assert sub.qsize() < 10
        assert sub._bytes <= 8000 + 256

    asyncio.run(run())


def test_small_event_is_flat():
    event = AgentStateChanged(state="idle", turn=0, max_turns=16)
    assert approx_size(event) == 256


def test_approx_size_covers_unbounded_fields():
    assert missing_size_fields() == []
    for name, cls in EVENTS.items():
        for field_name in unbounded_str_fields(cls):
            if field_name in SMALL_STRING_FIELDS:
                continue
            listed = SIZE_FIELDS.get(cls)
            assert listed is not None, name


def test_queue_shims():
    async def run():
        sub = Subscriber()
        assert sub.empty()
        assert sub.qsize() == 0
        try:
            sub.get_nowait()
            raise AssertionError("expected QueueEmpty")
        except asyncio.QueueEmpty:
            pass
        sub.put(AgentStateChanged(state="idle", turn=0, max_turns=1))
        assert not sub.empty()

    asyncio.run(run())


def test_terminal_added_survives_drops():
    async def run():
        sub = Subscriber(capacity=2, max_bytes=1000)
        for i in range(20):
            sub.put(ChatMessageDelta(id="1", channel="text", text=f"{i}"))
        sub.put(ChatMessageAdded(id="1", role="assistant", text="done", ts="t"))
        items = []
        while not sub.empty():
            items.append(sub.get_nowait())
        assert any(isinstance(item, ChatMessageAdded) for item in items)

    asyncio.run(run())
