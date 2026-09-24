"""`llm-hub` command line: run the MCP server, and read/post as the human."""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from rich.console import Console

from .server import build_server, format_header, format_line, format_post
from .store import HUMAN, POST_TYPES, Hub, HubError
from .view import render_thread, render_thread_list

CURRENT_FILE = Path(os.environ.get("LLM_HUB_HOME", "~/.llm-hub")).expanduser() / "current"


def resolve_root(explicit: str | None = None) -> Path:
    """--root, then $HUB_ROOT, then the hub picked with `llm-hub use`, then ./llm-hub."""
    if explicit:
        return _hub_dir(Path(explicit))
    if os.environ.get("HUB_ROOT"):
        return _hub_dir(Path(os.environ["HUB_ROOT"]))
    if CURRENT_FILE.exists() and CURRENT_FILE.read_text().strip():
        return Path(CURRENT_FILE.read_text().strip())
    if (Path.cwd() / "llm-hub" / "hub.yaml").exists():
        return Path.cwd() / "llm-hub"
    raise HubError("No hub selected. Run `llm-hub init <repo>` or `llm-hub use <repo>`.")


def _hub_dir(path: Path) -> Path:
    """Accept either the repo or its llm-hub/ directory."""
    path = path.expanduser().resolve()
    return path if (path / "hub.yaml").exists() or path.name == "llm-hub" else path / "llm-hub"


def _body(args: argparse.Namespace) -> str:
    if args.message:
        return args.message
    if not sys.stdin.isatty():
        return sys.stdin.read()
    editor = os.environ.get("EDITOR", "vi")
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as fh:
        fh.write("\n<!-- Write your post above. Lines starting with <!-- are removed. -->\n")
    subprocess.run([editor, fh.name], check=True)
    text = Path(fh.name).read_text()
    Path(fh.name).unlink()
    return "\n".join(line for line in text.splitlines() if not line.startswith("<!--")).strip()


def cmd_init(args: argparse.Namespace) -> None:
    root = _hub_dir(Path(args.repo))
    hub = Hub.init(root, agents=args.agents.split(",") if args.agents else None, max_turns=args.max_turns)
    print(f"Hub ready at {hub.root} (agents: {', '.join(hub.agents)}; max_turns: {hub.max_turns})")
    if not args.no_use:
        cmd_use(argparse.Namespace(repo=str(hub.root)))


def cmd_use(args: argparse.Namespace) -> None:
    root = Hub(_hub_dir(Path(args.repo))).root
    CURRENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CURRENT_FILE.write_text(str(root))
    print(f"Current hub: {root}")


def cmd_where(args: argparse.Namespace) -> None:
    print(resolve_root(args.root))


def cmd_serve(args: argparse.Namespace) -> None:
    if args.thread:
        # Runner mode: pin the hub so `llm-hub use` can't move a running session to another repo.
        if not (args.root or os.environ.get("HUB_ROOT")):
            raise HubError("--thread requires --root (or $HUB_ROOT) so the scope can't drift.")
        root = resolve_root(args.root)
        Hub(root).get(args.thread)  # fail fast if the thread doesn't exist
        server = build_server(args.agent, lambda: root, thread=args.thread)
    else:
        server = build_server(args.agent, lambda: resolve_root(args.root))
    if args.transport == "stdio":
        server.run("stdio")
        return
    from mcp.server.transport_security import TransportSecuritySettings

    token = args.token or os.environ.get("HUB_TOKEN") or secrets.token_urlsafe(24)
    path = f"/{token}/mcp"
    print(f"llm-hub ({args.agent}) listening on http://{args.host}:{args.port}{path}", file=sys.stderr)
    print("Expose it with a tunnel and keep the path secret: it is the only credential.", file=sys.stderr)
    server.run(
        "streamable-http",
        host=args.host,
        port=args.port,
        streamable_http_path=path,
        # The Host header is the tunnel's hostname, so host checks can't apply; the secret path guards access.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )


def _llm_hub_command() -> str:
    """Absolute path the apps should launch: the installed `llm-hub` on PATH, else this executable."""
    found = shutil.which("llm-hub")
    return str(Path(found).absolute()) if found else str(Path(sys.argv[0]).absolute())


def cmd_setup(args: argparse.Namespace) -> None:
    from rich.console import Console
    from rich.table import Table

    from .setup import run_setup, skill_zips

    console = Console()
    if args.zip:
        for path in skill_zips(Path(args.zip).expanduser()):
            console.print(f"built {path}")
        return
    command = args.command or _llm_hub_command()
    steps = run_setup(
        Path.home(), command, shutil.which("claude"), dry_run=args.dry_run, uninstall=args.uninstall
    )
    colors = {"added": "green", "updated": "yellow", "removed": "yellow", "present": "grey62",
              "absent": "grey62", "skipped": "grey50", "blocked": "bold yellow", "failed": "bold red"}
    table = Table(box=None, pad_edge=False, header_style="bold grey62")
    for col in ("Target", "Status", "Where"):
        table.add_column(col)
    for step in steps:
        table.add_row(step.target, f"[{colors.get(step.status, '')}]{step.status}[/]", step.detail)
    verb = "Would change" if args.dry_run else ("Uninstalled" if args.uninstall else "Set up")
    console.print(f"[bold]{verb}[/] llm-hub ([grey62]{command}[/])")
    console.print(table)
    if args.dry_run:
        console.print("\nDry run: nothing was changed. Run without --dry-run to apply.")
    elif not args.uninstall:
        console.print("\n[bold]Next:[/]")
        console.print("1. Restart Claude and ChatGPT so they load the hub server and skills.")
        console.print("2. Create a hub in your project: [bold]llm-hub init <repo>[/]")
        console.print('3. Ask either app: "open a thread with GPT/Claude about …" or "check the hub".')
    if any(step.status == "blocked" for step in steps):
        console.print("\n[bold yellow]Claude desktop wasn't changed[/] because it's running and rewrites its config "
                      "from memory. Quit Claude (Cmd+Q), run [bold]llm-hub setup[/] in Terminal, then reopen Claude. "
                      "Claude Code sessions are already covered by the Claude Code step.")
    if any(step.status == "failed" for step in steps):
        sys.exit(1)


def cmd_ls(args: argparse.Namespace) -> None:
    hub = Hub(resolve_root(args.root))
    threads = hub.threads(status=args.status, tag=args.tag)
    if args.raw:
        for thread in threads:
            mark = " ◀ your turn" if thread.meta.get("awaiting") == HUMAN else ""
            print(format_line(thread, mark))
        if not threads:
            print("No threads.")
        return
    Console().print(render_thread_list(threads))


def _show_once(hub: Hub, args: argparse.Namespace, console: Console) -> Path | None:
    thread, posts = hub.read(HUMAN, args.thread, only_new=args.new)
    hidden = len(thread.posts) - len(posts)
    if args.last and len(posts) > args.last:
        hidden += len(posts) - args.last
        posts = posts[-args.last :]
    if args.raw:
        print(format_header(thread))
        print("\n## Summary\n\n" + (thread.summary or "_No summary yet._"))
        print("\n## Posts\n")
        print("\n\n".join(format_post(p) for p in posts) or "_No new posts._")
    else:
        note = f"{hidden} earlier post{'s' if hidden != 1 else ''} hidden" if hidden else None
        console.print(render_thread(thread, posts, hub.repo_url, hidden_note=note))
    return thread.path


def cmd_show(args: argparse.Namespace) -> None:
    hub = Hub(resolve_root(args.root))
    console = Console()
    if not args.watch:
        _show_once(hub, args, console)
        return
    args.new = False  # a live view always shows the thread, not just what's unseen
    last_mtime = None
    try:
        while True:
            path = hub.get(args.thread).path
            mtime = path.stat().st_mtime if path else None
            if mtime != last_mtime:
                last_mtime = mtime
                console.clear()
                _show_once(hub, args, console)
                console.print(f"[grey50]Watching {args.thread} · refreshed {time.strftime('%H:%M:%S')} · Ctrl+C to stop[/]")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


def cmd_new(args: argparse.Namespace) -> None:
    hub = Hub(resolve_root(args.root))
    tags = args.tags.split(",") if args.tags else []
    thread = hub.create_thread(HUMAN, args.title, _body(args), args.to, args.type, tags)
    print(f"Created {thread.id} · {thread.title} → {args.to}")


def cmd_post(args: argparse.Namespace) -> None:
    hub = Hub(resolve_root(args.root))
    thread, post, notes = hub.reply(HUMAN, args.thread, _body(args), args.to, args.type, args.re)
    print(f"Posted {post.id} on {thread.id} → {post.hand_to}", *notes, sep="\n")


def cmd_resolve(args: argparse.Namespace) -> None:
    hub = Hub(resolve_root(args.root))
    thread = hub.resolve(HUMAN, args.thread, _body(args))
    print(f"{thread.id} resolved.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="llm-hub", description="Threads for multiple LLMs, stored as markdown.")
    parser.add_argument("--root", help="hub directory (or its repo); default: the hub picked with `use`")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create <repo>/llm-hub and make it current")
    p.add_argument("repo", nargs="?", default=".")
    p.add_argument("--agents", help="comma-separated agent names (default: claude,gpt)")
    p.add_argument("--max-turns", type=int)
    p.add_argument("--no-use", action="store_true", help="don't make it the current hub")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("use", help="make a repo's hub the current one for all apps")
    p.add_argument("repo", nargs="?", default=".")
    p.set_defaults(func=cmd_use)

    p = sub.add_parser("where", help="print the current hub directory")
    p.set_defaults(func=cmd_where)

    p = sub.add_parser("serve", help="run the MCP server for one agent")
    p.add_argument("--agent", required=True)
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--token", help="secret URL path segment for http (default: $HUB_TOKEN or random)")
    p.add_argument("--thread", help="runner mode: expose only the tools to take a turn on this thread")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("setup", help="install the skills and register the hub in Claude and ChatGPT/Codex")
    p.add_argument("--dry-run", action="store_true", help="show what would change without changing anything")
    p.add_argument("--uninstall", action="store_true", help="remove the skills and server registrations")
    p.add_argument("--command", help="path the apps should launch (default: llm-hub on PATH)")
    p.add_argument("--zip", metavar="DIR", help="only build uploadable skill zips into DIR")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("ls", help="list threads")
    p.add_argument("--status", default="open", choices=["open", "resolved", "all"])
    p.add_argument("--tag")
    p.add_argument("--raw", action="store_true", help="plain text, one line per thread")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("show", help="show a thread, formatted for the terminal")
    p.add_argument("thread")
    p.add_argument("--new", action="store_true", help="only posts you haven't seen")
    p.add_argument("--last", type=int, metavar="N", help="only the last N posts")
    p.add_argument("-w", "--watch", action="store_true", help="keep open and redraw when the thread changes")
    p.add_argument("--interval", type=float, default=1.0, help=argparse.SUPPRESS)
    p.add_argument("--raw", action="store_true", help="plain markdown instead of formatted output")
    p.set_defaults(func=cmd_show)

    body_help = "post body (default: stdin, or $EDITOR)"
    p = sub.add_parser("new", help="start a thread as the human")
    p.add_argument("title")
    p.add_argument("--to", required=True)
    p.add_argument("--type", default="question", choices=POST_TYPES)
    p.add_argument("--tags")
    p.add_argument("-m", "--message", help=body_help)
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("post", help="reply as the human (also reopens resolved threads)")
    p.add_argument("thread")
    p.add_argument("--to", required=True, help="who goes next")
    p.add_argument("--type", default="note", choices=POST_TYPES)
    p.add_argument("--re")
    p.add_argument("-m", "--message", help=body_help)
    p.set_defaults(func=cmd_post)

    p = sub.add_parser("resolve", help="close a thread with a decision")
    p.add_argument("thread")
    p.add_argument("-m", "--message", help=body_help)
    p.set_defaults(func=cmd_resolve)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except HubError as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
