"""`llm-hub run`: let the agents take turns on a thread by themselves, so nobody has to tell each app "check the hub".

The desktop apps can't be scripted, so each turn runs the agent headless: `claude -p` for claude, `codex exec` for gpt.
Each session gets only the thread-scoped hub server (`serve --thread`: read_thread, reply, update_summary, resolve)
and, optionally, read-only access to the hub's project folder. It runs until an agent hands the turn to the human or
closes the thread, the turn budget runs out, an agent fails to post twice in a row, or someone stops the run.

Progress is kept in <hub>/.state/runs/<thread>.json (and .log) so the app and `llm-hub run --status` can show it.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .store import HUMAN, NOBODY, Hub, HubError, normalize_thread_id

DEFAULT_TURNS = 20
TURN_TIMEOUT = 20 * 60  # seconds one agent may take for one turn
HUB_TOOLS = ("read_thread", "reply", "update_summary", "resolve")
READ_TOOLS = ("Read", "Grep", "Glob")
AUTH_ERRORS = ("Not logged in", "Please run /login", "Failed to authenticate", "OAuth session expired", "401 Unauthorized")

PROMPT = """It's your turn ({agent}) on LLM Hub thread {thread}. You are running unattended: nobody reads this session, \
so if you need something from the human, ask in the thread and hand the turn to human.

1. Call read_thread('{thread}').
2. {files}
3. Take your turn by the hub rules: optionally update_summary, then exactly one reply (hand_to the other agent, or to \
human when you need a decision or information only the human has), or resolve when the thread's goal is met.
4. Stop after posting. Nobody reads your final message here.

This run allows about {left} more agent posts. Don't pad the thread: when the goal is met, resolve it."""

FILES_ON = ("The project is your working directory ({repo}). You can read its files (read-only) to check claims "
            "yourself instead of asking for excerpts. You can't edit files, run builds or use the internet.")
FILES_OFF = "You only have the thread: you can't read the project's files."


# ---------- the agents' command-line tools ----------

SESSION_VARS = ("ANTHROPIC_BASE_URL", "MCP_CONNECTION_NONBLOCKING", "MCP_SERVER_CONNECTION_BATCH_SIZE",
                "DISABLE_AUTOUPDATER", "DISABLE_MICROCOMPACT")


def agent_env() -> dict[str, str]:
    """The environment for agent CLIs. Started from inside a Claude Code session (CLAUDECODE is set), the runner
    inherits that session's variables, and ANTHROPIC_BASE_URL then points `claude` at the host app instead of the
    user's own sign-in, so `claude` reports "not logged in". Drop them there; elsewhere keep the user's environment."""
    env = dict(os.environ)
    if "CLAUDECODE" in env:
        for key in list(env):
            if key.startswith("CLAUDE") or key in SESSION_VARS:
                del env[key]
    return env


def _works(path: str | None, *args: str) -> bool:
    if not path or not os.access(path, os.X_OK):
        return False
    try:
        return subprocess.run([path, *args], capture_output=True, timeout=30, env=agent_env()).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def find_claude() -> str | None:
    """The `claude` CLI (Claude Code). The desktop app's bundled copy can't sign in on its own, so it isn't used."""
    home = Path.home()
    candidates = [os.environ.get("LLM_HUB_CLAUDE"), shutil.which("claude"), str(home / ".local/bin/claude"),
                  str(home / ".claude/local/claude"), "/opt/homebrew/bin/claude", "/usr/local/bin/claude"]
    return next((c for c in candidates if _works(c, "--version")), None)


def find_codex() -> str | None:
    """`codex`, preferring the copy inside ChatGPT desktop (an npm-installed one can be missing its binary)."""
    home = Path.home()
    candidates = [os.environ.get("LLM_HUB_CODEX"), "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
                  str(home / ".codex/plugins/.plugin-appserver/codex-cli/bin/codex"), shutil.which("codex")]
    return next((c for c in candidates if _works(c, "--version")), None)


@dataclass
class Agent:
    """How to run one agent for one turn."""

    name: str
    exe: str | None

    def command(self, prompt: str, mcp_cmd: list[str], repo: Path, files: bool) -> list[str]:
        raise NotImplementedError

    def login_problem(self) -> str | None:
        """A sentence telling the human how to fix sign-in, or None when signed in."""
        return None


class ClaudeAgent(Agent):
    def command(self, prompt: str, mcp_cmd: list[str], repo: Path, files: bool) -> list[str]:
        mcp = {"mcpServers": {"llm-hub": {"command": mcp_cmd[0], "args": mcp_cmd[1:]}}}
        allowed = [f"mcp__llm-hub__{t}" for t in HUB_TOOLS] + (list(READ_TOOLS) if files else [])
        return [self.exe, "-p", prompt, "--mcp-config", json.dumps(mcp), "--strict-mcp-config",
                "--tools", ",".join(READ_TOOLS) if files else "", "--allowedTools", ",".join(allowed),
                "--permission-mode", "dontAsk", "--no-session-persistence"]

    def login_problem(self) -> str | None:
        if not self.exe:
            return "Claude Code isn't installed. Install it (https://claude.com/claude-code), then sign in."
        try:
            out = subprocess.run([self.exe, "auth", "status"], capture_output=True, text=True, timeout=30, env=agent_env())
            if json.loads(out.stdout or "{}").get("loggedIn"):
                return None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
        return "Claude Code isn't signed in. Open Terminal, run `claude auth login` once, then start again."


class CodexAgent(Agent):
    def command(self, prompt: str, mcp_cmd: list[str], repo: Path, files: bool) -> list[str]:
        cmd = [self.exe, "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
               # read-only also means no network for shell commands; without files, a scratch folder as workdir
               "--sandbox", "read-only", "-C", str(repo if files else Path.home() / ".llm-hub"),
               "-c", f"mcp_servers.llm-hub.command={json.dumps(mcp_cmd[0])}",
               "-c", f"mcp_servers.llm-hub.args={json.dumps(mcp_cmd[1:])}",
               "-c", 'approval_policy="never"']
        # --ignore-user-config keeps the user's other MCP servers out; carry over their model choice.
        for key, value in codex_model_settings().items():
            cmd += ["-c", f"{key}={json.dumps(value)}"]
        return [*cmd, prompt]

    def login_problem(self) -> str | None:
        if not self.exe:
            return "Codex wasn't found. Install ChatGPT desktop (it includes Codex), then sign in."
        try:
            if subprocess.run([self.exe, "login", "status"], capture_output=True, timeout=30, env=agent_env()).returncode == 0:
                return None
        except (OSError, subprocess.TimeoutExpired):
            pass
        return "Codex isn't signed in. Open ChatGPT desktop and sign in, then start again."


def codex_model_settings() -> dict[str, str]:
    try:
        config = tomllib.loads((Path.home() / ".codex" / "config.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return {k: config[k] for k in ("model", "model_reasoning_effort") if isinstance(config.get(k), str)}


def default_agents() -> dict[str, Agent]:
    return {"claude": ClaudeAgent("claude", find_claude()), "gpt": CodexAgent("gpt", find_codex())}


# ---------- run state ----------

@dataclass
class RunState:
    thread: str
    status: str = "running"  # running | done | stopped | failed
    pid: int = 0
    max_turns: int = DEFAULT_TURNS
    posts: int = 0
    agent: str | None = None  # who is taking a turn right now
    reason: str = ""
    started: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)


def runs_dir(hub_root: Path) -> Path:
    return Path(hub_root) / ".state" / "runs"


def state_path(hub_root: Path, thread: str) -> Path:
    return runs_dir(hub_root) / f"{normalize_thread_id(thread)}.json"


def log_path(hub_root: Path, thread: str) -> Path:
    return runs_dir(hub_root) / f"{normalize_thread_id(thread)}.log"


def save_state(hub_root: Path, state: RunState) -> None:
    state.updated = time.time()
    path = state_path(hub_root, state.thread)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2))
    os.replace(tmp, path)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return pid > 0


def load_state(hub_root: Path, thread: str) -> RunState | None:
    """The thread's latest run; a 'running' one whose process is gone reads as stopped."""
    try:
        data = json.loads(state_path(hub_root, thread).read_text())
        state = RunState(**{k: v for k, v in data.items() if k in RunState.__dataclass_fields__})
    except (OSError, ValueError, TypeError):
        return None
    if state.status == "running" and not _alive(state.pid):
        state.status, state.agent, state.reason = "stopped", None, "The run ended unexpectedly. See its log."
    return state


def stop(hub_root: Path, thread: str) -> bool:
    """Ask a running run to stop (it finishes cleanly and records why). Returns whether one was running."""
    state = load_state(hub_root, thread)
    if not state or state.status != "running":
        return False
    os.kill(state.pid, signal.SIGTERM)
    return True


def notify(message: str) -> None:
    if sys.platform == "darwin":
        script = f"display notification {json.dumps(message)} with title \"LLM Hub\""
        subprocess.run(["osascript", "-e", script], capture_output=True)


# ---------- the loop ----------

def display(agent: str) -> str:
    return {"claude": "Claude", "gpt": "GPT", HUMAN: "You"}.get(agent, agent)


def preflight(hub_root: Path, thread_id: str, agents: dict[str, Agent]) -> tuple[Hub, str]:
    """Everything that would stop a run from starting, as a HubError with a sentence for the human."""
    hub = Hub(hub_root)
    thread = hub.get(thread_id)
    tid = thread.id
    previous = load_state(hub.root, tid)
    if previous and previous.status == "running":
        raise HubError(f"{tid} is already running. Stop it first.")
    if thread.meta["status"] != "open":
        raise HubError(f"{tid} is closed. Reply to reopen it first.")
    awaiting = thread.meta["awaiting"]
    if awaiting not in agents:
        who = "your" if awaiting == HUMAN else "nobody's" if awaiting == NOBODY else f"{awaiting}'s"
        raise HubError(f"It's {who} turn on {tid}. Post and hand the turn to Claude or GPT first.")
    for name in sorted(a for a in hub.agents if a in agents):
        if problem := agents[name].login_problem():
            raise HubError(problem)
    return hub, tid


def run(
    hub_root: Path,
    thread_id: str,
    max_turns: int = DEFAULT_TURNS,
    *,
    mcp_command: str,
    agents: dict[str, Agent] | None = None,
    files: bool = True,
    turn_timeout: float = TURN_TIMEOUT,
    on_change: Callable[[RunState], None] = lambda s: None,
    alert: Callable[[str], None] = notify,
) -> RunState:
    """Run agents on the thread until it needs the human. `mcp_command` is the llm-hub that serves each session."""
    agents = default_agents() if agents is None else agents
    hub, tid = preflight(hub_root, thread_id, agents)
    hub.allow_turns(tid, max_turns)
    state = RunState(thread=tid, pid=os.getpid(), max_turns=max_turns)
    child: list[subprocess.Popen] = []
    stopping = [False]

    def on_sigterm(signum, frame):
        stopping[0] = True
        if child:
            try:
                os.killpg(child[0].pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    old_handler = signal.signal(signal.SIGTERM, on_sigterm)
    log = log_path(hub.root, tid)
    log.parent.mkdir(parents=True, exist_ok=True)

    def update(**changes) -> None:
        for key, value in changes.items():
            setattr(state, key, value)
        save_state(hub.root, state)
        on_change(state)

    def finish(status: str, reason: str) -> RunState:
        update(status=status, agent=None, reason=reason)
        alert(f"{tid}: {reason}")
        return state

    update()
    failures = 0
    try:
        with log.open("a") as out:
            out.write(f"\n=== run started {time.strftime('%Y-%m-%d %H:%M:%S')} · up to {max_turns} agent posts ===\n")
            for _ in range(max_turns * 2 + 4):  # a safety net; the budget and the checks below end the run first
                if stopping[0]:
                    return finish("stopped", "Stopped. The thread is as the agents left it.")
                t = hub.get(tid)
                meta = t.meta
                if meta["status"] != "open":
                    by = meta.get("resolved_by")
                    return finish("done", f"Closed by {display(by)}." if by and by != HUMAN else "The thread was closed.")
                awaiting = meta["awaiting"]
                if awaiting == HUMAN:
                    if int(meta.get("turns", 0)) >= int(meta.get("max_turns", max_turns)):
                        return finish("done", f"Used its {max_turns} turns. It's your turn.")
                    last = t.posts[-1].author if t.posts else "someone"
                    return finish("done", f"{display(last)} handed it to you.")
                if awaiting == NOBODY:
                    return finish("done", "Nothing is pending.")
                agent = agents.get(awaiting)
                if not agent or not agent.exe:
                    return finish("failed", f"There's no way to run {awaiting} here.")

                before = len(t.posts)
                prompt = PROMPT.format(agent=awaiting, thread=tid, left=max(1, max_turns - state.posts),
                                       files=(FILES_ON if files else FILES_OFF).format(repo=hub.root.parent))
                mcp_cmd = [mcp_command, "--root", str(hub.root), "serve", "--agent", awaiting, "--thread", tid]
                update(agent=awaiting)
                out.write(f"\n--- {awaiting} · {time.strftime('%H:%M:%S')} ---\n")
                out.flush()
                proc = subprocess.Popen(agent.command(prompt, mcp_cmd, hub.root.parent, files), cwd=hub.root.parent,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, start_new_session=True, env=agent_env())
                child[:] = [proc]
                try:
                    output, _ = proc.communicate(timeout=turn_timeout)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    output, _ = proc.communicate()
                    output += f"\n[llm-hub] {awaiting} took longer than {turn_timeout:.0f}s and was stopped.\n"
                child.clear()
                out.write(output or "")
                out.flush()

                after = hub.get(tid)
                mine = [p for p in after.posts[before:] if p.author == awaiting]
                if mine or after.meta["status"] != "open":
                    failures = 0
                    update(posts=state.posts + len(mine), agent=None)
                    continue
                if stopping[0]:
                    continue
                if any(err in (output or "") for err in AUTH_ERRORS):
                    return finish("failed", agent.login_problem() or f"{awaiting} couldn't sign in.")
                failures += 1
                update(agent=None)
                if failures >= 2:
                    return finish("failed", f"{display(awaiting)} didn't post, twice. See the run's log.")
            return finish("done", "Stopped after its safety limit.")
    except Exception as exc:  # record why, so the app doesn't show a run that silently vanished
        finish("failed", f"The run crashed: {type(exc).__name__}: {exc}"[:300])
        raise
    finally:
        signal.signal(signal.SIGTERM, old_handler)


def start_detached(command: str, hub_root: Path, thread: str, max_turns: int, files: bool = True) -> int:
    """Start `llm-hub run` in the background (used by the app). Returns its PID."""
    args = [command, "--root", str(hub_root), "run", normalize_thread_id(thread), "--max-turns", str(max_turns)]
    if not files:
        args.append("--no-files")
    log = log_path(hub_root, thread)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as fh:
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                                start_new_session=True, close_fds=True)
    return proc.pid
