import json
import sys
from pathlib import Path

import pytest

from llm_hub import desktop, doctor
from llm_hub.store import Hub

CMD = "/opt/llm-hub/bin/llm-hub"
START = "2026-09-25 01:25:23 [info] Starting app {\n"


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    app = home / "Library" / "Application Support" / "Claude"
    app.mkdir(parents=True)
    (app / "claude_desktop_config.json").write_text(json.dumps({"mcpServers": {"sqlite": {"command": "uv"}}}))
    (home / "Library" / "Logs" / "Claude").mkdir(parents=True)
    monkeypatch.setattr(desktop, "pending", lambda h: None)
    return home


def add_entry(home):
    path = home / "Library/Application Support/Claude/claude_desktop_config.json"
    cfg = json.loads(path.read_text())
    cfg["mcpServers"]["llm-hub"] = {"command": CMD, "args": ["serve", "--agent", "claude"]}
    path.write_text(json.dumps(cfg))


def write_log(home, text):
    desktop.log_path(home).write_text(text)


def test_server_self_test_talks_to_the_real_server(tmp_path):
    wrapper = tmp_path / "llm-hub"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m llm_hub.cli "$@"\n')
    wrapper.chmod(0o755)
    check = doctor.server_self_test(str(wrapper))
    assert check.status == "ok", check
    assert "8 tools" in check.detail


def test_server_self_test_reports_a_broken_command(tmp_path):
    broken = tmp_path / "llm-hub"
    broken.write_text("#!/bin/sh\necho 'ModuleNotFoundError: No module named mcp' >&2\nexit 1\n")
    broken.chmod(0o755)
    check = doctor.server_self_test(str(broken))
    assert check.status == "fail" and "ModuleNotFoundError" in check.detail and check.fix


def test_claude_desktop_missing_entry(home, monkeypatch):
    monkeypatch.setattr(desktop, "pids", lambda: [1])
    check = doctor.claude_desktop_mcp(home, CMD)
    assert check.status == "fail" and "llm-hub setup" in check.fix


def test_claude_desktop_missing_entry_while_setup_waits(home, monkeypatch):
    monkeypatch.setattr(desktop, "pending", lambda h: 99)
    check = doctor.claude_desktop_mcp(home, CMD)
    assert check.status == "warn" and "Cmd+Q" in check.detail


def test_claude_desktop_connected(home, monkeypatch):
    add_entry(home)
    monkeypatch.setattr(desktop, "pids", lambda: [1])
    write_log(home, START + "x [info] [LocalMcpServerManager] Connected to llm-hub (8 tools)\n")
    check = doctor.claude_desktop_mcp(home, CMD)
    assert (check.status, check.detail) == ("ok", "connected (8 tools)")


def test_claude_desktop_running_without_loading_it(home, monkeypatch):
    add_entry(home)
    monkeypatch.setattr(desktop, "pids", lambda: [1])
    write_log(home, START)
    check = doctor.claude_desktop_mcp(home, CMD)
    assert check.status == "warn" and "Cmd+Q" in check.fix


def test_claude_desktop_server_launched_but_never_connected(home, monkeypatch):
    add_entry(home)
    monkeypatch.setattr(desktop, "pids", lambda: [1])
    write_log(home, START + "x [info] Launching MCP Server: llm-hub\n")
    assert doctor.claude_desktop_mcp(home, CMD).status == "fail"


def test_claude_desktop_closed(home, monkeypatch):
    add_entry(home)
    monkeypatch.setattr(desktop, "pids", lambda: [])
    assert doctor.claude_desktop_mcp(home, CMD).status == "ok"


def test_current_hub(tmp_path):
    root = Hub.init(tmp_path / "repo" / "llm-hub").root
    assert doctor.current_hub(lambda: root).status == "ok"
    check = doctor.current_hub(lambda: tmp_path / "nowhere" / "llm-hub")
    assert check.status == "fail" and "llm-hub init" in check.fix


def test_no_permanent_install():
    [check] = doctor.run_doctor(Path("/nonexistent"), None, None, lambda: None)
    assert check.status == "fail" and "uv tool install" in check.fix
