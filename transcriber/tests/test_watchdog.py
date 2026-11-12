"""The watchdog: what it starts, when it restarts a program, and when it gives up for a while."""

from sdrtrunk_transcriber import watchdog
from sdrtrunk_transcriber.config import Config

BAT = r"E:\SDR\image\bin\sdr-trunk.bat"
JAVA = r"E:\SDR\image\bin\java.exe"


class FakeSystem:
    def __init__(self):
        self.time = 1_000_000.0
        self.up = {"ollama": True, "transcriber": True}
        self.sdrtrunk = [(500, True, JAVA)]
        self.launched = []
        self.killed = []
        self.port = set()
        self.next_pid = 100
        self.envs = []
        self.edit = False
        self.update = False
        self.restart = False
        self.stop = False
        self.status = None
        self.killed_names = []
        self.heartbeat_ago = None

    def now(self):
        return self.time

    def sleep(self, seconds):
        self.time += seconds

    def modified_ago(self, path):
        return self.heartbeat_ago

    def answers(self, url, timeout=20):
        return self.up["ollama" if "11434" in url else "transcriber"]

    def launch(self, bat, cwd, extra_env=None):
        name = bat.replace("\\", "/").rsplit("/", 1)[-1]
        self.launched.append(name)
        self.envs.append(extra_env)
        self.next_pid += 1
        if name == "start-transcriber.bat":
            self.up["transcriber"] = True
        elif name == "start-ollama.bat":
            self.up["ollama"] = True
        else:
            self.sdrtrunk = [(self.next_pid + 1000, True, JAVA)]
        return self.next_pid

    def kill_tree(self, pid):
        self.killed.append(pid)

    def port_pids(self, port):
        return set(self.port)

    def sdrtrunk_processes(self):
        return list(self.sdrtrunk)

    def exists(self, path):
        return path == BAT or path in getattr(self, "files", ())

    def listdir(self, path):
        return getattr(self, "dirs", {}).get(path, [])

    def editing(self):
        return self.edit

    def updating(self):
        return self.update

    def take_restart_request(self):
        asked, self.restart = self.restart, False
        return asked

    def take_stop_request(self):
        asked, self.stop = self.stop, False
        return asked

    def kill_name(self, image):
        self.killed_names.append(image)

    def clear_status(self):
        self.status = None

    def write_status(self, status):
        self.status = status


def make(sdrtrunk_bat="", summaries=True, background=False):
    config = Config()
    config.summaries.enabled = summaries
    config.watchdog.sdrtrunk = sdrtrunk_bat
    system = FakeSystem()
    alerts = []
    dog = watchdog.Watchdog(config, system, notify=lambda title, message: alerts.append((title, message)),
                            background=background)
    return dog, system, alerts


def run_checks(dog, system, n):
    for _ in range(n):
        system.sleep(dog.settings.check_s)
        dog.tick()


def test_running_programs_are_left_alone_and_sdrtrunk_path_is_learned():
    dog, system, alerts = make()
    dog.start_all()
    assert system.launched == []
    run_checks(dog, system, 10)
    assert dog.programs[-1].bat == BAT
    assert system.launched == [] and alerts == []


def test_starts_what_is_not_running_in_order():
    dog, system, _ = make(sdrtrunk_bat=BAT)
    system.up = {"ollama": False, "transcriber": False}
    system.sdrtrunk = []
    dog.start_all()
    assert system.launched == ["start-ollama.bat", "start-transcriber.bat", "sdr-trunk.bat"]


def test_no_ollama_when_summaries_are_off():
    dog, system, _ = make(summaries=False)
    assert [p.name for p in dog.programs] == ["Transcriber", "SDRTrunk"]


def test_crashed_transcriber_is_restarted_after_enough_failed_checks():
    dog, system, alerts = make()
    dog.start_all()
    system.time += dog.settings.startup_s
    system.up["transcriber"] = False
    system.port = {4242}   # the frozen old copy still holds port 3000
    run_checks(dog, system, dog.settings.fail_checks - 1)
    assert system.launched == []
    run_checks(dog, system, 1)
    assert system.launched == ["start-transcriber.bat"]
    assert 4242 in system.killed
    assert alerts[0][0] == "Transcriber restarted"
    assert "stopped answering" in alerts[0][1]


def test_slow_transcriber_still_finishing_calls_is_not_restarted():
    # Low memory or a pile of waiting calls: no answers, but a call was finished a minute ago.
    dog, system, alerts = make()
    dog.start_all()
    system.time += dog.settings.startup_s
    system.up["transcriber"] = False
    system.heartbeat_ago = 60
    run_checks(dog, system, dog.settings.fail_checks * 3)
    assert system.launched == [] and alerts == []
    # Nothing finished for a long while: it's stuck after all.
    system.heartbeat_ago = watchdog.TRANSCRIBER_BUSY_S + 1
    run_checks(dog, system, dog.settings.fail_checks)
    assert system.launched == ["start-transcriber.bat"]


def test_transcriber_is_checked_without_the_database():
    dog, _, _ = make()
    transcriber = next(p for p in dog.programs if p.name == "Transcriber")
    assert transcriber.url.endswith("/api/alive")
    assert transcriber.heartbeat.name == watchdog.HEARTBEAT_FILE


def test_a_single_slow_answer_does_not_restart():
    dog, system, alerts = make()
    dog.start_all()
    system.time += dog.settings.startup_s
    for _ in range(5):
        system.up["transcriber"] = False
        run_checks(dog, system, dog.settings.fail_checks - 1)
        system.up["transcriber"] = True
        run_checks(dog, system, 1)
    assert system.launched == [] and alerts == []


def test_frozen_sdrtrunk_is_killed_and_restarted():
    dog, system, alerts = make()
    dog.start_all()
    system.time += dog.settings.startup_s
    run_checks(dog, system, 1)       # learns where sdr-trunk.bat is
    system.sdrtrunk = [(500, False, JAVA)]
    run_checks(dog, system, dog.settings.fail_checks)
    assert 500 in system.killed
    assert system.launched == ["sdr-trunk.bat"]
    assert alerts == [("SDRTrunk restarted", alerts[0][1])] and "froze" in alerts[0][1]


def test_restarted_program_gets_time_to_start_before_checks():
    dog, system, _ = make(sdrtrunk_bat=BAT)
    dog.start_all()
    system.time += dog.settings.startup_s
    system.sdrtrunk = []
    run_checks(dog, system, dog.settings.fail_checks)
    assert system.launched == ["sdr-trunk.bat"]
    system.sdrtrunk = []     # still starting up
    run_checks(dog, system, dog.settings.startup_s // dog.settings.check_s - 1)
    assert system.launched == ["sdr-trunk.bat"]


def test_sdrtrunk_is_not_started_without_knowing_where_it_is():
    dog, system, alerts = make()
    system.sdrtrunk = []
    dog.start_all()
    run_checks(dog, system, 20)
    assert system.launched == [] and alerts == []


def test_gives_up_for_an_hour_after_too_many_restarts():
    dog, system, alerts = make()
    dog.start_all()
    limit = dog.settings.max_restarts_per_hour
    for _ in range(limit + 1):
        system.time += dog.settings.startup_s
        system.up["ollama"] = False
        run_checks(dog, system, dog.settings.fail_checks)
        system.up["ollama"] = False
    assert system.launched.count("start-ollama.bat") == limit
    assert alerts[-1][0] == "Ollama keeps failing"
    run_checks(dog, system, 30)
    assert system.launched.count("start-ollama.bat") == limit
    system.time += watchdog.PAUSE_S
    run_checks(dog, system, dog.settings.fail_checks)
    assert system.launched.count("start-ollama.bat") == limit + 1


def test_unknown_sdrtrunk_state_is_not_a_failure():
    dog, system, _ = make(sdrtrunk_bat=BAT)
    dog.start_all()
    system.time += dog.settings.startup_s
    system.sdrtrunk_processes = lambda: None
    run_checks(dog, system, 20)
    assert system.launched == []


def test_listening_pids():
    netstat = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1100
  TCP    127.0.0.1:3000         0.0.0.0:0              LISTENING       8812
  TCP    127.0.0.1:3000         127.0.0.1:51000        ESTABLISHED     8812
  TCP    127.0.0.1:51000        127.0.0.1:3000         ESTABLISHED     9900
  TCP    0.0.0.0:30000          0.0.0.0:0              LISTENING       7777
  TCP    [::]:3000              [::]:0                 LISTENING       8813
"""
    assert watchdog.listening_pids(netstat, 3000) == {8812, 8813}


def test_parse_sdrtrunk():
    text = "12345|True|E:\\SDR\\bin\\java.exe\r\n678|False|E:\\SDR\\bin\\java.exe\r\n\r\n"
    assert watchdog.parse_sdrtrunk(text) == [(12345, True, JAVA.replace("image\\", "")),
                                             (678, False, JAVA.replace("image\\", ""))]


def test_background_mode_starts_sdrtrunk_with_no_window():
    dog, system, _ = make(sdrtrunk_bat=BAT, background=True)
    system.sdrtrunk = []
    dog.start_all()
    assert system.launched[-1] == "sdr-trunk.bat"
    assert "-Djava.awt.headless=true" in system.envs[-1]["JAVA_OPTS"]


def test_normal_mode_starts_sdrtrunk_with_its_window():
    dog, system, _ = make(sdrtrunk_bat=BAT)
    system.sdrtrunk = []
    dog.start_all()
    assert system.launched[-1] == "sdr-trunk.bat" and system.envs[-1] is None


def test_edit_mode_leaves_sdrtrunk_alone_then_starts_it_right_away():
    dog, system, alerts = make(sdrtrunk_bat=BAT, background=True)
    dog.start_all()
    run_checks(dog, system, 7)
    system.edit = True
    system.sdrtrunk = []          # edit-mode.bat stopped the no-window copy
    run_checks(dog, system, 20)
    assert system.launched == [] and alerts == []
    system.edit = False           # the window was closed
    run_checks(dog, system, 1)
    assert system.launched == ["sdr-trunk.bat"] and alerts == []
    assert "-Djava.awt.headless=true" in system.envs[-1]["JAVA_OPTS"]


def states(system):
    return {p["name"]: p["state"] for p in system.status["programs"]}


def test_status_for_the_tray_icon():
    dog, system, _ = make(sdrtrunk_bat=BAT, background=True)
    dog.start_all()
    assert set(states(system).values()) == {"starting"}
    assert system.status["background"] and system.status["port"] == 3000 and system.status["sdrtrunk_bat"] == BAT
    run_checks(dog, system, 7)
    assert states(system) == {"Ollama": "ok", "Transcriber": "ok", "SDRTrunk": "ok"}
    system.up["transcriber"] = False
    run_checks(dog, system, 1)
    assert states(system)["Transcriber"] == "down"


def test_nothing_is_restarted_while_updating():
    dog, system, alerts = make(sdrtrunk_bat=BAT, background=True)
    dog.start_all()
    run_checks(dog, system, 7)
    system.update = True
    system.up["transcriber"] = False
    system.sdrtrunk = []
    run_checks(dog, system, 20)
    assert system.launched == [] and alerts == []
    assert set(states(system).values()) == {"updating"}
    system.update = False
    run_checks(dog, system, 4)
    assert "start-transcriber.bat" in system.launched and "sdr-trunk.bat" in system.launched


def test_restart_everything_from_the_tray_sends_no_alerts():
    dog, system, alerts = make(sdrtrunk_bat=BAT, background=True)
    dog.start_all()
    run_checks(dog, system, 7)
    system.restart = True
    run_checks(dog, system, 1)
    assert system.launched == ["start-ollama.bat", "start-transcriber.bat", "sdr-trunk.bat"]
    assert 500 in system.killed and alerts == []


def test_new_version_folder_is_found_when_the_saved_path_is_gone():
    old = r"E:\B\sdrtrunk\build\image\sdr-trunk-windows-x86_64-v0.6.2-beta-1\bin\sdr-trunk.bat"
    new = r"E:\B\sdrtrunk\build\image\sdr-trunk-windows-x86_64-v1.0.0-beta-1\bin\sdr-trunk.bat"
    dog, system, _ = make(sdrtrunk_bat=old)
    system.dirs = {r"E:\B\sdrtrunk\build\image": ["sdr-trunk-windows-x86_64-v1.0.0-beta-1"]}
    system.files = {new}
    system.sdrtrunk = []
    dog.start_all()
    sdrtrunk = dog.programs[-1]
    assert sdrtrunk.bat == new
    assert system.launched[-1] == "sdr-trunk.bat"


def test_stop_all_from_the_tray_stops_everything_and_the_watchdog():
    dog, system, alerts = make(sdrtrunk_bat=BAT, background=True)
    dog.start_all()
    run_checks(dog, system, 7)
    system.port = {777}
    system.stop = True
    run_checks(dog, system, 1)
    assert dog.stopped and system.status is None
    assert 500 in system.killed and 777 in system.killed
    assert system.killed_names == ["ollama.exe"]
    # Nothing is started again afterwards, and no alerts.
    system.up = {"ollama": False, "transcriber": False}
    system.sdrtrunk = []
    assert system.launched == [] and alerts == []


def test_run_returns_after_stop_all():
    dog, system, _ = make(sdrtrunk_bat=BAT)
    system.stop = True
    dog.run()  # would loop forever without the stop
    assert dog.stopped


def test_stop_all_leaves_ollama_alone_when_summaries_are_off():
    dog, system, _ = make(sdrtrunk_bat=BAT, summaries=False)
    dog.start_all()
    system.stop = True
    dog.tick()
    assert system.killed_names == []


def test_two_sdrtrunk_copies_are_closed_and_one_started_again():
    dog, system, _ = make(sdrtrunk_bat=BAT)
    dog.start_all()
    assert system.launched == []
    # A second copy turns up (started by hand, or by another watchdog): each would get "access denied".
    system.sdrtrunk = [(500, True, JAVA), (501, True, JAVA)]
    system.sleep(dog.settings.startup_s)
    run_checks(dog, system, 1)
    assert set(system.killed) == {500, 501}
    assert system.launched == ["sdr-trunk.bat"]
    assert len(system.sdrtrunk) == 1


def test_two_copies_at_start_are_replaced_by_one():
    dog, system, _ = make(sdrtrunk_bat=BAT)
    system.sdrtrunk = [(500, True, JAVA), (501, True, JAVA)]
    dog.start_all()
    assert set(system.killed) == {500, 501}
    assert system.launched == ["sdr-trunk.bat"]


class LockSystem:
    def __init__(self, free_after=None, status=None):
        self.time = 0.0
        self.free_after = free_after  # seconds until the other watchdog lets go; None = never
        self.status = status
        self.asked_stop = False

    def now(self):
        return self.time

    def sleep(self, seconds):
        self.time += seconds

    def take_lock(self):
        return self.free_after is not None and self.time >= self.free_after

    def read_status(self):
        return self.status

    def ask_stop(self):
        self.asked_stop = True


def test_first_watchdog_takes_the_lock():
    system = LockSystem(free_after=0)
    assert watchdog.claim(system, background=False)
    assert not system.asked_stop


def test_boot_watchdog_gives_way_to_a_running_one():
    system = LockSystem(status={"time": 0, "background": False})
    assert not watchdog.claim(system, background=True)
    assert not system.asked_stop


def test_second_windowed_watchdog_does_not_start_a_second_copy():
    system = LockSystem(status={"time": 0, "background": False})
    assert not watchdog.claim(system, background=False)
    assert not system.asked_stop


def test_windowed_watchdog_takes_over_from_a_hidden_one():
    system = LockSystem(free_after=20, status={"time": 0, "background": True})
    assert watchdog.claim(system, background=False)
    assert system.asked_stop


def test_windowed_watchdog_gives_up_if_the_hidden_one_never_stops():
    system = LockSystem(free_after=None, status={"time": 0, "background": True})
    assert not watchdog.claim(system, background=False)
    assert system.asked_stop


def test_real_lock_keeps_a_second_watchdog_out(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "LOCK_FILE", tmp_path / "watchdog.lock")
    first, second = watchdog.System(), watchdog.System()
    assert first.take_lock()
    assert not second.take_lock()
    first._lock.close()
    assert second.take_lock()
