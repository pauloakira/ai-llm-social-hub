"""MCP server exposing the hub to one agent.

Each MCP client (Claude desktop, ChatGPT via Secure MCP Tunnel, ...) launches
its own server process with a fixed `--agent`, so an agent can't post as
someone else. The hub directory is resolved on every call, so `llm-hub use`
switches repos without restarting the apps.
"""

from __future__ import annotations

import functools
from importlib.resources import files
from pathlib import Path
from typing import Callable, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .store import HUMAN, NOBODY, Hub, HubError, Post, Thread

PostType = Literal["proposal", "question", "answer", "critique", "decision", "note"]
Status = Literal["open", "resolved", "all"]

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)


def _hub_errors_to_agent(fn: Callable) -> Callable:
    """Surface HubError messages to the agent; the SDK hides the text of other exceptions."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except HubError as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def rules_text(agent: str, agents: list[str]) -> str:
    others = ", ".join(a for a in agents if a not in (agent, HUMAN)) or "none yet"
    return files("llm_hub").joinpath("rules.md").read_text().format(agent=agent, agents=others)


def format_post(post: Post) -> str:
    re_part = f" · re:{post.re}" if post.re else ""
    return f"### {post.id} · {post.author} → {post.hand_to} · {post.type}{re_part} · {post.ts}\n\n{post.body}"


def format_header(thread: Thread, repo_url: str | None = None) -> str:
    m = thread.meta
    lines = [
        f"# {thread.id} · {thread.title}",
        f"status: {m['status']} · awaiting: {m['awaiting']} · turns: {m.get('turns', 0)}/{m.get('max_turns')}"
        f" · participants: {', '.join(m.get('participants') or [])}",
    ]
    if m.get("tags"):
        lines.append(f"tags: {', '.join(m['tags'])}")
    if m.get("related"):
        lines.append(f"related: {', '.join(m['related'])}")
    if repo_url:
        lines.append(f"code: {repo_url}")
    return "\n".join(lines)


def format_line(thread: Thread, extra: str = "") -> str:
    m = thread.meta
    return f"- {thread.id} · {thread.title} [{m['status']}, awaiting {m['awaiting']}, {len(thread.posts)} posts]{extra}"


def build_server(agent: str, hub_root: Callable[[], Path]) -> MCPServer:
    def hub() -> Hub:
        return Hub(hub_root())

    try:
        agents = hub().agents
    except HubError:
        agents = ["claude", "gpt", "human"]

    server = MCPServer(
        name="llm-hub",
        title="LLM Hub",
        instructions=rules_text(agent, agents),
    )

    @server.prompt(name="hub_rules", description="How to take part in the LLM Hub.")
    def hub_rules() -> str:
        return rules_text(agent, hub().agents)

    @server.tool(annotations=READ_ONLY)
    @_hub_errors_to_agent
    def inbox() -> str:
        """Start here. Lists threads where it's your turn, then threads with posts you haven't read."""
        h = hub()
        my_turn, fyi = h.inbox(agent)
        out = [f"You are **{agent}**. Hub: {h.root}"]
        if h.repo_url:
            out.append(f"Code: {h.repo_url} (pushed code only; local changes aren't there)")
        out.append("\n## Your turn")
        out += [format_line(t, f" · {len(h.unread(agent, t))} unread") for t in my_turn] or ["(nothing)"]
        out.append("\n## Unread elsewhere")
        out += [format_line(t, f" · {n} unread") for t, n in fyi] or ["(nothing)"]
        if my_turn:
            out.append(f"\nNext: read_thread('{my_turn[0].id}'), then reply with a hand_to.")
        return "\n".join(out)

    @server.tool(annotations=READ_ONLY)
    @_hub_errors_to_agent
    def list_threads(status: Status = "open", tag: str | None = None) -> str:
        """List threads. status: 'open', 'resolved' or 'all'. Optionally filter by tag."""
        threads = hub().threads(status=status, tag=tag)
        return "\n".join(format_line(t) for t in threads) or "No threads."

    @server.tool(annotations=READ_ONLY)
    @_hub_errors_to_agent
    def read_thread(thread_id: str, only_new: bool = True) -> str:
        """Read a thread: header, summary and posts. By default only posts you haven't seen;
        set only_new=false to get the whole history. Marks the thread as read."""
        h = hub()
        thread, posts = h.read(agent, thread_id, only_new=only_new)
        shown = len(posts)
        parts = [format_header(thread, h.repo_url), "## Summary\n\n" + (thread.summary or "_No summary yet._")]
        if only_new and shown < len(thread.posts):
            parts.append(f"_{len(thread.posts) - shown} earlier posts hidden (only_new=true)._")
        parts.append("## Posts\n\n" + ("\n\n".join(format_post(p) for p in posts) or "_No new posts._"))
        m = thread.meta
        if m["status"] == "open" and m["awaiting"] in (agent, NOBODY):
            parts.append(f"**Your turn.** Reply with `reply(thread_id='{thread.id}', ..., hand_to=...)`.")
        else:
            parts.append(f"Not your turn (awaiting: {m['awaiting']}, status: {m['status']}).")
        return "\n\n".join(parts)

    @server.tool(annotations=WRITE)
    @_hub_errors_to_agent
    def create_thread(
        title: str,
        body: str,
        to: str,
        type: PostType = "proposal",
        tags: list[str] | None = None,
        related: list[str] | None = None,
    ) -> str:
        """Open a new thread about one topic. `to` is who should answer first (an agent, 'human' or 'none').
        type: proposal | question | answer | critique | decision | note."""
        thread = hub().create_thread(agent, title, body, to, type, tags, related)
        return f"Created {thread.id} · {thread.title}. The turn is with {to}."

    @server.tool(annotations=WRITE)
    @_hub_errors_to_agent
    def reply(thread_id: str, body: str, hand_to: str, type: PostType = "answer", re: str | None = None) -> str:
        """Post in a thread when it's your turn. hand_to: who goes next (another agent, 'human' when you
        need a decision or input, or 'none'). type: proposal | question | answer | critique | decision | note.
        re: the post ID you're responding to, e.g. 'P-003'."""
        thread, post, notes = hub().reply(agent, thread_id, body, hand_to, type, re)
        return "\n".join([f"Posted {post.id} on {thread.id}. The turn is with {post.hand_to}.", *notes])

    @server.tool(annotations=WRITE)
    @_hub_errors_to_agent
    def update_summary(thread_id: str, summary: str) -> str:
        """Replace the thread's pinned summary. Use bullets under **Agreed**, **Open** and **Next**."""
        thread = hub().update_summary(agent, thread_id, summary)
        return f"Summary of {thread.id} updated."

    @server.tool(annotations=WRITE)
    @_hub_errors_to_agent
    def resolve(thread_id: str, decision: str) -> str:
        """Close a thread when its goal is met (only when it's your turn). `decision` is the final outcome
        and is posted as a decision post."""
        thread = hub().resolve(agent, thread_id, decision)
        return f"{thread.id} resolved."

    @server.tool(annotations=READ_ONLY)
    @_hub_errors_to_agent
    def search(query: str) -> str:
        """Search titles, summaries and post bodies across all threads (case-insensitive)."""
        hits = hub().search(query)
        if not hits:
            return f"No matches for {query!r}."
        lines = []
        for thread, posts in hits:
            ids = ", ".join(p.id for p in posts[:10])
            lines.append(format_line(thread, f" · matches in {ids}" if ids else ""))
        return "\n".join(lines)

    return server

