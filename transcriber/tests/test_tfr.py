"""FAA TFR check before a drone launch alert (tfr.py)."""

import json
import time
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from sdrtrunk_transcriber import tfr
from sdrtrunk_transcriber.server import create_app
from sdrtrunk_transcriber.zones import read_zones

from test_alerts import lines, make_config
from test_incidents import Radio
from test_service import CALL, make_mp3, post, sdrtrunk_body

HOME = (40.6915, -75.21)


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def square(lat, lon, half_deg):
    ring = [[lon - half_deg, lat - half_deg], [lon + half_deg, lat - half_deg], [lon + half_deg, lat + half_deg],
            [lon - half_deg, lat + half_deg], [lon - half_deg, lat - half_deg]]
    return {"type": "Polygon", "coordinates": [ring]}


def detail(start, end):
    return f"""<XNOTAM-Update><Group><Add><Not><dateEffective>{iso(start)}</dateEffective>
      <dateExpire>{iso(end)}</dateExpire><TfrNot><TFRAreaGroup><aseTFRArea><ScheduleGroup>
      <isTimeSeparate>FALSE</isTimeSeparate><dateEffective>{iso(start)}</dateEffective>
      <dateExpire>{iso(end)}</dateExpire></ScheduleGroup></aseTFRArea></TFRAreaGroup></TfrNot>
      </Not></Add></Group></XNOTAM-Update>"""


class Reply:
    def __init__(self, body):
        self.body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def raise_for_status(self):
        if self.body is None:
            raise OSError("down")

    def json(self):
        return self.body


class FaaSite:
    def __init__(self, features=(), details=None, down=False):
        self.features, self.details, self.down = list(features), details or {}, down
        self.gets = []

    def get(self, url, timeout=None):
        self.gets.append(url)
        if self.down:
            raise OSError("no route to host")
        if "geoserver" in url:
            return Reply({"type": "FeatureCollection", "features": self.features})
        return Reply(self.details.get(url.rsplit("detail_", 1)[-1][:-4]))


def feature(notam, geometry, title="Bethlehem, PA", legal="VIP"):
    return {"type": "Feature", "id": f"V_TFR_LOC.{notam}", "geometry": geometry,
            "properties": {"NOTAM_KEY": f"{notam}-1-FDC-F", "TITLE": title, "LEGAL": legal}}


def test_shapes_reaching_the_zone():
    lat, lon = HOME
    assert tfr.ring_reaches_circle(square(lat, lon, 0.2)["coordinates"][0], lat, lon, 800)   # zone inside
    near = square(lat, lon + 0.02, 0.005)["coordinates"][0]                                # ~1.3 km east
    assert tfr.ring_reaches_circle(near, lat, lon, 1300)
    assert not tfr.ring_reaches_circle(near, lat, lon, 800)
    assert not tfr.ring_reaches_circle(square(26.07, -97.18, 0.05)["coordinates"][0], lat, lon, 800)


def test_times_and_margin():
    now = time.time()
    lat, lon = HOME
    site = FaaSite([feature("6/1111", square(lat, lon, 0.1)), feature("6/2222", square(26.07, -97.18, 0.05)),
                    feature("6/3333", square(lat, lon, 0.1))],
                   {"6_1111": detail(now + 20 * 60, now + 3 * 3600), "6_3333": detail(now - 5 * 3600, now - 2 * 3600)})
    check = tfr.TfrCheck(session=site, margin_minutes=30)
    found = check.check(lat, lon, 1200, now)
    assert found.ok and [t.notam for t, _w in found.tfrs] == ["6/1111"]      # starts in 20 min; other ended 2 h ago
    assert "TFR 6/1111 (Vip)" in found.describe()
    assert not check.check(lat, lon, 1200, now - 3600).tfrs                   # an hour earlier: 80 min before start
    assert [t.notam for t, _w in check.check(lat, lon, 1200, now - 2.4 * 3600).tfrs] == ["6/3333"]  # ended 24 min before
    # Shapes and details were fetched once, and only for TFRs reaching the zone.
    assert len(site.gets) == 3 and not any("2222" in u for u in site.gets)


def test_faa_site_down():
    found = tfr.TfrCheck(session=FaaSite(down=True)).check(*HOME, 1200)
    assert not found.ok and not found.tfrs


def zone_alert(tmp_path, monkeypatch, site):
    monkeypatch.setattr(tfr.requests, "Session", lambda: site)
    config = make_config(tmp_path, incidents=[], tfr_check=True, zones=[
        {"name": "Downtown", "lat": HOME[0], "lon": HOME[1], "radius_miles": 0.5, "prefix": "DRONE LAUNCH REQUESTED"}])
    now = int(time.time())
    client = TestClient(create_app(config, backend_factory=lambda _c: Radio(
        ["Engine 41 respond North Third and Northampton for a fall."])))
    with client:
        post(client, sdrtrunk_body({**CALL, "dateTime": str(now - 60)}, make_mp3(2.0)))
        assert client.app.state.worker.wait_idle()
        assert client.app.state.map_worker.wait_idle()
        return lines(client.get("/sdrtrunk-alerts/json", params={"poll": 1}))


def test_a_tfr_over_the_zone_drops_drone_launch(tmp_path, monkeypatch):
    now = time.time()
    site = FaaSite([feature("6/1111", square(*HOME, 0.1))], {"6_1111": detail(now - 3600, now + 3600)})
    [sent] = zone_alert(tmp_path, monkeypatch, site)
    assert sent["title"] == "Fall (TFR active, no drone)"
    assert sent["priority"] == 4
    assert "TFR 6/1111" in sent["message"]


def test_no_tfr_and_site_down_keep_drone_launch(tmp_path, monkeypatch):
    [sent] = zone_alert(tmp_path, monkeypatch, FaaSite())
    assert sent["title"] == "DRONE LAUNCH REQUESTED - Fall" and sent["priority"] == 5
    assert "TFR" not in sent["message"]


def test_site_down_says_so(tmp_path, monkeypatch):
    [sent] = zone_alert(tmp_path, monkeypatch, FaaSite(down=True))
    assert sent["title"] == "DRONE LAUNCH REQUESTED - Fall"
    assert "TFR check unavailable" in sent["message"]


def test_zone_config_still_reads():
    assert read_zones([{"name": "Home", "lat": 1, "lon": 2}])[0].prefix == ""
