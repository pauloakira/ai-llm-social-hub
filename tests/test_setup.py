import json
import tomllib
import zipfile

import pytest

from llm_hub.setup import bundled_skills, run_setup, skill_zips

CMD = "/opt/llm-hub/bin/llm-hub"
FAKE_CLAUDE = """#!/bin/sh
state="$(dirname "$0")/state"
case "$2" in
  get) [ -f "$state" ] && cat "$state" && exit 0; echo "No MCP server found" >&2; exit 1;;
  add) shift 2; printf 'llm-hub:\\n  Command: %s\\n  Args: %s %s %s\\n' "$5" "$6" "$7" "$8" > "$state";;
  remove) rm -f "$state";;
esac
"""
CODEX_CONFIG = """model = "gpt-5"

[mcp_servers.node_repl]
command = "/bin/node_repl"

[mcp_servers.node_repl.env]
A = "1"

[desktop]
followUpQueueMode = "queue"
"""


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text(CODEX_CONFIG)
    app = home / "Library" / "Application Support" / "Claude"
    app.mkdir(parents=True)
    (app / "claude_desktop_config.json").write_text(
        json.dumps({"mcpServers": {"sqlite": {"command": "uv"}}, "preferences": {"x": True}})
    )
    return home


@pytest.fixture
def claude_bin(tmp_path):
    path = tmp_path / "bin" / "claude"
    path.parent.mkdir()
    path.write_text(FAKE_CLAUDE)
    path.chmod(0o755)
    return str(path)


def setup(home, claude_bin, **kw):
    kw.setdefault("claude_running", False)
    return {s.target: s for s in run_setup(home, CMD, claude_bin, **kw)}


def desktop_config(home):
    return json.loads((home / "Library/Application Support/Claude/claude_desktop_config.json").read_text())


def test_fresh_install_then_idempotent(home, claude_bin):
    steps = setup(home, claude_bin)
    assert {s.status for s in steps.values()} == {"added"}, steps

    assert (home / ".claude/skills/llm-hub/SKILL.md").exists()
    assert (home / ".codex/skills/llm-hub/agents/openai.yaml").exists()
    assert "You are **gpt**" in (home / ".codex/skills/llm-hub/SKILL.md").read_text()

    cfg = desktop_config(home)
    assert cfg["mcpServers"]["llm-hub"] == {"command": CMD, "args": ["serve", "--agent", "claude"]}
    assert cfg["mcpServers"]["sqlite"] == {"command": "uv"} and cfg["preferences"] == {"x": True}

    codex = tomllib.loads((home / ".codex/config.toml").read_text())
    assert codex["mcp_servers"]["llm-hub"] == {"command": CMD, "args": ["serve", "--agent", "gpt"]}
    assert codex["mcp_servers"]["node_repl"]["env"] == {"A": "1"} and codex["desktop"]

    assert (home / ".codex/config.toml.bak-llm-hub").read_text() == CODEX_CONFIG

    again = setup(home, claude_bin)
    assert {s.status for s in again.values()} == {"present"}, again


def test_new_command_path_updates_every_registration(home, claude_bin):
    setup(home, claude_bin)
    steps = {s.target: s for s in run_setup(home, "/new/llm-hub", claude_bin, claude_running=False)}
    assert steps["MCP · Claude Code"].status == "updated"
    assert steps["MCP · Claude desktop"].status == "updated"
    assert steps["MCP · ChatGPT/Codex"].status == "updated"
    text = (home / ".codex/config.toml").read_text()
    assert text.count("[mcp_servers.llm-hub]") == 1 and "/new/llm-hub" in text
    assert tomllib.loads(text)["mcp_servers"]["node_repl"]["env"] == {"A": "1"}


def test_dry_run_changes_nothing(home, claude_bin):
    before = (home / ".codex/config.toml").read_text(), desktop_config(home)
    steps = setup(home, claude_bin, dry_run=True)
    assert {s.status for s in steps.values()} == {"added"}
    assert ((home / ".codex/config.toml").read_text(), desktop_config(home)) == before
    assert not (home / ".claude/skills/llm-hub").exists()


def test_uninstall_removes_everything_and_keeps_the_rest(home, claude_bin):
    setup(home, claude_bin)
    steps = setup(home, claude_bin, uninstall=True)
    assert {s.status for s in steps.values()} == {"removed"}, steps
    assert not (home / ".claude/skills/llm-hub").exists()
    assert "llm-hub" not in desktop_config(home)["mcpServers"]
    codex = tomllib.loads((home / ".codex/config.toml").read_text())
    assert "llm-hub" not in codex["mcp_servers"] and codex["mcp_servers"]["node_repl"]["env"] == {"A": "1"}
    assert {s.status for s in setup(home, claude_bin, uninstall=True).values()} == {"absent"}


def test_running_claude_desktop_is_not_edited(home, claude_bin):
    before = desktop_config(home)
    steps = setup(home, claude_bin, claude_running=True)
    assert steps["MCP · Claude desktop"].status == "blocked"
    assert desktop_config(home) == before
    assert steps["MCP · ChatGPT/Codex"].status == "added"


def test_missing_apps_are_skipped(tmp_path):
    steps = {s.target: s for s in run_setup(tmp_path, CMD, None, claude_running=False)}
    assert {s.status for s in steps.values()} == {"skipped"}


def test_bundled_skills_and_zips(tmp_path):
    for flavor, name in (("claude", "claude"), ("gpt", "gpt")):
        assert f"You are **{name}**" in (bundled_skills() / flavor / "llm-hub" / "SKILL.md").read_text()
    claude_zip, gpt_zip = skill_zips(tmp_path / "dist")
    assert "llm-hub/SKILL.md" in zipfile.ZipFile(claude_zip).namelist()
    assert "llm-hub/agents/openai.yaml" in zipfile.ZipFile(gpt_zip).namelist()
