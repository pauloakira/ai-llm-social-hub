import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from llm_hub import runner
from llm_hub.runner import Agent, ClaudeAgent, CodexAgent, RunState
from llm_hub.store import Hub, HubError

# Stands in for `claude -p` / `codex exec`: takes one turn through the store, the way the real agents do via MCP.
FAKE_TURN = textwrap.dedent("""
    import sys, time
    from pathlib import Path
    from llm_hub.store import Hub
    root, tid, me, mode = sys.argv[1:5]
    h = Hub(Path(root)); t = h.get(tid)
    other = "gpt" if me == "claude" else "claude"
    if mode == "silent":
        sys.exit(0)
    if mode == "auth":
        print("Not logged in · Please run /login"); sys.exit(1)
    if mode == "slow":
        time.sleep(30)
    if mode == "resolve-at-4" and len(t.posts) >= 4:
        h.resolve(me, tid, "Done: we agree.", expected_revision=t.revision)
    elif mode == "ask-human":
        h.reply(me, tid, "Which option do you prefer?", "human", "question", expected_revision=t.revision)
    else:
        h.reply(me, tid, f"{me} answers", other, "answer", expected_revision=t.revision)
""")


class FakeAgent(Agent):
    def __init__(self, name, root, mode="ping-pong", signed_in=True):
        super().__init__(name, sys.executable)
        self.root, self.mode, self.signed_in = root, mode, signed_in
        self.prompts = []

    def command(self, prompt, mcp_cmd, repo, files):
        self.prompts.append(prompt)
        tid = mcp_cmd[mcp_cmd.index("--thread") + 1]
        return [sys.executable, "-c", FAKE_TURN, str(self.root), tid, self.name, self.mode]

    def login_problem(self):
        return None if self.signed_in else f"{self.name} isn't signed in."


@pytest.fixture
def hub(tmp_path):
    return Hub.init(tmp_path / "repo" / "llm-hub")


def start_thread(hub, to="gpt"):
    return hub.create_thread("human", "Pick a DB", "Postgres or SQLite?", to, "question").id


def run(hub, tid, modes=("ping-pong", "ping-pong"), max_turns=20, **kw):
    agents = {name: FakeAgent(name, hub.root, mode) for name, mode in zip(("claude", "gpt"), modes)}
    alerts = []
    state = runner.run(hub.root, tid, max_turns, mcp_command="llm-hub", agents=agents, alert=alerts.append, **kw)
    return state, agents, alerts


def test_agents_take_turns_until_the_budget_hands_it_to_the_human(hub):
    tid = start_thread(hub)
    state, agents, alerts = run(hub, tid, max_turns=3)
    thread = hub.get(tid)
    assert [p.author for p in thread.posts] == ["human", "gpt", "claude", "gpt"]
    assert thread.meta["awaiting"] == "human"
    assert (state.status, state.posts) == ("done", 3) and "3 turns" in state.reason
    assert alerts == [f"{tid}: {state.reason}"]
    assert runner.load_state(hub.root, tid).status == "done"
    assert "read_thread('T-0001')" in agents["gpt"].prompts[0] and "read-only" in agents["gpt"].prompts[0]


def test_stops_when_an_agent_closes_the_thread(hub):
    tid = start_thread(hub)
    state, _, _ = run(hub, tid, modes=("resolve-at-4", "resolve-at-4"))
    assert hub.get(tid).meta["status"] == "resolved"
    assert state.status == "done" and state.reason.startswith("Closed by")


def test_stops_when_an_agent_needs_the_human(hub):
    tid = start_thread(hub)
    state, _, _ = run(hub, tid, modes=("ping-pong", "ask-human"))
    assert state.posts == 1 and state.reason == "GPT handed it to you."


def test_an_agent_that_never_posts_fails_the_run_after_two_tries(hub):
    tid = start_thread(hub)
    state, agents, _ = run(hub, tid, modes=("ping-pong", "silent"))
    assert state.status == "failed" and "GPT didn't post" in state.reason
    assert len(agents["gpt"].prompts) == 2
    assert "--- gpt" in runner.log_path(hub.root, tid).read_text()


def test_a_sign_in_error_fails_at_once_with_how_to_fix_it(hub):
    tid = start_thread(hub)
    state, agents, _ = run(hub, tid, modes=("ping-pong", "auth"))
    assert state.status == "failed" and len(agents["gpt"].prompts) == 1


def test_files_off_tells_the_agent(hub):
    tid = start_thread(hub)
    _, agents, _ = run(hub, tid, modes=("ping-pong", "ask-human"), files=False)
    assert "can't read the project's files" in agents["gpt"].prompts[0]


@pytest.mark.parametrize("setup, message", [
    (lambda h, t: h.reply("human", t, "Over to me", "human", "note"), "your turn"),
    (lambda h, t: h.resolve("human", t, "Done"), "closed"),
])
def test_preflight_refuses_threads_that_are_not_an_agents_turn(hub, setup, message):
    tid = start_thread(hub)
    setup(hub, tid)
    with pytest.raises(HubError, match=message):
        run(hub, tid)


def test_preflight_checks_sign_in_before_changing_anything(hub):
    tid = start_thread(hub)
    revision = hub.get(tid).revision
    agents = {"claude": FakeAgent("claude", hub.root), "gpt": FakeAgent("gpt", hub.root, signed_in=False)}
    with pytest.raises(HubError, match="gpt isn't signed in"):
        runner.run(hub.root, tid, 5, mcp_command="llm-hub", agents=agents, alert=lambda m: None)
    assert hub.get(tid).revision == revision and runner.load_state(hub.root, tid) is None


def test_only_one_run_per_thread(hub):
    tid = start_thread(hub)
    lock = runner.take_lock(hub.root, tid)
    try:
        # the lock alone decides, even before the first run has written its state
        with pytest.raises(HubError, match="already running"):
            run(hub, tid)
        assert runner.take_lock(hub.root, tid) is None
    finally:
        os.close(lock)
    assert runner.runner_pid(hub.root, tid) is None
    state, _, _ = run(hub, tid, modes=("ping-pong", "ask-human"))
    assert state.status == "done"


def test_a_second_start_waits_for_the_first_to_hold_the_lock(hub, tmp_path):
    """Two starts in a row (a double click): the first holds the lock before start_detached returns, so the second
    one's checks see it running."""
    tid = start_thread(hub)
    script = slow_run_script(hub, tid, tmp_path)
    command = tmp_path / "llm-hub"
    command.write_text(f"#!/bin/sh\nexec {sys.executable} {script}\n")
    command.chmod(0o755)
    pid = runner.start_detached(str(command), hub.root, tid, 5)
    try:
        assert runner.runner_pid(hub.root, tid) == pid
        with pytest.raises(HubError, match="already running"):
            runner.preflight(hub.root, tid, {n: FakeAgent(n, hub.root) for n in ("claude", "gpt")})
    finally:
        wait_for(lambda: (s := runner.load_state(hub.root, tid)) and s.agent_pgid)
        assert runner.stop(hub.root, tid)
        wait_for(lambda: runner.runner_pid(hub.root, tid) is None)


def test_start_detached_reports_why_a_run_didnt_start(hub, tmp_path):
    tid = start_thread(hub)
    command = tmp_path / "llm-hub"
    command.write_text("#!/bin/sh\necho 'error: Codex isn'\\''t signed in.'; exit 1\n")
    command.chmod(0o755)
    with pytest.raises(HubError, match="Codex isn't signed in"):
        runner.start_detached(str(command), hub.root, tid, 5)


def test_a_run_whose_process_died_reads_as_stopped(hub):
    tid = start_thread(hub)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    runner.save_state(hub.root, RunState(thread=tid, pid=dead.pid, agent="gpt"))
    state = runner.load_state(hub.root, tid)
    assert state.status == "stopped" and state.agent is None


def slow_run_script(hub, tid, tmp_path):
    """A script that runs the thread with agents that take 30 seconds a turn."""
    script = tmp_path / "run.py"
    script.write_text(textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(Path(__file__).parent)!r})
        from test_runner import FakeAgent
        from llm_hub import runner
        root = {str(hub.root)!r}
        agents = {{n: FakeAgent(n, root, "slow") for n in ("claude", "gpt")}}
        runner.run(root, {tid!r}, 5, mcp_command="llm-hub", agents=agents, alert=lambda m: None)
    """))
    return script


def wait_for(condition, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if result := condition():
            return result
        time.sleep(0.05)
    raise AssertionError("timed out")


def agent_turn_started(hub, tid):
    return wait_for(lambda: (s := runner.load_state(hub.root, tid)) and s.agent == "gpt" and s.agent_pgid and s)


def test_stop_ends_a_run_and_its_agent(hub, tmp_path):
    tid = start_thread(hub)
    proc = subprocess.Popen([sys.executable, str(slow_run_script(hub, tid, tmp_path))])
    agent_turn_started(hub, tid)
    assert runner.stop(hub.root, tid)
    proc.wait(timeout=20)
    state = runner.load_state(hub.root, tid)
    assert state.status == "stopped" and len(hub.get(tid).posts) == 1


def test_a_stop_while_the_agent_is_starting_still_stops_it(hub, monkeypatch):
    """SIGTERM between starting the agent and recording it used to be missed: the run then waited out the turn."""
    tid = start_thread(hub)
    real_popen = subprocess.Popen

    def popen(args, *a, **kw):
        proc = real_popen(args, *a, **kw)
        if FAKE_TURN in args:
            os.kill(os.getpid(), signal.SIGTERM)  # the handler runs here, before the run has recorded the agent
        return proc

    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    started = time.monotonic()
    state, _, _ = run(hub, tid, modes=("slow", "slow"), turn_timeout=25)
    assert time.monotonic() - started < 10
    assert state.status == "stopped" and len(hub.get(tid).posts) == 1


def test_an_agent_left_behind_by_a_killed_run_can_be_stopped(hub, tmp_path):
    tid = start_thread(hub)
    proc = subprocess.Popen([sys.executable, str(slow_run_script(hub, tid, tmp_path))])
    pgid = agent_turn_started(hub, tid).agent_pgid
    proc.kill()  # the run dies without a chance to clean up; its agent keeps going
    proc.wait()
    state = runner.load_state(hub.root, tid)
    assert state.status == "running" and "still taking its turn" in state.reason
    with pytest.raises(HubError, match="still taking its turn"):
        run(hub, tid)
    assert runner.stop(hub.root, tid)
    wait_for(lambda: runner.agent_group(RunState(thread=tid, agent_pgid=pgid)) is None)
    state = runner.load_state(hub.root, tid)
    assert state.status == "stopped" and len(hub.get(tid).posts) == 1


def test_a_reused_process_group_is_never_taken_for_the_agent(hub):
    tid = start_thread(hub)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        assert runner.agent_group(RunState(thread=tid, agent_pgid=other.pid)) is None
    finally:
        other.kill()
        other.wait()


def test_allow_turns_sets_the_budget_from_the_posts_so_far(hub):
    tid = start_thread(hub)
    hub.reply("gpt", tid, "Hi", "claude", "answer", expected_revision=1)
    thread = hub.allow_turns(tid, 20)
    assert thread.meta["max_turns"] == 21
    with pytest.raises(HubError):
        hub.allow_turns(tid, 0)


def test_real_agent_commands_are_locked_down(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "codex_model_settings", lambda: {"model": "gpt-x", "model_reasoning_effort": "high"})
    mcp = ["/bin/llm-hub", "--root", "/r/llm-hub", "serve", "--agent", "claude", "--thread", "T-0001"]
    claude = ClaudeAgent("claude", "/bin/claude").command("PROMPT", mcp, tmp_path, files=True)
    assert "--strict-mcp-config" in claude and claude[claude.index("--tools") + 1] == "Read,Grep,Glob"
    # Read/Grep/Glob must not be pre-approved: approved, they'd read the whole disk instead of the project
    allowed = claude[claude.index("--allowedTools") + 1].split(",")
    assert set(allowed) == {"mcp__llm-hub__read_thread", "mcp__llm-hub__reply", "mcp__llm-hub__update_summary",
                            "mcp__llm-hub__resolve"}
    assert claude[claude.index("--permission-mode") + 1] == "dontAsk"
    assert claude[claude.index("--setting-sources") + 1] == ""
    config = json.loads(claude[claude.index("--mcp-config") + 1])
    assert config["mcpServers"]["llm-hub"]["args"][-2:] == ["--thread", "T-0001"]
    no_files = ClaudeAgent("claude", "/bin/claude").command("PROMPT", mcp, tmp_path, files=False)
    assert no_files[no_files.index("--tools") + 1] == ""

    codex = CodexAgent("gpt", "/bin/codex").command("PROMPT", mcp, tmp_path, files=True)
    assert codex[:2] == ["/bin/codex", "exec"] and codex[-1] == "PROMPT"
    assert "--sandbox" not in codex and "--ignore-user-config" in codex
    assert 'default_permissions="llm_hub"' in codex and 'shell_environment_policy.inherit="core"' in codex
    assert f'permissions.llm_hub.filesystem={{":minimal"="read", {json.dumps(str(tmp_path))}="read"}}' in codex
    assert codex[codex.index("-C") + 1] == str(tmp_path)
    assert 'mcp_servers.llm-hub.command="/bin/llm-hub"' in codex and 'model="gpt-x"' in codex


def test_codex_without_files_reads_only_an_empty_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_HUB_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(runner, "codex_model_settings", lambda: {})
    codex = CodexAgent("gpt", "/bin/codex").command("PROMPT", ["/bin/llm-hub"], tmp_path / "repo", files=False)
    empty = tmp_path / "state" / "no-files"
    assert codex[codex.index("-C") + 1] == str(empty) and empty.is_dir() and not any(empty.iterdir())
    assert str(tmp_path / "repo") not in " ".join(codex)


def test_agents_get_a_minimal_environment(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    claude, codex = ClaudeAgent("claude", None).env(), CodexAgent("gpt", None).env()
    assert "GITHUB_TOKEN" not in claude and "GITHUB_TOKEN" not in codex  # the user's other secrets stay out
    assert claude["ANTHROPIC_API_KEY"] == "sk-ant" and "OPENAI_API_KEY" not in claude  # each gets only its own
    assert codex["OPENAI_API_KEY"] == "sk-openai" and "ANTHROPIC_API_KEY" not in codex
    assert "AWS_SECRET_ACCESS_KEY" not in codex
    for env in (claude, codex):
        assert env["HOME"] == os.environ["HOME"] and env["PATH"] == os.environ["PATH"] and env["LC_ALL"]


def test_agent_env_drops_a_host_claude_sessions_variables(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://host-app")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/custom")
    monkeypatch.delenv("CLAUDECODE", raising=False)
    env = ClaudeAgent("claude", None).env()
    assert env["ANTHROPIC_BASE_URL"] == "http://host-app" and env["CLAUDE_CONFIG_DIR"] == "/custom"  # the user's own
    assert "CLAUDE_CODE_SESSION_ID" not in env
    monkeypatch.setenv("CLAUDECODE", "1")
    env = ClaudeAgent("claude", None).env()
    assert "ANTHROPIC_BASE_URL" not in env and "CLAUDE_CONFIG_DIR" not in env and "CLAUDECODE" not in env
    assert env["HOME"] == os.environ["HOME"]
