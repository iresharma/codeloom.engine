from __future__ import annotations

from protocol.codec import decode_command, decode_event, encode
from protocol.commands import RequestSnapshot, StartSession, UndoLastEdit
from protocol.events import FileEdited, SnapshotReady


def test_start_session_round_trip():
    command = StartSession(workspace="/tmp/ws", session_id=None)
    parsed = decode_command(encode(command))
    assert isinstance(parsed, StartSession)
    assert parsed.workspace == "/tmp/ws"
    assert parsed.session_id is None


def test_start_session_with_id():
    parsed = decode_command(
        encode(StartSession(workspace="/tmp/ws", session_id="abc"))
    )
    assert parsed.session_id == "abc"


def test_undo_and_file_edited_round_trip():
    parsed = decode_command(encode(UndoLastEdit()))
    assert isinstance(parsed, UndoLastEdit)
    event = FileEdited(path="a.py", diff="d", tool="str_replace", edit_id="1")
    assert decode_event(encode(event)).path == "a.py"


def test_snapshot_optional_language():
    from protocol.snapshot import EngineSnapshot, GitState

    snap = EngineSnapshot(
        session_id="s",
        workspace="/tmp",
        messages=[],
        ended=False,
        open_files=[],
        file_tree=[],
        git=GitState.empty(),
        language=None,
        message_count=0,
    )
    event = SnapshotReady(snapshot=snap)
    decoded = decode_event(encode(event))
    assert decoded.snapshot.language is None
    assert decoded.snapshot.message_count == 0


def test_history_events_round_trip():
    from protocol.events import ChatHistoryAdded, ChatHistoryComplete

    added = ChatHistoryAdded(
        id="m1",
        role="user",
        text="hi",
        ts="t",
        index=0,
        total=2,
    )
    done = ChatHistoryComplete(count=2)
    decoded_added = decode_event(encode(added))
    decoded_done = decode_event(encode(done))
    assert decoded_added.total == 2
    assert decoded_added.text == "hi"
    assert decoded_done.count == 2


def test_new_session_snapshot_is_instant(tmp_path):
    import asyncio

    from protocol.commands import StartSession
    from protocol.events import ChatHistoryAdded, ChatHistoryComplete, SnapshotReady
    from runtime.session import EngineSession

    async def run():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        queue = session.subscribe()
        await session.handle(StartSession(workspace=str(tmp_path)))
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        snaps = [item for item in events if isinstance(item, SnapshotReady)]
        assert len(snaps) == 1
        assert snaps[0].snapshot.messages == []
        assert snaps[0].snapshot.message_count == 0
        assert not any(isinstance(item, ChatHistoryAdded) for item in events)
        assert any(
            isinstance(item, ChatHistoryComplete) and item.count == 0 for item in events
        )
        assert session._history_task is None

    asyncio.run(run())


def test_snapshot_streams_history_after_ui_bootstrap(tmp_path):
    import asyncio

    from protocol.commands import RequestSnapshot, StartSession
    from protocol.events import (
        ChatHistoryAdded,
        ChatHistoryComplete,
        ChatMessageAdded,
        FileContent,
        SnapshotReady,
    )
    from runtime.session import EngineSession

    async def run():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        queue = session.subscribe()
        await session.handle(StartSession(workspace=str(tmp_path)))
        while not queue.empty():
            queue.get_nowait()
        (tmp_path / "open.py").write_text("x = 1\n", encoding="utf-8")
        session._state.open_files = ["open.py"]
        session._add_message("user", "hello " * 50)
        session._add_message("assistant", "world " * 50)
        while not queue.empty():
            queue.get_nowait()
        await session.handle(RequestSnapshot())
        immediate = []
        while not queue.empty():
            immediate.append(queue.get_nowait())
        snaps = [item for item in immediate if isinstance(item, SnapshotReady)]
        assert len(snaps) == 1
        assert snaps[0].snapshot.messages == []
        assert snaps[0].snapshot.message_count == 2
        assert [type(item) for item in immediate[:2]] == [SnapshotReady, FileContent]
        assert immediate[1].path == "open.py"
        assert not any(isinstance(item, ChatHistoryAdded) for item in immediate)
        assert not any(isinstance(item, ChatHistoryComplete) for item in immediate)
        assert session._history_task is not None
        await session._history_task
        replayed = []
        while not queue.empty():
            replayed.append(queue.get_nowait())
        history = [item for item in replayed if isinstance(item, ChatHistoryAdded)]
        assert [item.role for item in history] == ["user", "assistant"]
        assert [item.index for item in history] == [0, 1]
        assert history[0].total == 2
        assert "hello" in history[0].text
        assert not any(isinstance(item, ChatMessageAdded) for item in replayed)
        assert isinstance(replayed[-1], ChatHistoryComplete)
        assert replayed[-1].count == 2

    asyncio.run(run())
