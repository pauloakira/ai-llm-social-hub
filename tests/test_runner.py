import json
import os
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
    runner.save_state(hub.root, RunState(thread=tid, pid=os.getpid()))
    with pytest.raises(HubError, match="already running"):
        run(hub, tid)


def test_a_run_whose_process_died_reads_as_stopped(hub):
    tid = start_thread(hub)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    runner.save_state(hub.root, RunState(thread=tid, pid=dead.pid, agent="gpt"))
    state = runner.load_state(hub.root, tid)
    assert state.status == "stopped" and state.agent is None


def test_stop_ends_a_run_and_its_agent(hub, tmp_path):
    tid = start_thread(hub)
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
    proc = subprocess.Popen([sys.executable, str(script)])
    deadline = time.time() + 20
    while time.time() < deadline:
        state = runner.load_state(hub.root, tid)
        if state and state.agent == "gpt":
            break
        time.sleep(0.1)
    assert runner.stop(hub.root, tid)
    proc.wait(timeout=20)
    state = runner.load_state(hub.root, tid)
    assert state.status == "stopped" and len(hub.get(tid).posts) == 1


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
    allowed = claude[claude.index("--allowedTools") + 1].split(",")
    assert set(allowed) == {"mcp__llm-hub__read_thread", "mcp__llm-hub__reply", "mcp__llm-hub__update_summary",
                            "mcp__llm-hub__resolve", "Read", "Grep", "Glob"}
    config = json.loads(claude[claude.index("--mcp-config") + 1])
    assert config["mcpServers"]["llm-hub"]["args"][-2:] == ["--thread", "T-0001"]
    no_files = ClaudeAgent("claude", "/bin/claude").command("PROMPT", mcp, tmp_path, files=False)
    assert no_files[no_files.index("--tools") + 1] == ""

    codex = CodexAgent("gpt", "/bin/codex").command("PROMPT", mcp, tmp_path, files=True)
    assert codex[:2] == ["/bin/codex", "exec"] and codex[-1] == "PROMPT"
    assert codex[codex.index("--sandbox") + 1] == "read-only" and "--ignore-user-config" in codex
    assert codex[codex.index("-C") + 1] == str(tmp_path)
    assert 'mcp_servers.llm-hub.command="/bin/llm-hub"' in codex and 'model="gpt-x"' in codex
