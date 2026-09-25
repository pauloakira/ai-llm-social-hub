from pathlib import Path

from llm_hub import desktop

START = "2026-09-25 01:25:23 [info] Starting app {\n"
CONNECTED = "2026-09-25 01:27:15 [info] [LocalMcpServerManager] Connected to llm-hub (8 tools)\n"


class FakeClaude:
    """Claude desktop on a fake clock: it quits at the times in `quits` and, unless `relaunch` is None,
    relaunches itself `relaunch` seconds later. Each launch after `apply` pops `connects` to decide whether
    that run loads llm-hub, and logs accordingly."""

    def __init__(self, home: Path, quits, relaunch=1.0, connects=(True,)):
        self.log = desktop.log_path(home)
        self.log.parent.mkdir(parents=True)
        self.log.write_text(START)
        self.t, self.pid, self.up, self.back_at = 0.0, 100, True, None
        self.quits, self.relaunch, self.connects = list(quits), relaunch, list(connects)
        self.applied, self.opened = [], 0

    def clock(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds
        if self.up and self.quits and self.t >= self.quits[0]:
            self.quits.pop(0)
            self.up, self.back_at = False, None if self.relaunch is None else self.t + self.relaunch
        if not self.up and self.back_at is not None and self.t >= self.back_at:
            self.launch()

    def launch(self):
        self.up, self.pid, self.back_at = True, self.pid + 1, None
        connected = bool(self.applied) and self.connects.pop(0)
        with self.log.open("a") as fh:
            fh.write(START + (CONNECTED if connected else ""))

    def find(self):
        return [self.pid] if self.up else []

    def is_alive(self, pid):
        return self.up and pid == self.pid

    def open_app(self):
        self.opened += 1
        self.launch()

    def apply(self):
        assert not self.up, "the config must only be written while Claude is closed"
        self.applied.append(self.t)
        return "added"


def finish(home, fake, uninstall=False, **kw):
    return desktop.finish(home, fake.apply, uninstall, find=fake.find, is_alive=fake.is_alive, open_app=fake.open_app,
                          sleep=fake.sleep, clock=fake.clock, say=lambda msg: None, **kw)


def test_writes_between_quit_and_the_automatic_relaunch(tmp_path):
    fake = FakeClaude(tmp_path, quits=[3.0], relaunch=1.0)
    assert finish(tmp_path, fake)
    assert len(fake.applied) == 1 and 3.0 <= fake.applied[0] < 3.2  # within a poll of the quit
    assert fake.opened == 0


def test_reopens_claude_when_it_does_not_relaunch_itself(tmp_path):
    fake = FakeClaude(tmp_path, quits=[1.0], relaunch=None)
    assert finish(tmp_path, fake)
    assert fake.opened == 1


def test_tries_again_on_the_next_quit_when_the_relaunch_missed_it(tmp_path):
    fake = FakeClaude(tmp_path, quits=[1.0, 20.0], relaunch=1.0, connects=(False, True))
    assert finish(tmp_path, fake)
    assert len(fake.applied) == 2


def test_uninstall_is_confirmed_when_claude_reopens_without_the_server(tmp_path):
    fake = FakeClaude(tmp_path, quits=[1.0], relaunch=1.0, connects=(False,))
    assert finish(tmp_path, fake, uninstall=True)


def test_gives_up_after_the_timeout_without_touching_the_config(tmp_path):
    fake = FakeClaude(tmp_path, quits=[], relaunch=1.0)
    assert not finish(tmp_path, fake, timeout=10)
    assert fake.applied == []


def test_load_state_reads_only_the_current_run(tmp_path):
    log = desktop.log_path(tmp_path)
    log.parent.mkdir(parents=True)
    assert desktop.load_state(tmp_path) == ("unknown", "")
    log.write_text(START + CONNECTED + START)
    assert desktop.load_state(tmp_path) == ("not-loaded", "")
    log.write_text(START + CONNECTED + START + "x [info] Launching MCP Server: llm-hub\n")
    assert desktop.load_state(tmp_path) == ("launched", "")
    log.write_text(START + CONNECTED)
    assert desktop.load_state(tmp_path) == ("connected", "8")


def test_read_log_restarts_after_rotation(tmp_path):
    log = tmp_path / "main.log"
    log.write_text("new log\n")
    assert desktop.read_log(log, offset=10_000) == "new log\n"
    assert desktop.read_log(log, offset=4) == "log\n"
