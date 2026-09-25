"""Claude desktop (Chat tab): its process, its log, and the background step that finishes setup.

Claude desktop keeps claude_desktop_config.json in memory and writes it back while it runs, so an entry added
underneath it can be dropped. It also relaunches itself about a second after Cmd+Q. So when Claude is running,
setup starts `finish`, a detached process that writes the entry the moment Claude quits (before the relaunch
reads the config) and then checks Claude's log to confirm the server connected.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

BUNDLE_ID = "com.anthropic.claudefordesktop"
MAIN_EXECUTABLE = "/Claude.app/Contents/MacOS/Claude"
CONNECTED = re.compile(r"Connected to llm-hub \((\d+) tools?\)")
LAUNCHED = "Launching MCP Server: llm-hub"
APP_START = re.compile(r"\] Starting app\b")
FINISH_FLAG = "--finish-claude-desktop"


def pids() -> list[int]:
    """PIDs of Claude desktop's main process (not its helpers, and not Claude Code)."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    found = []
    for line in out.splitlines():
        pid, _, comm = line.strip().partition(" ")
        if comm.strip().endswith(MAIN_EXECUTABLE) and pid.isdigit():
            found.append(int(pid))
    return found


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reopen() -> None:
    subprocess.run(["open", "-b", BUNDLE_ID], capture_output=True, timeout=30)


# ---------- Claude's log ----------

def log_path(home: Path) -> Path:
    return home / "Library" / "Logs" / "Claude" / "main.log"


def log_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def read_log(path: Path, offset: int = 0, limit: int = 8 << 20) -> str:
    """Log text from `offset` (from the start if the log was rotated since), at most the last `limit` bytes."""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            start = offset if offset <= size else 0
            fh.seek(max(start, size - limit))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def load_state(home: Path) -> tuple[str, str]:
    """What the current Claude desktop run did with llm-hub, from its log.

    Returns (state, detail): 'connected' (detail: tool count), 'launched' (started, never connected),
    'not-loaded' (Claude started without the entry) or 'unknown' (no log).
    """
    text = read_log(log_path(home))
    starts = list(APP_START.finditer(text))
    if not starts:
        return "unknown", ""
    run = text[starts[-1].start():]
    if found := CONNECTED.findall(run):
        return "connected", found[-1]
    if LAUNCHED in run:
        return "launched", ""
    return "not-loaded", ""


# ---------- the background finisher ----------

def state_dir(home: Path) -> Path:
    return home / ".llm-hub"


def pid_file(home: Path) -> Path:
    return state_dir(home) / "finish-claude-desktop.pid"


def finish_log(home: Path) -> Path:
    return state_dir(home) / "finish-claude-desktop.log"


def _command_of(pid: int) -> str:
    try:
        return subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def pending(home: Path) -> int | None:
    """PID of a finisher that is still waiting for Claude to quit, if any."""
    try:
        pid = int(pid_file(home).read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if alive(pid) and FINISH_FLAG in _command_of(pid) else None


def stop(home: Path) -> None:
    if pid := pending(home):
        os.kill(pid, signal.SIGTERM)
    pid_file(home).unlink(missing_ok=True)


def start(home: Path, command: str, uninstall: bool) -> int:
    """Start a detached finisher (replacing an older one). It survives Claude quitting: it has its own session."""
    stop(home)
    state_dir(home).mkdir(parents=True, exist_ok=True)
    args = [command, "setup", FINISH_FLAG, "--command", command] + (["--uninstall"] if uninstall else [])
    with finish_log(home).open("w") as log:
        proc = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True
        )
    pid_file(home).write_text(str(proc.pid))
    return proc.pid


def finish(
    home: Path,
    apply: Callable[[], str],
    uninstall: bool,
    *,
    find: Callable[[], list[int]] = pids,
    is_alive: Callable[[int], bool] = alive,
    open_app: Callable[[], None] = reopen,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    timeout: float = 24 * 3600,
    relaunch_wait: float = 5,
    verify_wait: float = 60,
    say: Callable[[str], None] = lambda msg: print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True),
) -> bool:
    """Wait for Claude to quit, `apply` the config change at once, reopen Claude if it doesn't relaunch itself,
    and confirm from its log. Repeats on the next quit if the relaunched Claude didn't pick the change up."""
    deadline = clock() + timeout
    log = log_path(home)
    say("waiting for Claude to quit (Cmd+Q)")
    while clock() < deadline:
        running = find()
        while running and clock() < deadline:
            running = [pid for pid in running if is_alive(pid)]
            if running:
                sleep(0.05)
        if running:
            break
        offset = log_size(log)
        say(f"Claude quit; llm-hub entry {apply()}")
        until = clock() + relaunch_wait
        while not find() and clock() < until:
            sleep(0.2)
        if not find():
            say("reopening Claude")
            open_app()
        until = clock() + verify_wait
        while clock() < until:
            text = read_log(log, offset)
            if uninstall:
                if APP_START.search(text) and find() and not CONNECTED.search(text):
                    say("OK: Claude reopened without llm-hub")
                    return True
            elif found := CONNECTED.findall(text):
                say(f"OK: Claude desktop connected to llm-hub ({found[-1]} tools)")
                return True
            if not find() and APP_START.search(text):
                break  # quit again before loading: go round and apply again
            sleep(0.5)
        say("Claude didn't load the change this time; waiting for the next quit")
    say("gave up waiting for Claude to quit; run llm-hub setup again")
    return False
