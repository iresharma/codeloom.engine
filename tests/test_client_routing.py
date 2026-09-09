from __future__ import annotations

from pathlib import Path

from dummy_client import (
    command_from_line,
    drain_notes,
    format_command,
    format_event,
    route_event,
)
from protocol.commands import (
    RequestOrchContext,
    RequestSnapshot,
    StartSession,
    SubmitUserMessage,
)
from protocol.events import (
    AgentStateChanged,
    AgentsUpdated,
    OrchContext,
    ChatHistoryAdded,
    ChatHistoryComplete,
    ChatMessageAdded,
    ChatMessageDelta,
    ChatMessageStarted,
    CommandOutputChunk,
    ErrorOccurred,
    FileClosed,
    FileEdited,
    SnapshotReady,
    StatsUpdated,
    ToolCallFinished,
    ToolCallStarted,
    UserPromptRequested,
    WarningOccurred,
    WorktreeSettled,
)
from protocol.snapshot import AgentRow, EngineSnapshot, GitState, Stats


def test_route_chat_events():
    assert (
        route_event(ChatMessageStarted(id="m1", role="assistant", ts="t")) == "chat"
    )
    assert (
        route_event(ChatMessageDelta(id="m1", channel="text", text="hi")) == "chat"
    )
    assert (
        route_event(ChatMessageAdded(id="m1", role="assistant", text="hi", ts="t"))
        == "chat"
    )
    assert (
        route_event(
            ChatHistoryAdded(
                id="h1",
                role="user",
                text="hello",
                ts="t",
                index=0,
                total=1,
            )
        )
        == "chat"
    )
    assert (
        route_event(
            UserPromptRequested(
                prompt_id="p1",
                question="continue?",
                kind="confirm",
                choices=["yes", "no"],
            )
        )
        == "chat"
    )


def test_route_tool_events():
    assert (
        route_event(
            ToolCallStarted(call_id="c1", name="read_file", arguments_json="{}")
        )
        == "tools"
    )
    assert (
        route_event(
            ToolCallFinished(
                call_id="c1",
                name="read_file",
                preview="ok",
                ok=True,
                duration_ms=12,
            )
        )
        == "tools"
    )
    assert (
        route_event(CommandOutputChunk(call_id="", stream="stdout", text="hi\n"))
        == "tools"
    )


def test_route_protocol_events():
    assert route_event(ChatHistoryComplete(count=3)) == "protocol"
    assert route_event(ErrorOccurred(message="nope")) == "protocol"
    assert route_event(WarningOccurred(message="slow")) == "protocol"
    assert route_event(FileClosed(path="a.py")) == "protocol"
    assert (
        route_event(
            FileEdited(path="a.py", diff="+x", tool="write_file", edit_id="e1")
        )
        == "protocol"
    )
    assert (
        route_event(AgentStateChanged(state="thinking", turn=1, max_turns=8))
        == "protocol"
    )
    snap = EngineSnapshot(
        session_id="s1",
        workspace=".",
        messages=[],
        ended=False,
        open_files=[],
        file_tree=[],
        git=GitState.empty(),
        language="python",
        language_supported=True,
        message_count=0,
        file_tree_count=0,
        stats=Stats(),
    )
    assert route_event(SnapshotReady(snapshot=snap)) == "protocol"
    assert route_event(StatsUpdated(stats=Stats())) == "protocol"
    assert (
        route_event(
            AgentsUpdated(
                agents=[
                    AgentRow(
                        id="a1",
                        role="subagent",
                        profile="ask",
                        status="thinking",
                        task="find retry",
                        batch_id="b1",
                    )
                ]
            )
        )
        == "agents"
    )
    assert route_event(OrchContext(text="--- system ---\n")) == "context"


def test_format_command_compact():
    assert format_command(RequestSnapshot()) == "RequestSnapshot replay=True"
    assert format_command(RequestSnapshot(replay=False)) == "RequestSnapshot replay=False"
    text = format_command(StartSession(workspace="/tmp/proj"))
    assert text.startswith("StartSession")
    assert "workspace=" in text
    long = format_command(SubmitUserMessage(text="x" * 400))
    assert long.startswith("SubmitUserMessage")
    assert "..." in long
    snap = command_from_line("snapshot", Path("."))
    assert isinstance(snap, RequestSnapshot)
    assert snap.replay is False
    assert command_from_line("snap", Path(".")).replay is False
    assert isinstance(command_from_line("context", Path(".")), RequestOrchContext)


def test_help_and_usage_go_to_notes():
    drain_notes()
    assert command_from_line("help", Path(".")) is None
    notes = drain_notes()
    assert notes
    assert "start [id]" in notes[0]
    assert "abort [id]" in notes[0]
    assert "Live agents:" in notes[0]
    assert "snapshot" in notes[0].lower()
    assert "context" in notes[0].lower()
    assert command_from_line("/nope", Path(".")) is None
    notes = drain_notes()
    assert notes and "unknown command" in notes[0]


def test_format_event_delta_returns_text_without_reprint():
    import dummy_client

    dummy_client._STREAM_ID = ""
    assert (
        format_event(ChatMessageDelta(id="m1", channel="text", text="hi")) == "hi"
    )
    assert (
        format_event(ChatMessageAdded(id="m1", role="assistant", text="hi", ts="t"))
        == ""
    )


def test_format_agents_groups_batches_and_tasks():
    text = format_event(
        AgentsUpdated(
            agents=[
                AgentRow(
                    id="aaaaaaaa",
                    role="subagent",
                    profile="coder",
                    status="calling_tool",
                    current_tool="str_replace",
                    task="add a feature flag",
                    batch_id="batchone",
                    batch_name="ship the flag",
                    worktree="/tmp/wt",
                    branch="engine/coder/aaaaaaaa",
                ),
                AgentRow(
                    id="bbbbbbbb",
                    role="subagent",
                    profile="debugger",
                    status="thinking",
                    task="customer escalation",
                    batch_id="batchone",
                    batch_name="ship the flag",
                ),
            ]
        )
    )
    assert "agents: 2 running" in text
    assert "ship the flag" in text
    assert "batchone" in text
    assert "coder aaaaaaaa  calling_tool str_replace" in text
    assert "add a feature flag" in text
    assert "debugger bbbbbbbb  thinking" in text
    assert "customer escalation" in text
    assert "worktree=/tmp/wt" in text
    assert format_event(AgentsUpdated(agents=[])) == "agents: 0 running"


def test_snapshot_lists_live_agents():
    snap = EngineSnapshot(
        session_id="s1",
        workspace=".",
        messages=[],
        ended=False,
        open_files=[],
        file_tree=[],
        git=GitState.empty(),
        language="python",
        language_supported=True,
        message_count=0,
        file_tree_count=0,
        stats=Stats(),
        agents=[
            AgentRow(
                id="cccccccc",
                role="subagent",
                profile="ask",
                status="thinking",
                task="where is retry?",
                batch_id="b2",
            )
        ],
    )
    text = format_event(SnapshotReady(snapshot=snap))
    assert "agents: 1 running" in text
    assert "ask cccccccc  thinking" in text
    assert "where is retry?" in text


def test_format_worktree_settled():
    text = format_event(
        WorktreeSettled(
            agent_id="aaaaaaaa",
            profile="coder",
            action="pr",
            detail="opened pull request",
            branch="engine/coder/aaaaaaaa",
            pr_url="https://example.com/pr/1",
            ok=True,
        )
    )
    assert "worktree pr ok" in text
    assert "https://example.com/pr/1" in text
    assert "opened pull request" in text
