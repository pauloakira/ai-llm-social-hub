"""HTTP API and static files for the LLM Hub app.

The app posts as `human` through `Hub`, so it follows the same rules as the CLI. It can post as you and change app
configs, so although it only listens on 127.0.0.1 every API call is guarded:
- a secret token, handed to the browser once through /open and kept in a SameSite=Strict, HttpOnly cookie;
- the Host header must be localhost (stops DNS rebinding);
- a cross-site Origin is refused (stops other web pages posting through the browser).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ..store import HUMAN, NOBODY, POST_TYPES, Hub, HubError, Post, Thread

COOKIE = "llm_hub_session"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


@dataclass
class AppConfig:
    """What the app needs from the outside world; tests swap these out."""

    resolve: Callable[[], Path]  # the current hub's directory (raises HubError when there's none)
    token: str
    home: Path = field(default_factory=Path.home)
    command: str | None = None  # the permanent `llm-hub`, for setup and the server self-test
    claude_bin: str | None = None
    use_hub: Callable[[Path], Path] | None = None  # make a hub current (creating it if needed)
    recent_hubs: Callable[[], list[str]] = list
    pick_folder: Callable[[], str | None] | None = None
    reveal: Callable[[str], None] | None = None
    quit_claude: Callable[[], None] | None = None
    health: Callable[[], list[dict]] | None = None
    setup: Callable[[], list[dict]] | None = None
    poll_interval: float = 1.0
    max_events: int | None = None  # end the event stream after this many changes (tests)
    idle_timeout: float | None = None  # seconds without requests or open pages before `on_idle` runs
    on_idle: Callable[[], None] | None = None


# ---------- serialization ----------

def _post(post: Post) -> dict:
    return {"id": post.id, "author": post.author, "hand_to": post.hand_to, "type": post.type, "ts": post.ts,
            "body": post.body, "re": post.re}


def _thread_summary(hub: Hub, thread: Thread) -> dict:
    meta = thread.meta
    last = thread.posts[-1] if thread.posts else None
    return {
        "id": thread.id,
        "title": thread.title,
        "status": meta.get("status", "open"),
        "awaiting": meta.get("awaiting", NOBODY),
        "participants": meta.get("participants") or [],
        "tags": meta.get("tags") or [],
        "turns": int(meta.get("turns", 0)),
        "max_turns": int(meta.get("max_turns", hub.max_turns)),
        "created": meta.get("created"),
        "updated": meta.get("updated"),
        "revision": thread.revision,
        "posts": len(thread.posts),
        "unread": len(hub.unread(HUMAN, thread)),
        "last": {"author": last.author, "ts": last.ts, "preview": " ".join(last.body.split())[:140]} if last else None,
    }


def _thread_full(hub: Hub, thread: Thread, first_unread: str | None) -> dict:
    return {
        **_thread_summary(hub, thread),
        "summary": thread.summary,
        "summary_by": thread.meta.get("summary_by"),
        "related": thread.meta.get("related") or [],
        "resolved_by": thread.meta.get("resolved_by"),
        "posts": [_post(p) for p in thread.posts],
        "first_unread": first_unread,
    }


def _error(message: str, status: int = 400, **extra) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


# ---------- the app ----------

def create_app(config: AppConfig) -> Starlette:
    last_activity = [time.monotonic()]
    open_pages = [0]
    health_cache: dict = {}

    def hub() -> Hub:
        return Hub(config.resolve())

    def allowed(request: Request) -> Response | None:
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if host not in LOCAL_HOSTS:
            return _error("This app only answers on localhost.", 403)
        origin = request.headers.get("origin")
        if origin and (urlsplit(origin).hostname or "") not in {h.strip("[]") for h in LOCAL_HOSTS}:
            return _error("Requests from other sites aren't allowed.", 403)
        return None

    def authorized(request: Request) -> bool:
        return hmac.compare_digest(request.cookies.get(COOKIE, ""), config.token)

    async def guard(request: Request, handler) -> Response:
        last_activity[0] = time.monotonic()
        if refused := allowed(request):
            return refused
        if not authorized(request):
            return _error("Open LLM Hub from your Applications folder (or run `llm-hub app`) to sign in.", 401)
        try:
            return await handler(request)
        except HubError as exc:
            return _error(str(exc), 400)

    def api(handler):
        async def endpoint(request: Request) -> Response:
            return await guard(request, handler)
        return endpoint

    async def body_of(request: Request) -> dict:
        try:
            data = await request.json()
        except ValueError:
            raise HubError("Send a JSON body.")
        if not isinstance(data, dict):
            raise HubError("Send a JSON object.")
        return data

    # ----- pages -----

    static = Path(str(files("llm_hub.web").joinpath("static")))
    # Version the assets by content (wheels give every file the same mtime) so an upgrade never runs a cached app.js.
    asset_version = hashlib.sha256(b"".join((static / n).read_bytes() for n in ("app.js", "app.css"))).hexdigest()[:12]

    async def index(request: Request) -> Response:
        if refused := allowed(request):
            return refused
        html = (static / "index.html").read_text().replace("__VERSION__", asset_version)
        return Response(html, media_type="text/html", headers={"Cache-Control": "no-store", "Content-Security-Policy": CSP})

    async def open_session(request: Request) -> Response:
        """The launcher opens /open?t=<token>; trade it for a cookie and drop it from the address bar."""
        if refused := allowed(request):
            return refused
        if not hmac.compare_digest(request.query_params.get("t", ""), config.token):
            return _error("This link has expired. Open LLM Hub again from your Applications folder.", 401)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE, config.token, max_age=365 * 86400, httponly=True, samesite="strict")
        return response

    async def ping(request: Request) -> Response:
        """Lets `llm-hub app` tell its own server from something else on the port. No data, no auth."""
        return JSONResponse({"app": "llm-hub"})

    # ----- hub -----

    async def get_state(request: Request) -> Response:
        try:
            current = hub()
        except HubError as exc:
            return JSONResponse({"hub": None, "message": str(exc), "recent": config.recent_hubs()})
        return JSONResponse({
            "hub": {
                "root": str(current.root),
                "repo": current.root.parent.name,
                "agents": [a for a in current.agents if a != HUMAN],
                "max_turns": current.max_turns,
                "repo_url": await run_in_threadpool(lambda: current.repo_url),
            },
            "post_types": list(POST_TYPES),
            "recent": config.recent_hubs(),
        })

    async def set_hub(request: Request) -> Response:
        if not config.use_hub:
            return _error("Switching hubs isn't available here.", 501)
        data = await body_of(request)
        path = data.get("path")
        if data.get("pick"):
            if not config.pick_folder:
                return _error("The folder picker isn't available here.", 501)
            path = await run_in_threadpool(config.pick_folder)
            if not path:
                return JSONResponse({"cancelled": True})
        if not path:
            raise HubError("Choose a folder.")
        root = await run_in_threadpool(config.use_hub, Path(path))
        return JSONResponse({"root": str(root)})

    # ----- threads -----

    async def list_threads(request: Request) -> Response:
        current = hub()
        query = (request.query_params.get("q") or "").strip()
        threads = [t for t, _ in current.search(query)] if query else current.threads(status="all")
        items = [_thread_summary(current, t) for t in threads]
        items.sort(key=lambda t: t["updated"] or "", reverse=True)
        return JSONResponse({"threads": items})

    async def get_thread(request: Request) -> Response:
        current = hub()
        thread = current.get(request.path_params["tid"])
        unread = current.unread(HUMAN, thread)
        thread, _ = current.read(HUMAN, thread.id, only_new=False)  # marks it read for you
        return JSONResponse(_thread_full(current, thread, unread[0].id if unread else None))

    async def create_thread(request: Request) -> Response:
        current = hub()
        data = await body_of(request)
        tags = data.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        thread = current.create_thread(
            HUMAN, str(data.get("title", "")), str(data.get("body", "")), str(data.get("to", "")),
            str(data.get("type") or "question"), tags,
        )
        return JSONResponse({"id": thread.id}, status_code=201)

    def check_revision(current: Hub, tid: str, expected) -> JSONResponse | None:
        """The store lets the human write at any time; the app still refuses to post over news you haven't seen."""
        if expected is None:
            return None
        thread = current.get(tid)
        if int(expected) != thread.revision:
            return _error("Someone posted while you were writing. Read the new post, then send again.", 409,
                          code="changed", revision=thread.revision)
        return None

    async def reply(request: Request) -> Response:
        current = hub()
        tid = request.path_params["tid"]
        data = await body_of(request)
        if conflict := check_revision(current, tid, data.get("expected_revision")):
            return conflict
        thread, post, notes = current.reply(
            HUMAN, tid, str(data.get("body", "")), str(data.get("hand_to") or NOBODY),
            str(data.get("type") or "note"), data.get("re") or None,
        )
        return JSONResponse({"post": _post(post), "revision": thread.revision, "notes": notes}, status_code=201)

    async def resolve(request: Request) -> Response:
        current = hub()
        tid = request.path_params["tid"]
        data = await body_of(request)
        if conflict := check_revision(current, tid, data.get("expected_revision")):
            return conflict
        decision = str(data.get("decision", "")).strip()
        if not decision:
            raise HubError("Write what was decided.")
        thread = current.resolve(HUMAN, tid, decision)
        return JSONResponse({"revision": thread.revision})

    # ----- live updates -----

    def signature() -> tuple:
        try:
            root = config.resolve()
            threads = Path(root) / "threads"
            return (str(root), tuple(sorted((p.name, p.stat().st_mtime_ns) for p in threads.glob("T-*.md"))))
        except (HubError, OSError):
            return ("", ())

    async def events(request: Request) -> Response:
        async def stream():
            open_pages[0] += 1
            try:
                previous = await run_in_threadpool(signature)
                sent = 0
                yield {"event": "hello", "data": "{}"}
                while not await request.is_disconnected():
                    await asyncio.sleep(config.poll_interval)
                    current = await run_in_threadpool(signature)
                    if current != previous:
                        old = dict(previous[1])
                        changed = [name[:6] for name, mtime in current[1] if old.get(name) != mtime]
                        hub_changed = current[0] != previous[0]
                        previous = current
                        yield {"event": "change", "data": _json({"threads": changed, "hub": hub_changed})}
                        sent += 1
                        if config.max_events and sent >= config.max_events:
                            return
            finally:
                open_pages[0] -= 1

        return EventSourceResponse(stream(), ping=15)

    # ----- setup and health -----

    async def health(request: Request) -> Response:
        if not config.health:
            return _error("Health checks aren't available here.", 501)
        fresh = request.query_params.get("fresh") == "1"
        if fresh or time.monotonic() - health_cache.get("at", 0) > 15:
            health_cache["checks"] = await run_in_threadpool(config.health)
            health_cache["at"] = time.monotonic()
        return JSONResponse({"checks": health_cache["checks"]})

    async def run_setup(request: Request) -> Response:
        if not config.setup:
            return _error("Setup isn't available here.", 501)
        steps = await run_in_threadpool(config.setup)
        health_cache.clear()
        return JSONResponse({"steps": steps})

    async def reveal(request: Request) -> Response:
        data = await body_of(request)
        what = data.get("what")
        if what != "claude-skill" or not config.reveal:
            return _error("Nothing to show.", 400)
        await run_in_threadpool(config.reveal, what)
        return JSONResponse({"ok": True})

    async def quit_claude(request: Request) -> Response:
        if not config.quit_claude:
            return _error("Not available here.", 501)
        await run_in_threadpool(config.quit_claude)
        health_cache.clear()
        return JSONResponse({"ok": True})

    async def watch_idle() -> None:
        while True:
            await asyncio.sleep(30)
            idle = time.monotonic() - last_activity[0]
            if open_pages[0] == 0 and config.idle_timeout and idle > config.idle_timeout and config.on_idle:
                config.on_idle()
                return

    @contextlib.asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(watch_idle()) if config.idle_timeout else None
        yield
        if task:
            task.cancel()

    routes = [
        Route("/", index),
        Route("/open", open_session),
        Route("/api/ping", ping),
        Route("/api/state", api(get_state)),
        Route("/api/hubs", api(set_hub), methods=["POST"]),
        Route("/api/threads", api(list_threads)),
        Route("/api/threads", api(create_thread), methods=["POST"]),
        Route("/api/threads/{tid}", api(get_thread)),
        Route("/api/threads/{tid}/posts", api(reply), methods=["POST"]),
        Route("/api/threads/{tid}/resolve", api(resolve), methods=["POST"]),
        Route("/api/events", api(events)),
        Route("/api/health", api(health)),
        Route("/api/setup", api(run_setup), methods=["POST"]),
        Route("/api/reveal", api(reveal), methods=["POST"]),
        Route("/api/claude/quit", api(quit_claude), methods=["POST"]),
        Mount("/static", StaticFiles(directory=static), name="static"),
    ]
    return Starlette(routes=routes, lifespan=lifespan)


def _json(data) -> str:
    return json.dumps(data)


# ---------- the real world (macOS) ----------

def pick_folder() -> str | None:
    """The Mac's own folder picker. Returns None when the user cancels."""
    script = 'POSIX path of (choose folder with prompt "Pick the project folder for your hub")'
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def quit_claude_desktop() -> None:
    from .. import desktop

    subprocess.run(["osascript", "-e", f'tell application id "{desktop.BUNDLE_ID}" to quit'], capture_output=True)


def health_checks(home: Path, command: str | None, claude_bin: str | None, resolve: Callable[[], Path]) -> list[dict]:
    from ..doctor import run_doctor

    return [asdict(check) for check in run_doctor(home, command, claude_bin, resolve)]


def setup_steps(home: Path, command: str, claude_bin: str | None) -> list[dict]:
    from ..setup import apply_setup

    return [asdict(step) for step in apply_setup(home, command, claude_bin)]


def reveal_claude_skill(home: Path) -> None:
    from ..setup import skill_zip_dir, skill_zips

    path = skill_zip_dir(home) / "llm-hub-claude-skill.zip"
    if not path.exists():
        skill_zips(path.parent)
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", str(path)], capture_output=True)
