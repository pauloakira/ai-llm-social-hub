import multiprocessing

import pytest

from llm_hub.store import Hub, HubError, parse_thread


@pytest.fixture
def hub(tmp_path):
    return Hub.init(tmp_path / "llm-hub", max_turns=4)


def rev(hub, tid):
    return hub.get(tid).revision


def test_thread_round_trips_through_markdown(hub):
    t = hub.create_thread("claude", "Retry policy", "Proposal body", "gpt", tags=["ingestion"])
    hub.reply("gpt", t.id, "Looks good\n### P-9 · fake header\n## Posts", "claude", "critique", re_="P-001",
              expected_revision=1)
    hub.update_summary("claude", t.id, "- **Agreed**: backoff\n## Summary inside", expected_revision=2)

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
    hub.reply("gpt", t.id, "2", "claude", expected_revision=1)
    hub.reply("claude", t.id, "3", "gpt", expected_revision=2)
    _, post, notes = hub.reply("gpt", t.id, "4", "claude", expected_revision=3)
    assert post.hand_to == "human" and notes
    assert hub.get(t.id).meta["awaiting"] == "human"

    hub.reply("human", t.id, "carry on", "claude")
    assert hub.get(t.id).meta["turns"] == 0


def test_resolve_and_reopen(hub):
    t = hub.create_thread("claude", "Topic", "body", "gpt")
    hub.resolve("gpt", t.id, "We go with option B.", expected_revision=1)
    t = hub.get(t.id)
    assert t.meta["status"] == "resolved" and t.posts[-1].type == "decision"
    with pytest.raises(HubError, match="resolved"):
        hub.reply("claude", t.id, "one more thing", "gpt", expected_revision=rev(hub, t.id))
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
    hub.reply("gpt", a.id, "answer", "human", expected_revision=thread.revision)
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


def test_every_change_bumps_the_revision(hub):
    t = hub.create_thread("claude", "Topic", "body", "gpt")
    assert t.revision == 1
    thread, _, _ = hub.reply("gpt", t.id, "x", "claude", expected_revision=1)
    assert thread.revision == 2
    assert hub.update_summary("claude", t.id, "s", expected_revision=2).revision == 3
    thread, _, _ = hub.reply("human", t.id, "y", "claude")  # the human needs no revision...
    assert thread.revision == 4  # ...but still bumps it


def test_agent_writes_require_a_current_revision(hub):
    t = hub.create_thread("claude", "Topic", "body", "gpt")
    with pytest.raises(HubError, match="Pass expected_revision"):
        hub.reply("gpt", t.id, "x", "claude")
    with pytest.raises(HubError, match="changed since you read it"):
        hub.reply("gpt", t.id, "x", "claude", expected_revision=0)


def test_stale_writer_cannot_overwrite_summary_or_reply(hub):
    """P-004 on T-0001: an orphaned run and a fresh run both act on the same turn."""
    t = hub.create_thread("claude", "Topic", "body", "gpt")
    seen = rev(hub, t.id)  # both runs read revision 1
    hub.update_summary("gpt", t.id, "fresh run's summary", expected_revision=seen)
    with pytest.raises(HubError, match="changed since you read it"):
        hub.update_summary("gpt", t.id, "orphan's summary", expected_revision=seen)
    with pytest.raises(HubError, match="changed since you read it"):
        hub.reply("gpt", t.id, "orphan's reply", "claude", expected_revision=seen)
    assert hub.get(t.id).summary == "fresh run's summary"


def test_human_post_invalidates_in_flight_agent(hub):
    t = hub.create_thread("claude", "Topic", "body", "gpt")
    seen = rev(hub, t.id)
    hub.reply("human", t.id, "wait, new constraint", "gpt")
    with pytest.raises(HubError, match="changed since you read it"):
        hub.reply("gpt", t.id, "answer based on stale state", "claude", expected_revision=seen)


def test_summary_requires_turn(hub):
    t = hub.create_thread("claude", "Topic", "body", "gpt")
    with pytest.raises(HubError, match="gpt's turn"):
        hub.update_summary("claude", t.id, "sneaky", expected_revision=1)
    hub.update_summary("human", t.id, "the human can always edit it")


def test_threads_without_revision_default_to_zero(hub):
    t = hub.create_thread("claude", "Old", "body", "gpt")
    t.meta.pop("revision")
    t.path.write_text(t.render())
    assert rev(hub, t.id) == 0
    thread, _, _ = hub.reply("gpt", t.id, "x", "claude", expected_revision=0)
    assert thread.revision == 1
