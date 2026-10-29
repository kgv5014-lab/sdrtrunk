"""The friends' map link: hidden path, password, lockouts, and nothing but read-only guest addresses."""

import pytest
from fastapi.testclient import TestClient

from sdrtrunk_transcriber import live, share
from test_service import CALL, make_client, make_mp3, post, sdrtrunk_body  # noqa: F401 (fixture)

PASSWORD = "correct-horse-battery"


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(share, "WRONG_DELAY_S", 0)
    # Fast hashing for tests; the real cost is checked in test_password_hash_is_salted_scrypt.
    monkeypatch.setattr(share, "SCRYPT_N", 2 ** 10)


def gate_client(client, data):
    gate = share.ShareGate(live.create_public_app(client.app, data), client.app, data)
    return TestClient(gate, base_url="https://testserver", follow_redirects=False)


def sign_in(guest, prefix, password=PASSWORD, ip="203.0.113.5"):
    return guest.post(f"{prefix}/login", data={"password": password}, headers={"X-Forwarded-For": ip})


def test_password_hash_is_salted_scrypt():
    stored = share.hash_password(PASSWORD)
    assert stored.startswith("scrypt$") and PASSWORD not in stored
    assert stored != share.hash_password(PASSWORD)
    assert share.check_password(PASSWORD, stored)
    assert not share.check_password(PASSWORD + "x", stored)
    assert not share.check_password(PASSWORD, "garbage")
    made = share.make_password()
    assert len(made) == 19 and made.count("-") == 3


def test_nothing_is_there_until_a_password_is_set(make_client, tmp_path):
    client, _ = make_client()
    data = tmp_path / "data"
    with client:
        guest = gate_client(client, data)
        prefix = f"/g/{share.read_path(data)}"
        for path in ("/map", "/login", "", "/api/calls"):
            assert guest.get(prefix + path).status_code == 404, path
        # The listen-only page keeps working as before.
        key = client.get("/api/listen/link").json()["key"]
        assert guest.get(f"/listen?key={key}").status_code == 200


def test_guest_signs_in_and_sees_the_map_read_only(make_client, tmp_path):
    client, _ = make_client()
    data = tmp_path / "data"
    share.set_password(data, PASSWORD)
    with client:
        assert post(client, sdrtrunk_body(CALL, make_mp3(1.0))).status_code == 200
        guest = gate_client(client, data)
        token = share.read_path(data)
        prefix = f"/g/{token}"
        assert len(token) >= 30

        # Wrong or guessed paths look like nothing is there.
        assert guest.get("/g/wrong/map").status_code == 404
        assert guest.get(f"/g/{token[:-1]}/map").status_code == 404

        # Not signed in: pages go to the login, data is refused.
        assert guest.get(prefix + "/map").headers["location"] == prefix + "/login"
        assert guest.get(prefix + "/api/calls").status_code == 401
        login = guest.get(prefix + "/login")
        assert login.status_code == 200 and 'type="password"' in login.text
        assert login.headers["referrer-policy"] == "no-referrer"

        assert sign_in(guest, prefix, "nope").status_code == 401
        signed = sign_in(guest, prefix)
        assert signed.status_code == 303 and signed.headers["location"] == prefix + "/map"
        cookie = signed.headers["set-cookie"].lower()
        assert "httponly" in cookie and "secure" in cookie and f"path={prefix.lower()}/" in cookie

        page = guest.get(prefix + "/map")
        assert page.status_code == 200 and 'data-guest="1"' in page.text
        assert guest.get(prefix + "/").status_code == 200
        assert guest.get(prefix + "/listen").status_code == 200
        calls = guest.get(prefix + "/api/calls?limit=5000")
        assert calls.status_code == 200 and calls.json()[0]["talkgroup"] == "1203"
        assert guest.get(prefix + f"/api/calls/{calls.json()[0]['id']}/audio").status_code == 200
        assert guest.get(prefix + "/api/incidents?hidden=true").status_code == 200
        assert guest.get(prefix + "/listen/talkgroups").status_code == 200

        # Admin pages, settings and every change are out of reach.
        for path in ("/units", "/api/status", "/api/units", "/api/corrections", "/api/listen/link",
                     "/api/aircraft", "/docs", "/openapi.json", "/api/calls/1/../../api/status"):
            assert guest.get(prefix + path).status_code == 404, path
        assert guest.post(prefix + "/api/calls/1/transcript", json={"text": "x"}).status_code == 405
        assert guest.delete(prefix + "/api/incidents/1").status_code == 405
        assert guest.post(prefix + "/api/listen/new-link").status_code == 405


def test_new_password_or_turning_off_signs_everyone_out(make_client, tmp_path):
    client, _ = make_client()
    data = tmp_path / "data"
    share.set_password(data, PASSWORD)
    with client:
        guest = gate_client(client, data)
        prefix = f"/g/{share.read_path(data)}"
        sign_in(guest, prefix)
        assert guest.get(prefix + "/api/calls").status_code == 200

        share.set_password(data, "another-long-password")
        assert guest.get(prefix + "/api/calls").status_code == 401
        assert sign_in(guest, prefix, "another-long-password").status_code == 303
        assert guest.get(prefix + "/api/calls").status_code == 200

        share.turn_off(data)
        assert guest.get(prefix + "/api/calls").status_code == 404
        assert guest.get(prefix + "/login").status_code == 404


def test_forged_or_expired_cookie_is_refused(make_client, tmp_path):
    client, _ = make_client()
    data = tmp_path / "data"
    share.set_password(data, PASSWORD)
    with client:
        guest = gate_client(client, data)
        prefix = f"/g/{share.read_path(data)}"
        for value in ("9999999999.abc", "1.x", "", "garbage"):
            guest.cookies.set("guest", value)
            assert guest.get(prefix + "/api/calls").status_code == 401, value


def test_too_many_wrong_passwords_locks_that_address_then_everyone(make_client, tmp_path):
    client, _ = make_client()
    data = tmp_path / "data"
    share.set_password(data, PASSWORD)
    with client:
        guest = gate_client(client, data)
        prefix = f"/g/{share.read_path(data)}"
        for _ in range(share.IP_TRIES):
            assert sign_in(guest, prefix, "wrong-guess", ip="198.51.100.1").status_code == 401
        # Locked: even the right password is refused from that address for now.
        assert sign_in(guest, prefix, ip="198.51.100.1").status_code == 429
        # A made-up X-Forwarded-For in front doesn't get around it; Tailscale's own entry is last.
        assert sign_in(guest, prefix, ip="10.9.9.9, 198.51.100.1").status_code == 429
        assert sign_in(guest, prefix, ip="198.51.100.2").status_code == 303

        # Spread over many addresses, enough wrong tries pause all logins.
        for i in range(share.ALL_TRIES):
            sign_in(guest, prefix, "wrong-guess", ip=f"192.0.2.{i}")
        assert sign_in(guest, prefix, ip="198.51.100.3").status_code == 429
        # Friends already signed in keep their view.
        assert guest.get(prefix + "/api/calls").status_code == 200


def test_private_listen_page_shows_the_friends_link(make_client, tmp_path):
    client, _ = make_client()
    data = tmp_path / "data"
    with client:
        assert client.get("/api/listen/link").json()["share_path"] is None
        share.set_password(data, PASSWORD)
        assert client.get("/api/listen/link").json()["share_path"] == f"/g/{share.read_path(data)}/map"
