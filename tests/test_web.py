import json
import os
import plistlib
import threading

import pytest
from starlette.testclient import TestClient

from llm_hub.setup import app_launcher
from llm_hub.store import Hub, HubError
from llm_hub.web.app import COOKIE, AppConfig, create_app

TOKEN = "s3cret-token"
BASE = "http://127.0.0.1:8770"


@pytest.fixture
def hub(tmp_path):
    return Hub.init(tmp_path / "repo" / "llm-hub")


def make_client(config: AppConfig, signed_in: bool = True) -> TestClient:
    client = TestClient(create_app(config), base_url=BASE)
    if signed_in:
        client.cookies.set(COOKIE, TOKEN)
    return client


@pytest.fixture
def client(hub):
    return make_client(AppConfig(resolve=lambda: hub.root, token=TOKEN, poll_interval=0.05))


# ---------- security ----------

def test_api_needs_the_session_cookie(hub):
    anon = make_client(AppConfig(resolve=lambda: hub.root, token=TOKEN), signed_in=False)
    assert anon.get("/api/threads").status_code == 401
    assert anon.get("/api/ping").json() == {"app": "llm-hub"}  # no data, so no auth


def test_open_trades_the_token_for_a_cookie(hub):
    anon = make_client(AppConfig(resolve=lambda: hub.root, token=TOKEN), signed_in=False)
    assert anon.get("/open?t=wrong", follow_redirects=False).status_code == 401
    resp = anon.get(f"/open?t={TOKEN}", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/"
    cookie = resp.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert anon.get("/api/threads").status_code == 200


def test_rejects_other_hosts_and_cross_site_origins(client):
    assert client.get("/api/threads", headers={"host": "evil.example:8770"}).status_code == 403
    assert client.get("/", headers={"host": "evil.example"}).status_code == 403
    resp = client.post("/api/threads", json={"title": "x", "body": "y", "to": "claude"},
                       headers={"origin": "https://evil.example"})
    assert resp.status_code == 403
    assert client.get("/api/threads", headers={"origin": BASE}).status_code == 200


# ---------- threads ----------

def test_index_is_versioned_and_locked_down(client):
    resp = client.get("/")
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
    assert resp.headers["cache-control"] == "no-store"
    assert "app.js?v=" in resp.text and "__VERSION__" not in resp.text


def test_state_reports_the_hub(client, hub):
    state = client.get("/api/state").json()
    assert state["hub"]["root"] == str(hub.root)
    assert state["hub"]["agents"] == ["claude", "gpt"]
    assert "note" in state["post_types"]


def test_state_without_a_hub(tmp_path):
    def missing():
        raise HubError("No hub selected.")

    state = make_client(AppConfig(resolve=missing, token=TOKEN, recent_hubs=lambda: ["/a"])).get("/api/state").json()
    assert state["hub"] is None and state["recent"] == ["/a"]


def test_create_list_read_reply_resolve(client, hub):
    resp = client.post("/api/threads", json={"title": "Pick a DB", "body": "Postgres or SQLite?", "to": "claude",
                                             "tags": "db, storage"})
    assert resp.status_code == 201
    tid = resp.json()["id"]

    hub.reply("claude", tid, "Postgres.", "human", "answer", expected_revision=1)

    [item] = client.get("/api/threads").json()["threads"]
    assert item["id"] == tid and item["awaiting"] == "human" and item["tags"] == ["db", "storage"]
    assert item["unread"] == 1 and item["last"]["author"] == "claude"

    thread = client.get(f"/api/threads/{tid}").json()
    assert [p["author"] for p in thread["posts"]] == ["human", "claude"]
    assert thread["first_unread"] == "P-002"
    assert client.get("/api/threads").json()["threads"][0]["unread"] == 0  # reading marks it read

    resp = client.post(f"/api/threads/{tid}/posts", json={"body": "Why?", "hand_to": "claude",
                                                         "expected_revision": thread["revision"]})
    assert resp.status_code == 201 and resp.json()["post"]["type"] == "note"
    revision = resp.json()["revision"]

    assert client.post(f"/api/threads/{tid}/resolve", json={"decision": "Postgres", "expected_revision": revision}).status_code == 200
    assert hub.get(tid).meta["status"] == "resolved"
    assert client.get(f"/api/threads/{tid}").json()["decision"] == "Postgres"


def test_close_without_a_decision(client, hub):
    tid = client.post("/api/threads", json={"title": "T", "body": "B", "to": "claude"}).json()["id"]
    assert client.get(f"/api/threads/{tid}").json()["decision"] is None  # open threads have none
    assert client.post(f"/api/threads/{tid}/resolve", json={"decision": "  "}).status_code == 200
    thread = hub.get(tid)
    assert thread.meta["status"] == "resolved" and thread.posts[-1].type == "decision"
    assert thread.posts[-1].body  # agents still see a closing post
    assert client.get(f"/api/threads/{tid}").json()["decision"] is None


def test_reply_refuses_to_post_over_unseen_news(client, hub):
    tid = client.post("/api/threads", json={"title": "T", "body": "B", "to": "claude"}).json()["id"]
    seen = client.get(f"/api/threads/{tid}").json()["revision"]
    hub.reply("claude", tid, "Meanwhile…", "human", "answer", expected_revision=seen)
    resp = client.post(f"/api/threads/{tid}/posts", json={"body": "Hi", "hand_to": "claude", "expected_revision": seen})
    assert resp.status_code == 409 and resp.json()["code"] == "changed"
    assert len(hub.get(tid).posts) == 2


def test_rule_violations_come_back_as_messages(client):
    resp = client.post("/api/threads", json={"title": "", "body": "", "to": "claude"})
    assert resp.status_code == 400 and "required" in resp.json()["error"]
    assert client.get("/api/threads/T-0099").status_code == 400


def test_search(client):
    client.post("/api/threads", json={"title": "Retry policy", "body": "exponential backoff?", "to": "gpt"})
    client.post("/api/threads", json={"title": "Pick a DB", "body": "Postgres?", "to": "gpt"})
    assert [t["title"] for t in client.get("/api/threads?q=backoff").json()["threads"]] == ["Retry policy"]


def test_events_announce_changed_threads(hub):
    tid = hub.create_thread("human", "T", "B", "claude", "question").id
    client = make_client(AppConfig(resolve=lambda: hub.root, token=TOKEN, poll_interval=0.05, max_events=1))
    # The test client returns only once the stream ends, so the agent posts from another thread meanwhile.
    agent = threading.Timer(0.3, lambda: hub.reply("claude", tid, "Answer", "human", "answer", expected_revision=1))
    agent.start()
    text = client.get("/api/events").text
    agent.join()
    events = [line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith("event:")]
    assert events == ["hello", "change"]
    change = [line for line in text.splitlines() if line.startswith("data:")][-1]
    assert json.loads(change[5:]) == {"threads": [tid], "hub": False}


# ---------- hubs, setup, health ----------

def test_switch_hub_by_path_or_picker(hub, tmp_path):
    used = []
    config = AppConfig(resolve=lambda: hub.root, token=TOKEN, use_hub=lambda p: used.append(p) or p,
                       pick_folder=lambda: None)
    c = make_client(config)
    assert c.post("/api/hubs", json={"path": str(tmp_path)}).json() == {"root": str(tmp_path)}
    assert c.post("/api/hubs", json={"pick": True}).json() == {"cancelled": True}
    assert used == [tmp_path]


def test_health_is_cached_and_setup_clears_it(hub):
    calls = []
    config = AppConfig(resolve=lambda: hub.root, token=TOKEN,
                       health=lambda: calls.append(1) or [{"name": "x", "status": "ok", "detail": "", "fix": ""}],
                       setup=lambda: [{"target": "t", "status": "added", "detail": ""}])
    c = make_client(config)
    assert c.get("/api/health").json()["checks"][0]["status"] == "ok"
    c.get("/api/health")
    assert len(calls) == 1
    assert c.post("/api/setup").json()["steps"][0]["status"] == "added"
    c.get("/api/health")
    assert len(calls) == 2


def test_missing_features_say_so(client):
    assert client.get("/api/health").status_code == 501
    assert client.post("/api/setup").status_code == 501
    assert client.post("/api/reveal", json={"what": "anything"}).status_code == 400


# ---------- launcher ----------

def test_launcher_app_bundle(tmp_path):
    home = tmp_path / "home"
    (home / "Library").mkdir(parents=True)
    assert app_launcher(home, "/x/llm-hub", dry_run=False, uninstall=False).status == "added"
    app = home / "Applications" / "LLM Hub.app"
    script = app / "Contents" / "MacOS" / "LLM Hub"
    assert '"/x/llm-hub" app' in script.read_text() and script.stat().st_mode & 0o111
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleExecutable"] == "LLM Hub" and info["LSUIElement"] is True
    assert info["CFBundleIconFile"] == "AppIcon"
    assert (app / "Contents" / "Resources" / "AppIcon.icns").read_bytes()[:4] == b"icns"
    assert app_launcher(home, "/x/llm-hub", dry_run=False, uninstall=False).status == "present"
    assert app_launcher(home, "/y/llm-hub", dry_run=False, uninstall=False).status == "updated"
    assert app_launcher(home, "/y/llm-hub", dry_run=False, uninstall=True).status == "removed"
    assert not app.exists()
    assert app_launcher(tmp_path / "linux-home", "/x", dry_run=False, uninstall=False).status == "skipped"


# ---------- the runner ----------

class SignedIn:
    def __init__(self, ok=True):
        self.ok = ok

    def login_problem(self):
        return None if self.ok else "Claude Code isn't signed in."


def runner_client(hub, started, signed_in=True):
    agents = lambda: {"claude": SignedIn(signed_in), "gpt": SignedIn()}  # noqa: E731
    return make_client(AppConfig(resolve=lambda: hub.root, token=TOKEN, agents=agents,
                                 start_run=lambda root, tid, n: started.append((root, tid, n))))


def test_start_a_run(hub):
    from llm_hub import runner

    started = []
    c = runner_client(hub, started)
    tid = c.post("/api/threads", json={"title": "T", "body": "B", "to": "claude"}).json()["id"]
    resp = c.post(f"/api/threads/{tid}/run", json={"max_turns": 12})
    assert resp.status_code == 202 and started == [(hub.root, tid, 12)]
    assert c.post(f"/api/threads/{tid}/run", json={"max_turns": 500}).status_code == 400

    lock = runner.take_lock(hub.root, tid)  # what a live run holds
    try:
        runner.save_state(hub.root, runner.RunState(thread=tid, pid=os.getpid(), agent="claude", max_turns=12))
        run = c.get(f"/api/threads/{tid}").json()["run"]
        assert run["status"] == "running" and run["agent"] == "claude"
        assert c.get("/api/threads").json()["threads"][0]["run"]["status"] == "running"
        resp = c.post(f"/api/threads/{tid}/run", json={})
        assert resp.status_code == 400 and "already running" in resp.json()["error"]
    finally:
        os.close(lock)


def test_run_refuses_with_a_reason(hub):
    started = []
    tid = hub.create_thread("human", "T", "B", "claude", "question").id
    c = runner_client(hub, started, signed_in=False)
    resp = c.post(f"/api/threads/{tid}/run", json={})
    assert resp.status_code == 400 and "isn't signed in" in resp.json()["error"] and not started
    hub.reply("human", tid, "Mine", "human", "note")
    resp = runner_client(hub, started).post(f"/api/threads/{tid}/run", json={})
    assert resp.status_code == 400 and "your turn" in resp.json()["error"]


def test_stop_a_run(hub, client):
    tid = hub.create_thread("human", "T", "B", "claude", "question").id
    assert client.post(f"/api/threads/{tid}/run/stop").json() == {"stopping": False}
    assert client.post(f"/api/threads/{tid}/run", json={}).status_code == 501


def test_run_progress_is_a_live_update(hub):
    from llm_hub import runner

    tid = hub.create_thread("human", "T", "B", "claude", "question").id
    client = make_client(AppConfig(resolve=lambda: hub.root, token=TOKEN, poll_interval=0.05, max_events=1))
    progress = threading.Timer(0.3, lambda: runner.save_state(hub.root, runner.RunState(thread=tid, pid=1)))
    progress.start()
    text = client.get("/api/events").text
    progress.join()
    change = [line for line in text.splitlines() if line.startswith("data:")][-1]
    assert json.loads(change[5:])["threads"] == [tid]
