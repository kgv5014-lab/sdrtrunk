"""The PC speakers player: which stream it asks for and which sound output it picks."""

from sdrtrunk_transcriber import pc_speakers
from sdrtrunk_transcriber.config import Config


def test_url_for_all_talkgroups():
    config = Config()
    assert pc_speakers.stream_url(config) == "http://127.0.0.1:3000/listen/stream.mp3?id=pc-speakers"


def test_url_for_some_talkgroups_and_any_host():
    config = Config()
    config.server.host = "0.0.0.0"
    config.pc_speakers.talkgroups = ["10000", " Countywide ", ""]
    assert pc_speakers.stream_url(config) == \
        "http://127.0.0.1:3000/listen/stream.mp3?id=pc-speakers&tg=10000%2CCountywide"


class FakeSd:
    @staticmethod
    def query_devices():
        return [{"name": "Microphone (USB)", "max_output_channels": 0},
                {"name": "Speakers (Realtek)", "max_output_channels": 2},
                {"name": "Headphones", "max_output_channels": 2}]


def test_device_by_part_of_its_name():
    assert pc_speakers.find_device(FakeSd, "") is None
    assert pc_speakers.find_device(FakeSd, "realtek") == 1
    assert pc_speakers.find_device(FakeSd, "Microphone") is None


def test_edit_mode_uses_the_configured_sdrtrunk_first():
    from sdrtrunk_transcriber import editmode
    assert editmode.find_bat(r" E:\SDR\bin\sdr-trunk.bat ", []) == r"E:\SDR\bin\sdr-trunk.bat"
    assert editmode.find_bat("", []) == ""


def test_quiet_while_sdrtrunk_has_its_window(tmp_path):
    import json
    status = tmp_path / "watchdog-status.json"
    assert not pc_speakers.windowed(status, now=lambda: 1000)  # no watchdog: nothing else plays the calls
    status.write_text(json.dumps({"time": 990, "background": False}))
    assert pc_speakers.windowed(status, now=lambda: 1000)
    assert not pc_speakers.windowed(status, now=lambda: 5000)  # the watchdog stopped long ago
    status.write_text(json.dumps({"time": 990, "background": True}))
    assert not pc_speakers.windowed(status, now=lambda: 1000)
    status.write_text("{half")
    assert not pc_speakers.windowed(status, now=lambda: 1000)
