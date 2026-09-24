import io
from datetime import datetime, timedelta, timezone

import pytest
from rich.console import Console

from llm_hub.cli import main
from llm_hub.store import Hub
from llm_hub.view import _ago, _when, render_thread, render_thread_list


def plain(renderable) -> str:
    console = Console(file=io.StringIO(), width=100, color_system=None)
    console.print(renderable)
    return console.file.getvalue()


@pytest.fixture
def hub(tmp_path):
    hub = Hub.init(tmp_path / "llm-hub")
    t = hub.create_thread("claude", "Cache design", "Should we use **LRU**?\n\n```python\ncache = {}\n```", "gpt")
    hub.reply("gpt", t.id, "LFU fits better.", "human", "critique", re_="P-001", expected_revision=1)
    hub.update_summary("human", t.id, "- **Agreed:** LFU")
    return hub


def test_thread_view_shows_header_summary_posts_and_turn(hub):
    out = plain(render_thread(hub.get("T-0001")))
    assert "T-0001 · Cache design" in out
    assert "your turn" in out and "awaiting human" in out
    assert "Agreed: LFU" in out  # markdown rendered, not raw
    assert "P-001  claude → gpt  ·  proposal" in out
    assert "P-002  gpt → human  ·  critique  ·  re P-001" in out
    assert "cache = {}" in out and "```" not in out
    assert "Your turn." in out


def test_thread_list(hub):
    out = plain(render_thread_list(hub.threads()))
    assert "T-0001" in out and "Cache design" in out and "your turn" in out
    assert "No threads." in plain(render_thread_list([]))


def test_show_last_and_raw(hub, capsys):
    main(["--root", str(hub.root), "show", "T-0001", "--last", "1"])
    out = capsys.readouterr().out
    assert "1 earlier post hidden" in out and "LFU fits better." in out and "Should we use" not in out

    main(["--root", str(hub.root), "show", "T-0001", "--raw"])
    out = capsys.readouterr().out
    assert out.startswith("# T-0001 · Cache design") and "**LRU**" in out


def test_ls_raw(hub, capsys):
    main(["--root", str(hub.root), "ls", "--raw"])
    assert "◀ your turn" in capsys.readouterr().out


def test_time_formatting():
    now = datetime(2026, 9, 24, 15, 30, tzinfo=timezone.utc)
    assert _when("2026-09-24T15:05:00+00:00", now) == "15:05"
    assert _when("2026-09-20T15:05:00+00:00", now) == "Sep 20 15:05"
    assert _ago((now - timedelta(minutes=5)).isoformat(), now) == "5m ago"
    assert _ago((now - timedelta(days=2)).isoformat(), now) == "2d ago"
    assert _ago(now.isoformat(), now) == "just now"
