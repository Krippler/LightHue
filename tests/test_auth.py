import pytest


def set_password(client, new, current=None):
    body = {"new_password": new}
    if current is not None:
        body["current_password"] = current
    return client.put("/api/auth/password", json=body)


def test_console_is_open_by_default(client):
    assert client.get("/api/auth").json() == {"required": False, "authenticated": True}
    assert client.get("/api/settings").status_code == 200


def test_setting_a_password_gates_the_api(client):
    assert set_password(client, "quaddamage").status_code == 200
    assert client.get("/api/auth").json()["required"] is True
    # The caller who set it keeps working, on the session cookie handed back.
    assert client.get("/api/settings").status_code == 200

    client.cookies.clear()
    assert client.get("/api/settings").status_code == 401
    assert client.get("/api/lights").status_code == 401
    assert client.post("/api/flicker/stop", json={}).status_code == 401


def test_public_endpoints_stay_reachable_when_locked(client):
    set_password(client, "quaddamage")
    client.cookies.clear()
    assert client.get("/").status_code == 200
    assert client.get("/api/auth").json() == {"required": True, "authenticated": False}
    assert client.get("/static/app.js").status_code == 200


def test_login_with_the_right_password(client):
    set_password(client, "quaddamage")
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "quaddamage"}).status_code == 200
    assert client.get("/api/settings").status_code == 200
    assert client.get("/api/auth").json()["authenticated"] is True


def test_logout_drops_the_session(client):
    set_password(client, "quaddamage")
    client.post("/api/auth/logout")
    client.cookies.clear()
    assert client.get("/api/settings").status_code == 401


def test_header_auth_for_scripts(client):
    set_password(client, "quaddamage")
    client.cookies.clear()
    assert client.get("/api/settings", headers={"X-Console-Password": "quaddamage"}).status_code == 200
    assert client.get("/api/settings", headers={"Authorization": "Bearer quaddamage"}).status_code == 200
    assert client.get("/api/settings", headers={"X-Console-Password": "nope"}).status_code == 401


def test_changing_the_password_requires_the_old_one(client):
    set_password(client, "quaddamage")
    assert set_password(client, "pentagram").status_code == 403
    assert set_password(client, "pentagram", current="wrong").status_code == 403
    assert set_password(client, "pentagram", current="quaddamage").status_code == 200
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"password": "pentagram"}).status_code == 200


def test_changing_the_password_signs_other_sessions_out(client, app_modules):
    set_password(client, "quaddamage")
    stale = list(app_modules.auth._sessions)[0]
    set_password(client, "pentagram", current="quaddamage")
    assert stale not in app_modules.auth._sessions


def test_removing_the_password_reopens_the_console(client):
    set_password(client, "quaddamage")
    assert client.request("DELETE", "/api/auth/password",
                          json={"current_password": "wrong"}).status_code == 403
    assert client.request("DELETE", "/api/auth/password",
                          json={"current_password": "quaddamage"}).status_code == 200
    client.cookies.clear()
    assert client.get("/api/auth").json()["required"] is False
    assert client.get("/api/settings").status_code == 200


def test_password_is_never_stored_in_the_clear(client, app_modules):
    set_password(client, "quaddamage")
    import json
    with open(app_modules.config_store.CONFIG_PATH) as f:
        raw = f.read()
    assert "quaddamage" not in raw
    record = json.loads(raw)["auth"]
    assert record["algo"] == "pbkdf2_sha256"
    assert record["iterations"] >= 100_000
    assert len(record["salt"]) == 32


def test_short_passwords_are_rejected(client):
    assert set_password(client, "abc").status_code == 422
    assert client.get("/api/auth").json()["required"] is False


def test_websocket_is_gated(client):
    from starlette.websockets import WebSocketDisconnect

    set_password(client, "quaddamage")
    client.cookies.clear()
    with pytest.raises(WebSocketDisconnect) as excinfo, client.websocket_connect("/ws"):
        pass
    assert excinfo.value.code == 1008   # policy violation


def test_websocket_allowed_once_authenticated(client):
    set_password(client, "quaddamage")
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "status"
