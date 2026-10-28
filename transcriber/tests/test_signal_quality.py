"""Signal quality per dongle: SDRTrunk's hourly reports in, a phone alert when a dongle gets worse than normal."""

from fastapi.testclient import TestClient

from sdrtrunk_transcriber.config import Config, SignalConfig
from sdrtrunk_transcriber.server import create_app
from sdrtrunk_transcriber.signal_quality import find_problems, parse_rows

from test_alerts import lines, make_config, session  # noqa: F401  (pytest fixture)
from test_incidents import Radio

HOUR = 3600
START = 1_793_000_000 - 1_793_000_000 % HOUR


def control(good=97.0, lost=3.0, ppm=0.42, change=0.0, usb=0, tuner="R820T 00000011"):
    return {"tuner": tuner, "status": "Enabled", "channel": "Control South", "decoder": "P25-1",
            "frequency": 852550000, "minutes": 60, "messagesGood": int(good * 400), "messagesBad": int(
                (100 - good) * 400), "goodPercent": good, "syncLostSeconds": lost, "signalDb": -42.0,
            "noiseDb": None, "ppm": ppm, "ppmChange": change, "channelErrorHz": 12, "usbErrors": usb}


def fire(signal=-61.0, noise=-98.0, tuner="R828D 00000003"):
    return {"tuner": tuner, "status": "Enabled", "channel": "Nancy FD", "decoder": "NBFM", "frequency": 502425000,
            "minutes": 60, "messagesGood": 0, "messagesBad": 0, "goodPercent": None, "syncLostSeconds": 0.0,
            "signalDb": signal, "noiseDb": noise, "ppm": -1.5, "ppmChange": 0.0, "channelErrorHz": None,
            "usbErrors": 0}


def report(hour, *rows):
    return {"key": "", "hourStart": START + hour * HOUR, "rows": list(rows)}


def history(hours=8):
    rows = []
    for hour in range(hours):
        rows += parse_rows(report(hour, control(good=96.0 + hour % 3), fire()))[1]
    return rows


def kinds(problems):
    return sorted((p.tuner, p.kind) for p in problems)


def test_a_normal_hour_is_fine():
    _, rows = parse_rows(report(8, control(), fire()))
    assert find_problems(rows, history(), [], SignalConfig()) == []


def test_each_kind_of_problem_is_found():
    settings = SignalConfig()
    _, rows = parse_rows(report(8, control(good=75.0, lost=400.0, change=1.3, usb=9), fire(signal=-75.0, noise=-85.0)))
    problems = find_problems(rows, history(), [], settings)
    assert kinds(problems) == [("R820T 00000011", "decode"), ("R820T 00000011", "drift"),
                               ("R820T 00000011", "sync"), ("R820T 00000011", "usb"),
                               ("R828D 00000003", "noise"), ("R828D 00000003", "signal")]
    decode = next(p for p in problems if p.kind == "decode")
    assert decode.text == "Control South: decoded 75% of messages, normally 97%."
    signal = next(p for p in problems if p.kind == "signal")
    assert signal.text == "Nancy FD: signal 14 dB weaker than normal (-75 dB, normally -61 dB)."


def test_no_alerts_until_there_is_a_normal_to_compare_with():
    _, rows = parse_rows(report(8, control(good=50.0), fire(signal=-90.0)))
    assert find_problems(rows, history(hours=3), [], SignalConfig()) == []


def test_drift_away_from_normal_and_short_hours():
    _, rows = parse_rows(report(8, control(ppm=3.0)))
    assert kinds(find_problems(rows, history(), [], SignalConfig())) == [("R820T 00000011", "drift")]
    # A channel that only ran a few minutes isn't judged.
    short = control(good=10.0)
    short["minutes"] = 5
    _, rows = parse_rows(report(8, short))
    assert find_problems(rows, history(), [], SignalConfig()) == []


def test_a_dongle_that_disappears_or_errors():
    previous = parse_rows(report(7, control(), fire()))[1]
    _, rows = parse_rows(report(8, control()))
    assert kinds(find_problems(rows, history(), previous, SignalConfig())) == [("R828D 00000003", "missing")]
    errored = {"tuner": "R828D 00000003", "status": "Error", "channel": None, "minutes": 0}
    _, rows = parse_rows(report(8, control(), errored))
    assert kinds(find_problems(rows, history(), previous, SignalConfig())) == [("R828D 00000003", "error")]


def test_reports_alert_once_per_problem_until_it_clears(tmp_path, session):  # noqa: F811
    config = make_config(tmp_path, bark_key="abc")
    client = TestClient(create_app(config, backend_factory=lambda _c: Radio([])))
    with client:
        for hour in range(8):
            reply = client.post("/api/signal", json=report(hour, control(), fire()))
            assert reply.status_code == 200 and reply.json()["stored"] == 2
        bad = client.post("/api/signal", json=report(8, control(good=70.0), fire())).json()
        again = client.post("/api/signal", json=report(9, control(good=70.0), fire())).json()
        fine = client.post("/api/signal", json=report(10, control(), fire())).json()
        back = client.post("/api/signal", json=report(11, control(good=70.0), fire())).json()
        client.app.state.alerter.flush()
        sent = lines(client.get("/sdrtrunk-alerts/json", params={"poll": 1, "since": "all"}))
        stored = client.get("/api/signal", params={"hours": 10 ** 6}).json()
        assert client.post("/api/signal", json={"rows": []}).status_code == 400

    assert (bad["alerted"], again["alerted"], fine["alerted"], back["alerted"]) == (1, 0, 0, 1)
    assert [m["title"] for m in sent] == ["Dongle problem: R820T 00000011"] * 2
    assert sent[0]["message"] == ("Control South: decoded 70% of messages, normally 97%.\n"
                                  "Check the antenna, its cable and connectors.")
    assert len(session.bark) == 2
    assert len(stored) == 24


def test_reports_are_kept_without_phone_alerts(tmp_path):
    config = Config()
    config.base_dir = tmp_path
    config.map.enabled = False
    client = TestClient(create_app(config, backend_factory=lambda _c: Radio([])))
    with client:
        for hour in range(9):
            reply = client.post("/api/signal", json=report(hour, control(good=70.0 if hour == 8 else 97.0)))
        assert reply.json()["problems"] == ["Control South: decoded 70% of messages, normally 97%."]
