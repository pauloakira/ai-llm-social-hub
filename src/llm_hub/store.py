"""Thread storage: one append-only markdown file per thread.

The hub directory looks like:

    llm-hub/
      hub.yaml              # agents + turn budget
      threads/T-0001-some-title.md
      .state/cursors.json   # per-agent read cursors (local, gitignored)
      .state/lock           # flock target for writers

All writes go through `Hub`, which serializes them with an exclusive flock and
replaces files atomically, so several server processes (one per agent) can
share the same directory.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator

import yaml

HUMAN = "human"
NOBODY = "none"
POST_TYPES = ("proposal", "question", "answer", "critique", "decision", "note")
STATUSES = ("open", "resolved")
DEFAULT_CONFIG = {"agents": ["claude", "gpt", HUMAN], "max_turns": 8}

POST_HEADER = re.compile(
    r"^### (?P<id>P-\d+) · (?P<author>\S+) → (?P<hand_to>\S+) · (?P<type>\w+)"
    r"(?: · re:(?P<re>P-\d+))? · (?P<ts>\S+)$"
)
# Lines in user text that would be mistaken for file structure get a leading backslash.
_STRUCTURAL = re.compile(r"^(\\*)(## Summary|## Posts|### P-\d)")


class HubError(Exception):
    """A rule violation or bad input. The message is shown to the agent."""


@dataclass
class Post:
    id: str
    author: str
    hand_to: str
    type: str
    ts: str
    body: str
    re: str | None = None

    def render(self) -> str:
        re_part = f" · re:{self.re}" if self.re else ""
        header = f"### {self.id} · {self.author} → {self.hand_to} · {self.type}{re_part} · {self.ts}"
        return f"{header}\n\n{_escape(self.body.strip())}\n"


@dataclass
class Thread:
    meta: dict
    summary: str
    posts: list[Post] = field(default_factory=list)
    path: Path | None = None

    @property
    def id(self) -> str:
        return self.meta["id"]

    @property
    def title(self) -> str:
        return self.meta["title"]

    @property
    def revision(self) -> int:
        """Bumped by every change to the thread; agents must send the one they read (compare-and-swap)."""
        return int(self.meta.get("revision", 0))

    def render(self) -> str:
        front = yaml.safe_dump(self.meta, sort_keys=False, allow_unicode=True).strip()
        summary = _escape(self.summary.strip()) or "_No summary yet._"
        posts = "\n".join(p.render() for p in self.posts)
        return f"---\n{front}\n---\n\n# {self.title}\n\n## Summary\n\n{summary}\n\n## Posts\n\n{posts}"

    def post(self, post_id: str) -> Post | None:
        return next((p for p in self.posts if p.id == post_id), None)

    def posts_after(self, post_id: str | None) -> list[Post]:
        if post_id is None:
            return list(self.posts)
        ids = [p.id for p in self.posts]
        return self.posts[ids.index(post_id) + 1 :] if post_id in ids else list(self.posts)


def _escape(text: str) -> str:
    return "\n".join(_STRUCTURAL.sub(r"\\\1\2", line) for line in text.splitlines())


def _unescape(text: str) -> str:
    return "\n".join(
        line[1:] if line.startswith("\\") and _STRUCTURAL.match(line[1:]) else line
        for line in text.splitlines()
    )


def parse_thread(text: str, path: Path | None = None) -> Thread:
    if not text.startswith("---\n"):
        raise HubError(f"{path}: missing frontmatter")
    _, front, rest = text.split("---\n", 2)
    meta = yaml.safe_load(front) or {}

    summary_start = rest.find("\n## Summary\n")
    posts_start = rest.find("\n## Posts\n")
    if summary_start < 0 or posts_start < 0:
        raise HubError(f"{path}: missing '## Summary' or '## Posts' section")
    summary = rest[summary_start + len("\n## Summary\n") : posts_start].strip()
    if summary == "_No summary yet._":
        summary = ""

    posts: list[Post] = []
    current: dict | None = None
    body_lines: list[str] = []

    def flush() -> None:
        if current is not None:
            posts.append(Post(body=_unescape("\n".join(body_lines).strip()), **current))

    for line in rest[posts_start + len("\n## Posts\n") :].splitlines():
        m = POST_HEADER.match(line)
        if m:
            flush()
            current, body_lines = m.groupdict(), []
        elif current is not None:
            body_lines.append(line)
    flush()
    return Thread(meta=meta, summary=_unescape(summary), posts=posts, path=path)


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:40].rstrip("-") or "thread"


def web_url(remote: str) -> str:
    """git@github.com:owner/repo.git or https://github.com/owner/repo.git -> https://github.com/owner/repo"""
    remote = remote.strip()
    m = re.fullmatch(r"(?:ssh://)?git@([^:/]+)[:/](.+?)(?:\.git)?/?", remote)
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    return re.sub(r"(?:\.git)?/?$", "", re.sub(r"^(https?://)[^@/]+@", r"\1", remote))


def normalize_thread_id(raw: str) -> str:
    m = re.fullmatch(r"(?:T-?)?0*(\d+)", raw.strip(), re.IGNORECASE)
    if not m:
        raise HubError(f"Invalid thread id {raw!r}; expected something like T-0007.")
    return f"T-{int(m.group(1)):04d}"


class Hub:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        if not (self.root / "hub.yaml").exists():
            raise HubError(f"No hub at {self.root} (hub.yaml missing). Run `llm-hub init <repo>`.")
        self.config = {**DEFAULT_CONFIG, **(yaml.safe_load((self.root / "hub.yaml").read_text()) or {})}
        self.threads_dir = self.root / "threads"
        self.state_dir = self.root / ".state"
        self.threads_dir.mkdir(exist_ok=True)
        self.state_dir.mkdir(exist_ok=True)

    # ---------- setup ----------

    @staticmethod
    def init(root: Path, agents: list[str] | None = None, max_turns: int | None = None) -> "Hub":
        root = Path(root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        if not (root / "hub.yaml").exists():
            config = dict(DEFAULT_CONFIG)
            if agents:
                config["agents"] = [*dict.fromkeys([*agents, HUMAN])]
            if max_turns:
                config["max_turns"] = max_turns
            (root / "hub.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
        (root / "threads").mkdir(exist_ok=True)
        (root / ".gitignore").write_text(".state/\n")
        return Hub(root)

    @property
    def agents(self) -> list[str]:
        return list(self.config["agents"])

    @property
    def max_turns(self) -> int:
        return int(self.config["max_turns"])

    @property
    def repo_url(self) -> str | None:
        """Where agents without repo access can read the code: `repo_url` in hub.yaml, else the git origin."""
        if "repo_url" in self.config:
            return self.config["repo_url"] or None
        try:
            out = subprocess.run(
                ["git", "-C", str(self.root.parent), "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return web_url(out.stdout) if out.returncode == 0 and out.stdout.strip() else None

    # ---------- low-level io ----------

    @contextmanager
    def _lock(self) -> Iterator[None]:
        with open(self.state_dir / "lock", "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(text)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _thread_path(self, thread_id: str) -> Path:
        tid = normalize_thread_id(thread_id)
        matches = sorted(self.threads_dir.glob(f"{tid}*.md"))
        if not matches:
            raise HubError(f"Thread {tid} not found.")
        return matches[0]

    def _save(self, thread: Thread) -> None:
        assert thread.path is not None
        self._atomic_write(thread.path, thread.render())

    def _cursors(self) -> dict:
        path = self.state_dir / "cursors.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def _set_cursor(self, agent: str, thread: Thread) -> None:
        if not thread.posts:
            return
        cursors = self._cursors()
        cursors.setdefault(agent, {})[thread.id] = thread.posts[-1].id
        self._atomic_write(self.state_dir / "cursors.json", json.dumps(cursors, indent=2))

    # ---------- reads ----------

    def get(self, thread_id: str) -> Thread:
        path = self._thread_path(thread_id)
        return parse_thread(path.read_text(), path)

    def threads(self, status: str | None = None, tag: str | None = None) -> list[Thread]:
        result = []
        for path in sorted(self.threads_dir.glob("T-*.md")):
            thread = parse_thread(path.read_text(), path)
            if status and status != "all" and thread.meta.get("status") != status:
                continue
            if tag and tag not in (thread.meta.get("tags") or []):
                continue
            result.append(thread)
        return result

    def unread(self, agent: str, thread: Thread) -> list[Post]:
        cursor = self._cursors().get(agent, {}).get(thread.id)
        return [p for p in thread.posts_after(cursor) if p.author != agent]

    def inbox(self, agent: str) -> tuple[list[Thread], list[tuple[Thread, int]]]:
        """(threads waiting on `agent`, [(other thread, unread count)])."""
        my_turn, fyi = [], []
        for thread in self.threads(status="open"):
            if thread.meta.get("awaiting") == agent:
                my_turn.append(thread)
            elif agent in (thread.meta.get("participants") or []):
                n = len(self.unread(agent, thread))
                if n:
                    fyi.append((thread, n))
        return my_turn, fyi

    def read(self, agent: str, thread_id: str, only_new: bool = True) -> tuple[Thread, list[Post]]:
        """Return the thread and the posts to show; advances the agent's read cursor."""
        with self._lock():
            thread = self.get(thread_id)
            cursor = self._cursors().get(agent, {}).get(thread.id)
            posts = thread.posts_after(cursor) if only_new else list(thread.posts)
            self._set_cursor(agent, thread)
        return thread, posts

    def search(self, query: str) -> list[tuple[Thread, list[Post]]]:
        q = query.lower()
        hits = []
        for thread in self.threads(status="all"):
            posts = [p for p in thread.posts if q in p.body.lower()]
            if posts or q in thread.title.lower() or q in thread.summary.lower():
                hits.append((thread, posts))
        return hits

    # ---------- writes ----------

    def _check_agent(self, name: str, role: str) -> None:
        if name not in self.agents:
            raise HubError(f"Unknown {role} {name!r}. Known agents: {', '.join(self.agents)}.")

    def _check_hand_to(self, author: str, hand_to: str, *, allow_self: bool = True) -> None:
        if hand_to != NOBODY:
            self._check_agent(hand_to, "hand_to")
        if hand_to == author and not allow_self:
            raise HubError("A new thread must go to someone else: another agent, 'human', or 'none'.")

    def _check_type(self, post_type: str) -> None:
        if post_type not in POST_TYPES:
            raise HubError(f"Invalid type {post_type!r}. Use one of: {', '.join(POST_TYPES)}.")

    @staticmethod
    def _check_agent_write(thread: Thread, author: str, expected_revision: int | None) -> None:
        """Agents may change a thread only when it's open, nothing changed since they read it, and either it's
        their turn or they wrote the latest post (a follow-up before anyone else answers). The human is exempt.
        Must be called under the store lock."""
        if author == HUMAN:
            return
        meta = thread.meta
        if meta["status"] != "open":
            raise HubError(f"{thread.id} is {meta['status']}. Only the human can reopen it.")
        follow_up = bool(thread.posts) and thread.posts[-1].author == author
        if meta["awaiting"] not in (author, NOBODY) and not follow_up:
            raise HubError(
                f"It's {meta['awaiting']}'s turn on {thread.id}, not yours. You can post again only as a follow-up "
                "to your own latest post, before anyone else answers."
            )
        if expected_revision is None:
            raise HubError(f"Pass expected_revision: the revision read_thread showed for {thread.id}.")
        if expected_revision != thread.revision:
            raise HubError(
                f"{thread.id} changed since you read it (revision {thread.revision}, you sent {expected_revision}). "
                "Call read_thread again and rethink before writing."
            )

    @staticmethod
    def _bump(thread: Thread, ts: str | None = None) -> None:
        thread.meta["revision"] = thread.revision + 1
        thread.meta["updated"] = ts or now()

    def create_thread(
        self,
        author: str,
        title: str,
        body: str,
        to: str,
        post_type: str = "proposal",
        tags: list[str] | None = None,
        related: list[str] | None = None,
    ) -> Thread:
        self._check_agent(author, "author")
        self._check_hand_to(author, to, allow_self=False)
        self._check_type(post_type)
        if not title.strip() or not body.strip():
            raise HubError("Title and body are required.")
        with self._lock():
            existing = [int(p.name[2:6]) for p in self.threads_dir.glob("T-*.md")]
            tid = f"T-{max(existing, default=0) + 1:04d}"
            ts = now()
            meta = {
                "id": tid,
                "title": title.strip(),
                "status": "open",
                "awaiting": to,
                "participants": [*dict.fromkeys([author, to] if to != NOBODY else [author])],
                "tags": tags or [],
                "related": [normalize_thread_id(r) for r in related or []],
                "turns": 0 if author == HUMAN else 1,
                "max_turns": self.max_turns,
                "revision": 1,
                "created": ts,
                "updated": ts,
            }
            thread = Thread(meta=meta, summary="", path=self.threads_dir / f"{tid}-{slugify(title)}.md")
            thread.posts.append(Post("P-001", author, to, post_type, ts, body))
            self._save(thread)
            self._set_cursor(author, thread)
        return thread

    def reply(
        self,
        author: str,
        thread_id: str,
        body: str,
        hand_to: str,
        post_type: str = "answer",
        re_: str | None = None,
        *,
        expected_revision: int | None = None,
        resolve: bool = False,
    ) -> tuple[Thread, Post, list[str]]:
        """Append a post. Returns (thread, post, notes for the author)."""
        self._check_agent(author, "author")
        self._check_hand_to(author, hand_to)
        self._check_type(post_type)
        if not body.strip():
            raise HubError("Body is required.")
        notes: list[str] = []
        with self._lock():
            thread = self.get(thread_id)
            meta = thread.meta
            self._check_agent_write(thread, author, expected_revision)
            if re_ and not thread.post(re_):
                raise HubError(f"Post {re_} doesn't exist in {thread.id}.")

            if author == HUMAN:
                meta["turns"] = 0
                if meta["status"] != "open":
                    meta["status"] = "open"
                    notes.append(f"{thread.id} reopened.")
            else:
                meta["turns"] = int(meta.get("turns", 0)) + 1
                budget_hit = meta["turns"] >= int(meta.get("max_turns", self.max_turns))
                if budget_hit and not resolve and hand_to != HUMAN:
                    notes.append(
                        f"Turn budget ({meta['max_turns']}) reached: the turn goes to human instead of {hand_to}."
                    )
                    hand_to = HUMAN

            post = Post(f"P-{len(thread.posts) + 1:03d}", author, hand_to, post_type, now(), body, re_)
            thread.posts.append(post)
            meta["awaiting"] = hand_to
            self._bump(thread, post.ts)
            if resolve:
                meta["status"] = "resolved"
                meta["resolved_by"] = author
            participants = meta.setdefault("participants", [])
            for name in (author, hand_to):
                if name != NOBODY and name not in participants:
                    participants.append(name)
            self._save(thread)
            self._set_cursor(author, thread)
        return thread, post, notes

    def update_summary(
        self, author: str, thread_id: str, summary: str, *, expected_revision: int | None = None
    ) -> Thread:
        self._check_agent(author, "author")
        with self._lock():
            thread = self.get(thread_id)
            self._check_agent_write(thread, author, expected_revision)
            thread.summary = summary.strip()
            thread.meta["summary_by"] = author
            self._bump(thread)
            self._save(thread)
        return thread

    def resolve(
        self, author: str, thread_id: str, decision: str, *, expected_revision: int | None = None
    ) -> Thread:
        thread, _, _ = self.reply(
            author, thread_id, decision, NOBODY, "decision", expected_revision=expected_revision, resolve=True
        )
        return thread
