"""`llm-hub` command line: run the MCP server, and read/post as the human."""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

from .server import build_server, format_header, format_line, format_post
from .store import HUMAN, POST_TYPES, Hub, HubError

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


def cmd_ls(args: argparse.Namespace) -> None:
    hub = Hub(resolve_root(args.root))
    threads = hub.threads(status=args.status, tag=args.tag)
    for thread in threads:
        mark = " ◀ your turn" if thread.meta.get("awaiting") == HUMAN else ""
        print(format_line(thread, mark))
    if not threads:
        print("No threads.")


def cmd_show(args: argparse.Namespace) -> None:
    hub = Hub(resolve_root(args.root))
    thread, posts = hub.read(HUMAN, args.thread, only_new=args.new)
    print(format_header(thread))
    print("\n## Summary\n\n" + (thread.summary or "_No summary yet._"))
    print("\n## Posts\n")
    print("\n\n".join(format_post(p) for p in posts) or "_No new posts._")


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

    p = sub.add_parser("ls", help="list threads")
    p.add_argument("--status", default="open", choices=["open", "resolved", "all"])
    p.add_argument("--tag")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("show", help="print a thread")
    p.add_argument("thread")
    p.add_argument("--new", action="store_true", help="only posts you haven't seen")
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
