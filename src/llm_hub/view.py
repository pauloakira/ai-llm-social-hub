"""Readable terminal rendering of threads for the human (`llm-hub show` / `ls`)."""

from __future__ import annotations

from datetime import datetime

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .store import HUMAN, NOBODY, Post, Thread

AUTHOR_STYLES = {"claude": "dark_orange", "gpt": "green3", HUMAN: "deep_sky_blue1"}
FALLBACK_STYLES = ["magenta", "yellow", "cyan", "orchid", "spring_green2"]


def author_style(name: str) -> str:
    if name in AUTHOR_STYLES:
        return AUTHOR_STYLES[name]
    if name == NOBODY:
        return "grey50"
    return FALLBACK_STYLES[sum(map(ord, name)) % len(FALLBACK_STYLES)]


def _name(name: str) -> Text:
    return Text(name, style=f"bold {author_style(name)}")


def _when(ts: str, now: datetime | None = None) -> str:
    """'15:05' today, 'Sep 24 15:05' otherwise."""
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    now = now or datetime.now(dt.tzinfo)
    return dt.strftime("%H:%M") if dt.date() == now.date() else dt.strftime("%b %d %H:%M")


def _ago(ts: str, now: datetime | None = None) -> str:
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    seconds = int(((now or datetime.now(dt.tzinfo)) - dt).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


def _status(thread: Thread) -> Text:
    m = thread.meta
    if m.get("status") != "open":
        return Text(f" {m.get('status')} ", style="bold white on grey35")
    if m.get("awaiting") == HUMAN:
        return Text(" your turn ", style="bold black on deep_sky_blue1")
    return Text(" open ", style="bold black on green3")


def render_header(thread: Thread, repo_url: str | None = None) -> Panel:
    m = thread.meta
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="grey62", justify="right")
    grid.add_column()
    grid.add_row("status", Text.assemble(_status(thread), "  awaiting ", _name(str(m.get("awaiting")))))
    grid.add_row("turns", f"{m.get('turns', 0)}/{m.get('max_turns')}  ·  revision {thread.revision}  ·  {len(thread.posts)} posts")
    grid.add_row(
        "who",
        Text(", ").join(_name(p) for p in m.get("participants") or []),
    )
    if m.get("tags"):
        grid.add_row("tags", Text(" ".join(f"#{t}" for t in m["tags"]), style="cyan"))
    if m.get("related"):
        grid.add_row("related", ", ".join(m["related"]))
    if repo_url:
        grid.add_row("code", Text(repo_url, style="underline"))
    grid.add_row("updated", f"{_when(str(m.get('updated', '')))} ({_ago(str(m.get('updated', '')))})")
    return Panel(grid, title=Text(f"{thread.id} · {thread.title}", style="bold"), title_align="left", border_style="bright_white")


def render_summary(thread: Thread) -> Panel:
    body: RenderableType = Markdown(thread.summary) if thread.summary else Text("No summary yet.", style="italic grey50")
    return Panel(body, title="Summary", title_align="left", border_style="grey50")


def render_post(post: Post) -> Panel:
    title = Text.assemble(
        (post.id, "bold"), "  ", _name(post.author), (" → ", "grey50"), _name(post.hand_to),
        ("  ·  ", "grey50"), (post.type, "italic"),
    )
    if post.re:
        title.append(f"  ·  re {post.re}", style="grey50")
    return Panel(
        Markdown(post.body),
        title=title,
        title_align="left",
        subtitle=Text(_when(post.ts), style="grey50"),
        subtitle_align="right",
        border_style=author_style(post.author),
        padding=(0, 1),
    )


def render_footer(thread: Thread) -> RenderableType:
    m = thread.meta
    if m.get("status") != "open":
        return Text(f"{thread.id} is {m.get('status')}. Post as human to reopen it.", style="grey50")
    if m.get("awaiting") == HUMAN:
        return Text.assemble(
            ("◀ Your turn. ", "bold deep_sky_blue1"),
            (f"llm-hub post {thread.id} --to <claude|gpt> -m \"...\"", "grey62"),
        )
    return Text.assemble("Waiting for ", _name(str(m.get("awaiting"))), ".")


def render_thread(
    thread: Thread, posts: list[Post] | None = None, repo_url: str | None = None, hidden_note: str | None = None
) -> Group:
    posts = thread.posts if posts is None else posts
    parts: list[RenderableType] = [render_header(thread, repo_url), render_summary(thread)]
    if hidden_note:
        parts.append(Rule(hidden_note, style="grey42"))
    parts += [render_post(p) for p in posts] or [Text("No new posts.", style="italic grey50")]
    parts.append(render_footer(thread))
    return Group(*parts)


def render_thread_list(threads: list[Thread]) -> RenderableType:
    if not threads:
        return Text("No threads.", style="italic grey50")
    table = Table(box=None, header_style="bold grey62", pad_edge=False)
    table.add_column("ID", style="bold", no_wrap=True)
    table.add_column("Title", ratio=1)
    table.add_column("Status", no_wrap=True)
    table.add_column("Awaiting", no_wrap=True)
    table.add_column("Posts", justify="right")
    table.add_column("Updated", no_wrap=True, style="grey62")
    for t in threads:
        m = t.meta
        table.add_row(
            t.id, t.title, _status(t), _name(str(m.get("awaiting"))), str(len(t.posts)), _ago(str(m.get("updated", "")))
        )
    return table
