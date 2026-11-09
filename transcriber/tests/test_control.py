"""The tray icon's Stop all and Start menus (sdrtrunk_transcriber.control)."""

import json

import pytest

from sdrtrunk_transcriber import control
from sdrtrunk_transcriber.config import Config

from test_watchdog import BAT, JAVA, FakeSystem


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "HOME", tmp_path)
    monkeypatch.setattr(control, "STOP_FLAG", tmp_path / "stop.flag")
    monkeypatch.setattr(control, "STATUS_FILE", tmp_path / "watchdog-status.json")
    monkeypatch.setattr(control.time, "sleep", lambda s: None)
    config = Config()
    config.summaries.enabled = True
    config.watchdog.sdrtrunk = BAT
    system = FakeSystem()
    system.killed_names = []
    system.kill_name = system.killed_names.append
    system.hidden_launches = []
    plain_launch = system.launch

    def launch(bat, cwd, extra_env=None, hidden=None):
        system.hidden_launches.append(hidden)
        return plain_launch(bat, cwd, extra_env)
    system.launch = launch
    return control.Control(config, system, home=tmp_path), system, tmp_path


def write_status(tmp_path, age=0):
    status = {"time": control.time.time() - age, "programs": [], "sdrtrunk_bat": BAT}
    (tmp_path / "watchdog-status.json").write_text(json.dumps(status))


def test_stop_all_without_a_watchdog_stops_the_programs_directly(setup):
    ctl, system, tmp_path = setup
    system.port = {777}
    kill = system.kill_tree

    def kill_tree(pid):
        kill(pid)
        if pid == 500:
            system.sdrtrunk = []
        system.port.discard(pid)
    system.kill_tree = kill_tree
    result = ctl.stop_all()
    assert result[0] == "ok"
    assert 500 in system.killed and 777 in system.killed and system.killed_names == ["ollama.exe"]
    assert not (tmp_path / "stop.flag").exists()


def test_stop_all_asks_the_watchdog_first(setup, monkeypatch):
    ctl, system, tmp_path = setup
    write_status(tmp_path)
    system.sdrtrunk = []
    seen = []

    def watchdog_answers(s):
        # The watchdog takes the flag and removes its status, as Watchdog.stop_all does.
        seen.append((tmp_path / "stop.flag").exists())
        (tmp_path / "watchdog-status.json").unlink(missing_ok=True)
    monkeypatch.setattr(control.time, "sleep", watchdog_answers)
    assert ctl.stop_all()[0] == "ok"
    assert seen[0] is True


def test_stop_all_says_what_it_could_not_stop(setup):
    ctl, system, _ = setup  # kill_tree does nothing here, as when Windows says no
    result = ctl.stop_all()
    assert result[0] == "failed" and "SDRTrunk" in result[1]


def test_start_sdrtrunk_with_no_window_and_with_its_window(setup):
    ctl, system, _ = setup
    system.sdrtrunk = []
    assert ctl.start("sdrtrunk", window=False)[0] == "ok"
    assert "headless" in system.envs[-1]["JAVA_OPTS"] and system.hidden_launches[-1] is True
    system.sdrtrunk = []
    assert ctl.start("sdrtrunk", window=True)[0] == "ok"
    assert system.envs[-1] is None and system.hidden_launches[-1] is False


def test_start_what_is_already_running_does_nothing(setup):
    ctl, system, _ = setup
    assert ctl.start("sdrtrunk", window=False)[0] == "already"
    assert ctl.start("transcriber", window=True)[0] == "already"
    assert ctl.start("ollama", window=True)[0] == "already"
    assert system.launched == []


def test_start_transcriber_and_ollama(setup):
    ctl, system, _ = setup
    system.up = {"ollama": False, "transcriber": False}
    assert ctl.start("transcriber", window=False)[0] == "ok"
    assert ctl.start("ollama", window=True)[0] == "ok"
    assert system.launched == ["start-transcriber.bat", "start-ollama.bat"]
    assert system.hidden_launches == [True, False]


def test_start_everything(setup, monkeypatch):
    ctl, system, tmp_path = setup
    write_status(tmp_path)
    assert ctl.start("everything", window=False)[0] == "already"
    write_status(tmp_path, age=1000)  # a watchdog that stopped long ago
    assert ctl.start("everything", window=True)[0] == "ok"
    assert system.launched == ["start-everything.bat"]
    monkeypatch.setattr(ctl, "run_boot_task", lambda: True)
    assert ctl.start("everything", window=False)[0] == "ok"
    assert system.launched == ["start-everything.bat"]  # the boot task did it


def test_sdrtrunk_path_comes_from_the_watchdog_when_not_set(setup):
    ctl, system, tmp_path = setup
    ctl.config.watchdog.sdrtrunk = ""
    write_status(tmp_path, age=5000)
    system.sdrtrunk = []
    assert ctl.sdrtrunk_bat() == BAT
