from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ChatMessage:
    id: str
    role: str
    text: str
    ts: str

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "text": self.text,
            "ts": self.ts,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ChatMessage:
        return cls(
            id=data["id"],
            role=data["role"],
            text=data["text"],
            ts=data["ts"],
        )


@dataclass
class SessionSummary:
    id: str
    saved_at: str
    message_count: int
    open_files: list[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "saved_at": self.saved_at,
            "message_count": self.message_count,
            "open_files": list(self.open_files),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SessionSummary:
        return cls(
            id=data["id"],
            saved_at=data["saved_at"],
            message_count=int(data["message_count"]),
            open_files=list(data.get("open_files") or []),
        )


@dataclass
class FileTreeNode:
    name: str
    path: str
    is_dir: bool
    children: list[FileTreeNode] | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "path": self.path,
            "is_dir": self.is_dir,
        }
        if self.children is not None:
            payload["children"] = [child.to_json() for child in self.children]
        return payload

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> FileTreeNode:
        raw_children = data.get("children")
        children = None
        if raw_children is not None:
            children = [
                item if isinstance(item, FileTreeNode) else FileTreeNode.from_json(item)
                for item in raw_children
            ]
        return cls(
            name=data["name"],
            path=data["path"],
            is_dir=bool(data["is_dir"]),
            children=children,
        )


@dataclass
class GitState:
    branch: str | None
    dirty: bool
    staged: list[str]
    unstaged: list[str]
    untracked: list[str]
    staged_diff: str
    unstaged_diff: str

    def to_json(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "dirty": self.dirty,
            "staged": list(self.staged),
            "unstaged": list(self.unstaged),
            "untracked": list(self.untracked),
            "staged_diff": self.staged_diff,
            "unstaged_diff": self.unstaged_diff,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> GitState:
        if not data:
            return cls.empty()
        return cls(
            branch=data.get("branch"),
            dirty=bool(data.get("dirty", False)),
            staged=list(data.get("staged") or []),
            unstaged=list(data.get("unstaged") or []),
            untracked=list(data.get("untracked") or []),
            staged_diff=data.get("staged_diff") or "",
            unstaged_diff=data.get("unstaged_diff") or "",
        )

    @classmethod
    def empty(cls) -> GitState:
        return cls(
            branch=None,
            dirty=False,
            staged=[],
            unstaged=[],
            untracked=[],
            staged_diff="",
            unstaged_diff="",
        )


@dataclass
class Stats:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    cost: float = 0.0
    requests: int = 0
    tool_calls: int = 0
    turns: int = 0
    elapsed_s: float = 0.0
    last_turn_tokens: int = 0
    last_turn_cost: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_tokens": self.cached_tokens,
            "total_tokens": self.total_tokens,
            "cost": self.cost,
            "requests": self.requests,
            "tool_calls": self.tool_calls,
            "turns": self.turns,
            "elapsed_s": self.elapsed_s,
            "last_turn_tokens": self.last_turn_tokens,
            "last_turn_cost": self.last_turn_cost,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> Stats:
        if not data:
            return cls()
        return cls(
            prompt_tokens=int(data.get("prompt_tokens") or 0),
            completion_tokens=int(data.get("completion_tokens") or 0),
            reasoning_tokens=int(data.get("reasoning_tokens") or 0),
            cached_tokens=int(data.get("cached_tokens") or 0),
            total_tokens=int(data.get("total_tokens") or 0),
            cost=float(data.get("cost") or 0),
            requests=int(data.get("requests") or 0),
            tool_calls=int(data.get("tool_calls") or 0),
            turns=int(data.get("turns") or 0),
            elapsed_s=float(data.get("elapsed_s") or 0),
            last_turn_tokens=int(data.get("last_turn_tokens") or 0),
            last_turn_cost=float(data.get("last_turn_cost") or 0),
        )


@dataclass
class PendingPrompt:
    prompt_id: str
    question: str
    kind: str
    choices: list[str]
    default: str | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "prompt_id": self.prompt_id,
            "question": self.question,
            "kind": self.kind,
            "choices": list(self.choices),
        }
        if self.default is not None:
            payload["default"] = self.default
        return payload

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> PendingPrompt | None:
        if not data:
            return None
        return cls(
            prompt_id=data["prompt_id"],
            question=data["question"],
            kind=data.get("kind") or "text",
            choices=list(data.get("choices") or []),
            default=data.get("default"),
        )


@dataclass
class EngineSnapshot:
    session_id: str
    workspace: str
    messages: list[ChatMessage]
    ended: bool
    open_files: list[str]
    file_tree: list[FileTreeNode]
    git: GitState
    language: str | None = None
    language_supported: bool = False
    message_count: int = 0
    file_tree_count: int = 0
    stats: Stats | None = None
    pending_prompt: PendingPrompt | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "EngineSnapshot",
            "session_id": self.session_id,
            "workspace": self.workspace,
            "messages": [message.to_json() for message in self.messages],
            "ended": self.ended,
            "open_files": list(self.open_files),
            "file_tree": [node.to_json() for node in self.file_tree],
            "git": self.git.to_json(),
            "language": self.language,
            "language_supported": self.language_supported,
            "message_count": self.message_count,
            "file_tree_count": self.file_tree_count,
            "stats": (self.stats or Stats()).to_json(),
        }
        if self.pending_prompt is not None:
            payload["pending_prompt"] = self.pending_prompt.to_json()
        return payload

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> EngineSnapshot:
        messages = [
            item if isinstance(item, ChatMessage) else ChatMessage.from_json(item)
            for item in data.get("messages", [])
        ]
        return cls(
            session_id=data["session_id"],
            workspace=data["workspace"],
            messages=messages,
            ended=bool(data.get("ended", False)),
            open_files=list(data.get("open_files") or []),
            file_tree=[
                item if isinstance(item, FileTreeNode) else FileTreeNode.from_json(item)
                for item in data.get("file_tree") or []
            ],
            git=GitState.from_json(data.get("git")),
            language=data.get("language"),
            language_supported=bool(data.get("language_supported", False)),
            message_count=int(data.get("message_count", len(messages))),
            file_tree_count=int(data.get("file_tree_count") or 0),
            stats=Stats.from_json(data.get("stats")),
            pending_prompt=PendingPrompt.from_json(data.get("pending_prompt")),
        )
