import multiprocessing

import pytest

from llm_hub.store import Hub, HubError, parse_thread


@pytest.fixture
def hub(tmp_path):
    return Hub.init(tmp_path / "llm-hub", max_turns=4)


def test_thread_round_trips_through_markdown(hub):
    t = hub.create_thread("claude", "Retry policy", "Proposal body", "gpt", tags=["ingestion"])
    hub.reply("gpt", t.id, "Looks good\n### P-9 · fake header\n## Posts", "claude", "critique", re_="P-001")
    hub.update_summary("claude", t.id, "- **Agreed**: backoff\n## Summary inside")

    text = t.path.read_text()
    assert "\\### P-9" in text and "\\## Posts" in text  # user text can't break the structure

    again = hub.get(t.id)
    assert [p.id for p in again.posts] == ["P-001", "P-002"]
    assert again.posts[1].body == "Looks good\n### P-9 · fake header\n## Posts"
    assert again.posts[1].re == "P-001"
    assert again.summary == "- **Agreed**: backoff\n## Summary inside"
    assert parse_thread(again.render()).render() == again.render()


def test_turn_taking_is_enforced(hub):
    t = hub.create_thread("claude", "Topic", "body", "gpt")
    with pytest.raises(HubError, match="gpt's turn"):
        hub.reply("claude", t.id, "again", "gpt")
    with pytest.raises(HubError, match="yourself"):
        hub.reply("gpt", t.id, "x", "gpt")
    with pytest.raises(HubError, match="Unknown hand_to"):
        hub.reply("gpt", t.id, "x", "gemini")
    hub.reply("human", t.id, "the human can always post", "claude")
    assert hub.get(t.id).meta["awaiting"] == "claude"


def test_turn_budget_hands_to_human_and_human_resets_it(hub):
    t = hub.create_thread("claude", "Topic", "1", "gpt")  # turn 1
    hub.reply("gpt", t.id, "2", "claude")
    hub.reply("claude", t.id, "3", "gpt")
    _, post, notes = hub.reply("gpt", t.id, "4", "claude")
    assert post.hand_to == "human" and notes
    assert hub.get(t.id).meta["awaiting"] == "human"

    hub.reply("human", t.id, "carry on", "claude")
    assert hub.get(t.id).meta["turns"] == 0


def test_resolve_and_reopen(hub):
    t = hub.create_thread("claude", "Topic", "body", "gpt")
    hub.resolve("gpt", t.id, "We go with option B.")
    t = hub.get(t.id)
    assert t.meta["status"] == "resolved" and t.posts[-1].type == "decision"
    with pytest.raises(HubError, match="resolved"):
        hub.reply("claude", t.id, "one more thing", "gpt")
    _, _, notes = hub.reply("human", t.id, "reopening", "claude")
    assert hub.get(t.id).meta["status"] == "open" and notes


def test_inbox_and_unread(hub):
    a = hub.create_thread("claude", "A", "for gpt", "gpt")
    b = hub.create_thread("gpt", "B", "for human", "human")
    my_turn, fyi = hub.inbox("gpt")
    assert [t.id for t in my_turn] == [a.id] and fyi == []

    my_turn, fyi = hub.inbox("claude")
    assert my_turn == [] and fyi == []  # claude isn't in B

    thread, posts = hub.read("gpt", a.id)
    assert [p.id for p in posts] == ["P-001"]
    hub.reply("gpt", a.id, "answer", "human")
    _, posts = hub.read("gpt", a.id)
    assert posts == []  # own posts don't count as unread

    my_turn, fyi = hub.inbox("claude")
    assert [(t.id, n) for t, n in fyi] == [(a.id, 1)]
    assert hub.search("for human")[0][0].id == b.id


def _post_many(root, agent, tid, n):
    hub = Hub(root)
    for i in range(n):
        hub.reply("human", tid, f"{agent} {i}", "claude")


def test_concurrent_writers_do_not_lose_posts(hub):
    t = hub.create_thread("human", "Busy", "start", "claude")
    procs = [multiprocessing.Process(target=_post_many, args=(hub.root, f"w{i}", t.id, 15)) for i in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    posts = hub.get(t.id).posts
    assert len(posts) == 61
    assert [p.id for p in posts] == [f"P-{i:03d}" for i in range(1, 62)]


@pytest.mark.parametrize(
    "remote, url",
    [
        ("git@github.com:pauloakira/ai-llm-social-hub.git", "https://github.com/pauloakira/ai-llm-social-hub"),
        ("git@gitlab.com:mahhp/superlyfe/data.git\n", "https://gitlab.com/mahhp/superlyfe/data"),
        ("ssh://git@github.com/o/r.git", "https://github.com/o/r"),
        ("https://user:tok@github.com/o/r.git", "https://github.com/o/r"),
        ("https://github.com/o/r", "https://github.com/o/r"),
    ],
)
def test_web_url(remote, url):
    from llm_hub.store import web_url

    assert web_url(remote) == url


def test_repo_url_from_git_origin_or_config(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", "git@github.com:o/r.git"], check=True)
    hub = Hub.init(tmp_path / "llm-hub")
    assert hub.repo_url == "https://github.com/o/r"

    (hub.root / "hub.yaml").write_text("repo_url: ''\n")
    assert Hub(hub.root).repo_url is None  # explicitly hidden
