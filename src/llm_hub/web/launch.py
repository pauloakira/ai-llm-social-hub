"""`llm-hub app`: start the app's local server if it isn't running, then open it in the browser.

The server runs detached, so the launcher (LLM Hub.app or the command) returns at once, and it stops by itself
after 30 minutes with no open page and no requests.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import time
import urllib.request
import webbrowser
from pathlib import Path

DEFAULT_PORT = 8770
IDLE_TIMEOUT = 30 * 60


def state_dir() -> Path:
    return Path(os.environ.get("LLM_HUB_HOME", "~/.llm-hub")).expanduser()


def load_token() -> str:
    """The app's secret, created once and readable only by you."""
    path = state_dir() / "app-token"
    if path.exists() and path.read_text().strip():
        return path.read_text().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(token)
    return token


def is_ours(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=0.5) as resp:
            return json.loads(resp.read()).get("app") == "llm-hub"
    except (OSError, ValueError):
        return False


def is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def launch(command: str, port: int = DEFAULT_PORT, open_browser: bool = True) -> str:
    """Reuse a running app server or start one, and return (and open) the sign-in URL."""
    token = load_token()
    for candidate in range(port, port + 10):
        if is_ours(candidate):
            break
        if is_free(candidate):
            log = (state_dir() / "app.log").open("a")
            subprocess.Popen(
                [command, "app", "--serve", "--port", str(candidate)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True,
            )
            deadline = time.monotonic() + 15
            while not is_ours(candidate):
                if time.monotonic() > deadline:
                    raise RuntimeError(f"The app server didn't start; see {state_dir() / 'app.log'}")
                time.sleep(0.2)
            break
    else:
        raise RuntimeError(f"Ports {port}-{port + 9} are all taken by other programs. Try `llm-hub app --port N`.")
    url = f"http://127.0.0.1:{candidate}/open?t={token}"
    if open_browser:
        webbrowser.open(url)
    return url


def serve(port: int, command: str | None, claude_bin: str | None, *, idle_timeout: float | None = IDLE_TIMEOUT) -> None:
    """Run the app server in this process (what the launcher starts in the background)."""
    import uvicorn

    from .. import cli
    from .app import AppConfig, create_app, health_checks, pick_folder, quit_claude_desktop, reveal_claude_skill, setup_steps

    home = Path.home()
    holder: dict = {}

    def stop() -> None:
        if server := holder.get("server"):
            server.should_exit = True

    def run_setup() -> list[dict]:
        if not command:
            raise cli.HubError("No permanent llm-hub install found. Reinstall it, then try again.")
        return setup_steps(home, command, claude_bin)

    def use(path: Path) -> Path:
        root = cli._hub_dir(path)
        if not (root / "hub.yaml").exists():
            from ..store import Hub

            Hub.init(root)
        return cli.use_hub(root)

    config = AppConfig(
        resolve=cli.resolve_root,
        token=load_token(),
        home=home,
        command=command,
        claude_bin=claude_bin,
        use_hub=use,
        recent_hubs=cli.recent_hubs,
        pick_folder=pick_folder,
        reveal=lambda what: reveal_claude_skill(home),
        quit_claude=quit_claude_desktop,
        health=lambda: health_checks(home, command, claude_bin, cli.resolve_root),
        setup=run_setup,
        idle_timeout=idle_timeout,
        on_idle=stop,
    )
    server = uvicorn.Server(uvicorn.Config(create_app(config), host="127.0.0.1", port=port, log_level="warning"))
    holder["server"] = server
    server.run()
