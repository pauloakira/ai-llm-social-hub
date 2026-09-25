"""`llm-hub setup`: wire the hub into every app on this machine in one idempotent step.

Targets (each skipped when its app isn't installed):
- skills: ~/.claude/skills/llm-hub (Claude Code) and ~/.codex/skills/llm-hub (ChatGPT desktop / Codex)
- Claude Code: user-scope MCP server, via the `claude` CLI
- Claude desktop: mcpServers entry in claude_desktop_config.json (deferred to the moment Claude quits while it runs)
- ChatGPT desktop / Codex: [mcp_servers.llm-hub] in ~/.codex/config.toml
- Claude Chat tab skill: a zip in ~/.llm-hub/skills to upload in Claude's settings (it can't be installed from here)

Config files are backed up to <file>.bak-llm-hub before the first change.
"""

from __future__ import annotations

import filecmp
import json
import re
import shutil
import subprocess
import tomllib
import zipfile
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from . import desktop

SERVER = "llm-hub"
CODEX_BLOCK = re.compile(r"(?ms)^\[mcp_servers\.llm-hub\][^\n]*\n.*?(?=^\[(?!mcp_servers\.llm-hub[.\]])|\Z)")


@dataclass
class Step:
    target: str
    status: str  # added | updated | present | removed | absent | skipped | pending | upload | failed
    detail: str = ""


def bundled_skills() -> Path:
    return Path(str(files("llm_hub").joinpath("skills")))


def _backup(path: Path, dry_run: bool) -> None:
    backup = path.with_name(path.name + ".bak-llm-hub")
    if path.exists() and not backup.exists() and not dry_run:
        shutil.copy2(path, backup)


def _same_tree(a: Path, b: Path) -> bool:
    if not b.is_dir():
        return False
    cmp = filecmp.dircmp(a, b)
    if cmp.left_only or cmp.right_only or cmp.diff_files or cmp.funny_files:
        return False
    return all(_same_tree(a / d, b / d) for d in cmp.common_dirs)


# ---------- skills ----------

def skills(home: Path, dry_run: bool, uninstall: bool) -> list[Step]:
    steps = []
    for app, root, flavor in (("Claude Code", home / ".claude", "claude"), ("ChatGPT/Codex", home / ".codex", "gpt")):
        target = f"skill · {app}"
        dest = root / "skills" / SERVER
        if not root.is_dir():
            steps.append(Step(target, "skipped", f"{root} not found"))
        elif uninstall:
            existed = dest.exists()
            if existed and not dry_run:
                shutil.rmtree(dest)
            steps.append(Step(target, "removed" if existed else "absent", str(dest)))
        else:
            src = bundled_skills() / flavor / SERVER
            if _same_tree(src, dest):
                steps.append(Step(target, "present", str(dest)))
                continue
            status = "updated" if dest.exists() else "added"
            if not dry_run:
                shutil.rmtree(dest, ignore_errors=True)
                shutil.copytree(src, dest)
            steps.append(Step(target, status, str(dest)))
    return steps


def _skill_files(flavor: str) -> dict[str, bytes]:
    src = bundled_skills() / flavor / SERVER
    return {str(Path(SERVER) / f.relative_to(src)): f.read_bytes() for f in sorted(src.rglob("*")) if f.is_file()}


def _zip_files(path: Path) -> dict[str, bytes] | None:
    try:
        with zipfile.ZipFile(path) as zf:
            return {name: zf.read(name) for name in zf.namelist()}
    except (OSError, zipfile.BadZipFile):
        return None


def skill_zips(out_dir: Path) -> list[Path]:
    """Zips for apps that take an upload (ChatGPT web, Claude chat)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for flavor in ("claude", "gpt"):
        path = out_dir / f"llm-hub-{flavor}-skill.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in _skill_files(flavor).items():
                zf.writestr(name, data)
        made.append(path)
    return made


def skill_zip_dir(home: Path) -> Path:
    return home / ".llm-hub" / "skills"


def claude_chat_skill(home: Path, dry_run: bool, uninstall: bool) -> Step:
    """The Chat tab only takes skills uploaded to the account, so setup builds the zip and says where to upload it."""
    target = "skill · Claude Chat tab"
    app_dir = home / "Library" / "Application Support" / "Claude"
    out = skill_zip_dir(home)
    path = out / "llm-hub-claude-skill.zip"
    if not app_dir.is_dir():
        return Step(target, "skipped", f"{app_dir} not found")
    if uninstall:
        existed = out.exists()
        if existed and not dry_run:
            shutil.rmtree(out)
        return Step(target, "removed" if existed else "absent", "also delete it in Claude → Settings → Capabilities → Skills")
    if _zip_files(path) == _skill_files("claude"):
        return Step(target, "present", f"{path} (upload it in Claude → Settings → Capabilities → Skills if you haven't)")
    if not dry_run:
        skill_zips(out)
    return Step(target, "upload", f"upload {path} in Claude → Settings → Capabilities → Skills")


# ---------- Claude Code ----------

def claude_code(command: str, claude_bin: str | None, dry_run: bool, uninstall: bool) -> Step:
    target = "MCP · Claude Code"
    if not claude_bin:
        return Step(target, "skipped", "`claude` CLI not found")

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([claude_bin, "mcp", *args], capture_output=True, text=True, timeout=60)

    current = run("get", SERVER)
    exists = current.returncode == 0
    if uninstall:
        if exists and not dry_run:
            run("remove", SERVER, "-s", "user")
        return Step(target, "removed" if exists else "absent", "user scope")
    if exists and command in current.stdout and "serve --agent claude" in current.stdout:
        return Step(target, "present", "user scope")
    if dry_run:
        return Step(target, "updated" if exists else "added", "user scope")
    if exists:
        run("remove", SERVER, "-s", "user")
    added = run("add", "--scope", "user", SERVER, "--", command, "serve", "--agent", "claude")
    if added.returncode != 0:
        return Step(target, "failed", (added.stderr or added.stdout).strip()[:200])
    return Step(target, "updated" if exists else "added", "user scope")


# ---------- Claude desktop ----------

def claude_desktop_running() -> bool:
    return bool(desktop.pids())


def claude_desktop(
    home: Path, command: str, dry_run: bool, uninstall: bool, running: bool = False, loaded: bool | None = None
) -> Step:
    """Write the mcpServers entry, or, while Claude runs, report it as pending: the caller then starts
    `desktop.finish`, which writes it the moment Claude quits. `loaded` says whether the running Claude has
    llm-hub connected (read from its log when None); an entry that's in the file but not loaded is pending too,
    because Claude may write its in-memory copy back over it."""
    target = "MCP · Claude desktop"
    app_dir = home / "Library" / "Application Support" / "Claude"
    path = app_dir / "claude_desktop_config.json"
    if not app_dir.is_dir():
        return Step(target, "skipped", f"{app_dir} not found")
    config = json.loads(path.read_text()) if path.exists() and path.read_text().strip() else {}
    servers = config.setdefault("mcpServers", {})
    wanted = {"command": command, "args": ["serve", "--agent", "claude"]}
    if uninstall:
        if SERVER not in servers:
            return Step(target, "absent", str(path))
        del servers[SERVER]
        status = "removed"
    elif servers.get(SERVER) == wanted:
        if running and not (desktop.load_state(home)[0] == "connected" if loaded is None else loaded):
            return Step(target, "pending", "in the config but not loaded yet: quit Claude (Cmd+Q) and it reopens with it")
        return Step(target, "present", str(path))
    else:
        status = "updated" if SERVER in servers else "added"
        servers[SERVER] = wanted
    if running:
        what = "removed" if uninstall else "added"
        return Step(target, "pending", f"{what} when you quit Claude (Cmd+Q); it reopens by itself")
    if not dry_run:
        _backup(path, dry_run)
        path.write_text(json.dumps(config, indent=2) + "\n")
    return Step(target, status, str(path))


# ---------- ChatGPT desktop / Codex ----------

def codex(home: Path, command: str, dry_run: bool, uninstall: bool) -> Step:
    target = "MCP · ChatGPT/Codex"
    path = home / ".codex" / "config.toml"
    if not path.parent.is_dir():
        return Step(target, "skipped", f"{path.parent} not found")
    text = path.read_text() if path.exists() else ""
    current = tomllib.loads(text).get("mcp_servers", {}).get(SERVER) if text else None
    block = f'[mcp_servers.{SERVER}]\ncommand = {json.dumps(command)}\nargs = ["serve", "--agent", "gpt"]\n'
    if uninstall:
        if current is None:
            return Step(target, "absent", str(path))
        new, status = CODEX_BLOCK.sub("", text).rstrip() + "\n", "removed"
    elif current and current.get("command") == command and current.get("args") == ["serve", "--agent", "gpt"]:
        return Step(target, "present", str(path))
    elif current is not None:
        new, status = CODEX_BLOCK.sub(lambda _: block + "\n", text), "updated"
    else:
        new, status = (text.rstrip() + "\n\n" if text.strip() else "") + block, "added"
    tomllib.loads(new)  # never write a config the app can't parse
    if not dry_run:
        _backup(path, dry_run)
        path.write_text(new)
    return Step(target, status, str(path))


def run_setup(
    home: Path,
    command: str,
    claude_bin: str | None,
    *,
    dry_run: bool = False,
    uninstall: bool = False,
    claude_running: bool | None = None,
    claude_loaded: bool | None = None,
) -> list[Step]:
    running = claude_desktop_running() if claude_running is None else claude_running
    steps = skills(home, dry_run, uninstall)
    for target, fn in (
        ("skill · Claude Chat tab", lambda: claude_chat_skill(home, dry_run, uninstall)),
        ("MCP · Claude Code", lambda: claude_code(command, claude_bin, dry_run, uninstall)),
        ("MCP · Claude desktop", lambda: claude_desktop(home, command, dry_run, uninstall, running, claude_loaded)),
        ("MCP · ChatGPT/Codex", lambda: codex(home, command, dry_run, uninstall)),
    ):
        try:
            steps.append(fn())
        except Exception as exc:  # one broken app config shouldn't stop the others
            steps.append(Step(target, "failed", f"{type(exc).__name__}: {exc}"[:200]))
    return steps
