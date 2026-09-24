import os
import sys

import anyio
import pytest
from mcp import Client, ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from llm_hub.server import build_server
from llm_hub.store import Hub


def text(result) -> str:
    return "\n".join(c.text for c in result.content)


@pytest.fixture
def root(tmp_path):
    return Hub.init(tmp_path / "llm-hub").root


def test_two_agents_converse_through_tools(root):
    async def run():
        async with Client(build_server("claude", lambda: root)) as claude, \
                Client(build_server("gpt", lambda: root)) as gpt:
            await converse(claude, gpt)

    async def converse(claude, gpt):
        out = text(await claude.call_tool("create_thread", {"title": "Cache design", "body": "LRU?", "to": "gpt"}))
        assert "T-0001" in out
        assert "T-0001" in text(await gpt.call_tool("inbox", {}))
        assert "LRU?" in text(await gpt.call_tool("read_thread", {"thread_id": "1"}))

        bad = await claude.call_tool("reply", {"thread_id": "T-0001", "body": "hm", "hand_to": "gpt",
                                               "expected_revision": 1})
        assert bad.is_error and "gpt's turn" in text(bad)

        out = text(await gpt.call_tool("reply", {"thread_id": "T-0001", "body": "LFU", "hand_to": "claude",
                                                  "type": "critique", "re": "P-001", "expected_revision": 1}))
        assert "P-002" in out and "revision 2" in out
        read = text(await claude.call_tool("read_thread", {"thread_id": "T-0001"}))
        assert "LFU" in read and "LRU?" not in read and "expected_revision=2" in read

        stale = await claude.call_tool("resolve", {"thread_id": "T-0001", "decision": "LFU", "expected_revision": 1})
        assert stale.is_error and "changed since you read it" in text(stale)

        out = text(await claude.call_tool("update_summary", {"thread_id": "T-0001", "summary": "- **Agreed**: LFU",
                                                             "expected_revision": 2}))
        assert "revision 3" in out
        out = await claude.call_tool("resolve", {"thread_id": "T-0001", "decision": "LFU", "expected_revision": 3})
        assert "resolved" in text(out)
        assert "T-0001" in text(await gpt.call_tool("list_threads", {"status": "resolved"}))

    anyio.run(run)


def test_tools_are_annotated_for_chatgpt_confirmation(root):
    server = build_server("gpt", lambda: root)

    async def run():
        async with Client(server) as client:
            return (await client.list_tools()).tools

    tools = {t.name: t for t in anyio.run(run)}
    assert tools["inbox"].annotations.read_only_hint is True
    assert tools["read_thread"].annotations.read_only_hint is True
    assert tools["reply"].annotations.read_only_hint is False


def test_stdio_server_end_to_end(root):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "llm_hub.cli", "--root", str(root), "serve", "--agent", "gpt"],
        env={**os.environ},
    )

    async def run():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {t.name for t in (await session.list_tools()).tools}
                assert {"inbox", "read_thread", "reply", "create_thread", "resolve"} <= names
                result = await session.call_tool("inbox", {})
                assert "You are **gpt**" in text(result)
                prompt = await session.get_prompt("hub_rules", {})
                assert "You are **gpt**" in prompt.messages[0].content.text

    anyio.run(run)


def test_post_type_schema_matches_store():
    import typing

    from llm_hub.server import PostType
    from llm_hub.store import POST_TYPES

    assert typing.get_args(PostType) == POST_TYPES
