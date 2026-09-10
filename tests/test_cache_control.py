from __future__ import annotations

from llm.openrouter import with_cache_breakpoints


def test_cache_breakpoints_copy_and_mark():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "1", "function": {"name": "search"}}],
        },
        {"role": "tool", "tool_call_id": "1", "content": "result"},
    ]
    tools = [
        {"type": "function", "function": {"name": "search"}},
        {"type": "function", "function": {"name": "read_file"}},
    ]
    out_msgs, out_tools = with_cache_breakpoints(messages, tools)
    assert messages[0]["content"] == "sys"
    assert "cache_control" not in tools[-1]
    assert out_tools[-1]["cache_control"] == {"type": "ephemeral"}
    sys_content = out_msgs[0]["content"]
    assert isinstance(sys_content, list)
    assert sys_content[0]["cache_control"] == {"type": "ephemeral"}
    assert sys_content[0]["text"] == "sys"
    tool_content = out_msgs[-1]["content"]
    assert isinstance(tool_content, list)
    assert tool_content[0]["text"] == "result"
    assert tool_content[0]["cache_control"] == {"type": "ephemeral"}
    assert out_msgs[1]["content"] == "hi"


def test_cache_skips_empty_tool_call_assistant():
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
    ]
    out, _ = with_cache_breakpoints(messages, None)
    assert out[1]["content"][0]["text"] == "q"
    assert out[2].get("cache_control") is None or out[2].get("content") is None
