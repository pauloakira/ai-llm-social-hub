"""`llm-hub doctor`: check, without changing anything, that each app can actually reach the hub.

Setup only proves the config files are right. Doctor also checks that the server starts and answers, and that
each app has loaded it: Claude Code through `claude mcp get`, Claude desktop through its log, ChatGPT/Codex
through a running `llm-hub serve --agent gpt`.
"""

from __future__ import annotations

import json
import select
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import desktop
from .setup import SERVER, claude_desktop, codex, skill_zip_dir, skills
from .store import Hub, HubError

TOOLS = {"inbox", "list_threads", "read_thread", "create_thread", "reply", "update_summary", "resolve", "search"}
SETUP = "Run `llm-hub setup`."


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail | skip | info
    detail: str = ""
    fix: str = ""


def _list_tools(args: list[str], messages: list[dict], timeout: float) -> tuple[set[str], str]:
    """Answer each request before sending the next: a stdio server exits at EOF without finishing pending ones.
    Returns the tool names and, when there are none, the reason (the server's last stderr line if it has one)."""
    with tempfile.TemporaryFile("w+") as stderr:
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, text=True)
        deadline = time.monotonic() + timeout
        tools, reason = set(), ""
        try:
            for message in messages:
                proc.stdin.write(json.dumps(message) + "\n")
                proc.stdin.flush()
                while "id" in message:
                    if not select.select([proc.stdout], [], [], max(0.0, deadline - time.monotonic()))[0]:
                        reason = f"no reply within {timeout:.0f}s"
                        break
                    line = proc.stdout.readline()
                    if not line:
                        reason = "server exited without replying"
                        break
                    try:
                        reply = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if reply.get("id") != message["id"]:
                        continue
                    if "error" in reply:
                        reason = str(reply["error"].get("message", reply["error"]))
                    elif message["method"] == "tools/list":
                        tools = {t["name"] for t in reply.get("result", {}).get("tools", [])}
                    break
                if reason:
                    break
        except OSError as exc:  # e.g. BrokenPipeError: the server died before reading
            reason = f"{type(exc).__name__}: {exc}"
        finally:
            proc.kill()
            proc.wait()
        if not tools:
            stderr.seek(0)
            reason = (stderr.read().strip().splitlines() or [reason or "no tools"])[-1]
        return tools, reason


def server_self_test(command: str, agent: str = "claude", timeout: float = 30) -> Check:
    """Start the server the way the apps do and ask it for its tools over stdio."""
    name = "server self-test"
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "llm-hub-doctor", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    try:
        tools, err = _list_tools([command, "serve", "--agent", agent], messages, timeout)
    except OSError as exc:
        return Check(name, "fail", f"{type(exc).__name__}: {exc}"[:200], "Reinstall: uv tool install --reinstall git+https://github.com/pauloakira/ai-llm-social-hub")
    if TOOLS <= tools:
        return Check(name, "ok", f"`{command} serve` answers with {len(tools)} tools")
    return Check(name, "fail", f"server gave {len(tools)} tools: {err}"[:200],
                 "Reinstall: uv tool install --reinstall git+https://github.com/pauloakira/ai-llm-social-hub")


def current_hub(resolve: Callable[[], Path]) -> Check:
    name = "current hub"
    try:
        hub = Hub(resolve())
        threads = hub.threads(status="open")
    except (HubError, OSError) as exc:
        return Check(name, "fail", str(exc)[:200], "Create one: `llm-hub init <repo>` (or `llm-hub use <repo>`).")
    return Check(name, "ok", f"{hub.root} ({len(threads)} open thread{'s' if len(threads) != 1 else ''})")


def installed_skills(home: Path) -> list[Check]:
    checks = []
    for step in skills(home, dry_run=True, uninstall=False):
        if step.status == "present":
            checks.append(Check(step.target, "ok", step.detail))
        elif step.status == "skipped":
            checks.append(Check(step.target, "skip", step.detail))
        else:
            what = "missing" if step.status == "added" else "out of date"
            checks.append(Check(step.target, "fail", f"{what}: {step.detail}", SETUP))
    return checks


def claude_code_mcp(claude_bin: str | None) -> Check:
    name = "MCP · Claude Code"
    if not claude_bin:
        return Check(name, "skip", "`claude` CLI not found")
    try:
        out = subprocess.run([claude_bin, "mcp", "get", SERVER], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check(name, "warn", f"`claude mcp get` failed: {exc}"[:200])
    if out.returncode != 0:
        return Check(name, "fail", "not registered", SETUP)
    status = next((line.split(":", 1)[1].strip() for line in out.stdout.splitlines() if line.strip().startswith("Status:")), "")
    if "Connected" in status:
        return Check(name, "ok", "registered and connects (user scope)")
    return Check(name, "fail", f"registered but {status or 'status unknown'}", "Check `claude mcp get llm-hub`; then start a new session.")


def claude_desktop_mcp(home: Path, command: str) -> Check:
    name = "MCP · Claude desktop"
    step = claude_desktop(home, command, dry_run=True, uninstall=False)
    if step.status == "skipped":
        return Check(name, "skip", step.detail)
    finisher = desktop.pending(home)
    if step.status != "present":
        if finisher:
            return Check(name, "warn", "waiting for you to quit Claude (Cmd+Q); it reopens with the hub", "Quit Claude (Cmd+Q).")
        return Check(name, "fail", f"not in {step.detail}", SETUP + " If Claude is open, quit it (Cmd+Q) when setup says so.")
    if not desktop.pids():
        return Check(name, "ok", "in the config; Claude loads it when you open it")
    state, count = desktop.load_state(home)
    if state == "connected":
        return Check(name, "ok", f"connected ({count} tools)")
    if state == "launched":
        return Check(name, "fail", "Claude started the server but it never connected; see ~/Library/Logs/Claude/main.log",
                     "Check the server self-test above, then quit Claude (Cmd+Q) and reopen it.")
    if finisher:
        return Check(name, "warn", "in the config, not loaded yet: waiting for you to quit Claude (Cmd+Q)", "Quit Claude (Cmd+Q).")
    return Check(name, "warn", "in the config, but the running Claude started before it was added",
                 SETUP + " Then quit Claude (Cmd+Q); it reopens with the hub.")


def claude_chat_skill(home: Path) -> Check:
    name = "skill · Claude Chat tab"
    if not (home / "Library" / "Application Support" / "Claude").is_dir():
        return Check(name, "skip", "Claude desktop not found")
    path = skill_zip_dir(home) / "llm-hub-claude-skill.zip"
    if not path.exists():
        return Check(name, "info", "zip not built yet", SETUP + " Then upload the zip it names in Claude → Settings → Capabilities → Skills.")
    return Check(name, "info", f"can't be checked from here: upload {path} in Claude → Settings → Capabilities → Skills")


def codex_mcp(home: Path, command: str) -> Check:
    name = "MCP · ChatGPT/Codex"
    step = codex(home, command, dry_run=True, uninstall=False)
    if step.status == "skipped":
        return Check(name, "skip", step.detail)
    if step.status != "present":
        return Check(name, "fail", f"not in {step.detail}", SETUP + " Then restart ChatGPT/Codex.")
    try:
        ps = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        ps = ""
    if any(f"{SERVER} serve --agent gpt" in line for line in ps.splitlines()):
        return Check(name, "ok", "in the config, and a gpt server is running")
    return Check(name, "ok", "in the config; the server starts when ChatGPT or Codex opens a session")


def run_doctor(home: Path, command: str | None, claude_bin: str | None, resolve: Callable[[], Path]) -> list[Check]:
    if not command:
        return [Check("llm-hub command", "fail", "no permanent install found",
                      "Install: uv tool install git+https://github.com/pauloakira/ai-llm-social-hub")]
    checks = [Check("llm-hub command", "ok", command), server_self_test(command), current_hub(resolve)]
    checks += installed_skills(home)
    checks.append(claude_chat_skill(home))
    for fn in (lambda: claude_code_mcp(claude_bin), lambda: claude_desktop_mcp(home, command), lambda: codex_mcp(home, command)):
        try:
            checks.append(fn())
        except Exception as exc:  # one broken app config shouldn't hide the others
            checks.append(Check("check", "fail", f"{type(exc).__name__}: {exc}"[:200]))
    return checks
