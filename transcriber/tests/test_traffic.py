import requests

from sdrtrunk_transcriber.config import Config
from sdrtrunk_transcriber.traffic import Traffic511, decode_polyline

NOW = 1_792_000_000

# Shapes as 511PA sent them to Ken's PC on 2026-10-26, trimmed.
ICONS = {
    "ActiveRoadwork": [{"itemId": "423362", "location": [40.754712, -75.269579], "icon": {}, "title": ""},
                       {"itemId": "122369", "location": [41.381607, -75.724396], "icon": {}, "title": ""}],
    "OtherRouteIncident": [{"itemId": "500001", "location": [40.6301, -75.3702], "icon": {}, "title": ""}],
    "Cameras": [{"itemId": "4001", "location": [40.6185, -75.3920],
                 "icon": {"url": "/Generated/Content/Images/511/map_cameraStreams.svg"}, "title": ""},
                {"itemId": "1", "location": [40.30142, -75.90045], "icon": {}, "title": ""}],
}
DETAILS = {
    "/map/data/ActiveRoadwork/423362": {
        "id": 423362, "description": "ROADWORK on PA-191 Northbound At Exit PA 33 SOUTH - EASTON. Entry Ramp Blocked.",
        "eventType": "roadwork", "roadway": "PA-191", "direction": "N", "isFullClosure": False,
        "startDate": "2026-10-02T17:57:00+00:00", "lastUpdated": "2026-10-02T17:58:00+00:00",
        "latitude": 40.754712, "longitude": -75.269579, "locationDescription": "Exit PA 33 SOUTH - EASTON - ",
        "laneDescription": "Ramp Closure", "area": {"areaName": {"text": "Northampton"}}},
    "/map/data/OtherRouteIncident/500001": {
        "id": 500001, "description": "Crash on US 22 Eastbound at PA 378. Right lane closed.",
        "eventType": "multi vehicle crash", "roadway": "US 22", "direction": "E", "isFullClosure": False,
        "startDate": "2026-10-26T20:30:00+00:00", "lastUpdated": "2026-10-26T20:41:00+00:00",
        "latitude": 40.6301, "longitude": -75.3702, "polyline": "_p~iF~ps|U_ulLnnqC_mqNvxq`@",
        "laneDescription": "right lane closed", "area": {"areaName": {"text": "Lehigh"}}},
    "/map/data/Cameras/4001": {
        "id": 4001, "roadway": "US 22", "location": "US 22 @ PA 378", "images": [
            {"id": 7001, "description": "Traffic closest to camera is traveling Eastbound", "imageUrl": "/map/Cctv/7001",
             "videoUrl": "https://pa-se3.arcadis-ivds.com:8200/chan-3207/index.m3u8", "isVideoAuthRequired": True,
             "videoDisabled": False, "disabled": False, "blocked": False},
            {"id": 7002, "description": "Westbound", "imageUrl": "/map/Cctv/7002", "videoUrl": None,
             "disabled": False, "blocked": False},
            {"id": 7003, "description": "Off", "disabled": True, "blocked": False}]},
}


class Reply:
    def __init__(self, data=None, text="", content=b"", headers=None, status=200):
        self.data, self.text, self.content, self.status = data, text, content, status
        self.headers = headers or {}

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(str(self.status))


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        path = url.replace("https://www.511pa.com", "")
        self.calls.append(path)
        if path.startswith("/map/mapIcons/"):
            return Reply({"item1": {}, "item2": ICONS.get(path.rsplit("/", 1)[1], [])})
        if path in DETAILS:
            return Reply(DETAILS[path])
        if path.startswith("/map/Cctv/"):
            return Reply(content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"})
        return Reply(status=404)


def make(**settings):
    config = Config()
    config.traffic.enabled = True
    for key, value in settings.items():
        setattr(config.traffic, key, value)
    session = FakeSession()
    return Traffic511(config, session), session


def test_polyline_decodes_googles_example():
    assert decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@") == [[38.5, -120.2], [40.7, -120.95], [43.252, -126.453]]


def test_only_events_in_the_map_area_are_kept_with_their_details():
    traffic, session = make()
    traffic.poll(NOW)
    events = traffic.events()
    assert [e["id"] for e in events] == ["500001", "423362"]  # crashes before roadwork
    crash, roadwork = events
    assert crash["kind"] == "incident" and crash["county"] == "Lehigh" and crash["type"] == "multi vehicle crash"
    assert len(crash["path"]) == 3
    assert roadwork["kind"] == "roadwork" and roadwork["county"] == "Northampton"
    assert "/map/data/ActiveRoadwork/122369" not in session.calls  # Scranton area


def test_details_are_not_fetched_again_until_stale():
    traffic, session = make()
    traffic.poll(NOW)
    first = session.calls.count("/map/data/OtherRouteIncident/500001")
    traffic.poll(NOW + 180)
    assert session.calls.count("/map/data/OtherRouteIncident/500001") == first
    traffic.poll(NOW + 3600)
    assert session.calls.count("/map/data/OtherRouteIncident/500001") == first + 1


def test_cameras_keep_working_views_and_link_to_511pa_for_video():
    traffic, session = make()
    traffic.poll(NOW)
    [camera] = traffic.cameras()
    assert camera["name"] == "US 22 @ PA 378"
    assert [(v["image_id"], v["live"]) for v in camera["views"]] == [(7001, True), (7002, False)]
    assert camera["page"] == "https://www.511pa.com/map#camera-4001"
    assert traffic.known_image(7001) and not traffic.known_image(7003)
    assert traffic.image(7001) == (b"\xff\xd8jpeg", "image/jpeg")


def test_cameras_can_be_turned_off():
    traffic, session = make(cameras=False)
    traffic.poll(NOW)
    assert traffic.cameras() == []
    assert "/map/mapIcons/Cameras" not in session.calls
