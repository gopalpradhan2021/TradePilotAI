"""
Covers the login/session flow added to replace the old ?key= URL password. DASHBOARD_PASSWORD
and SESSION_SECRET_KEY must be set before dashboard.app is first imported (it reads them at
module load time), so this file sets them at the top, before the import — safe as long as no
other test module imports dashboard.app first with different values in the same test session.
"""
import os

os.environ["DASHBOARD_PASSWORD"] = "test-password-123"
os.environ["SESSION_SECRET_KEY"] = "test-session-secret-key-not-for-production"

from starlette.testclient import TestClient

from dashboard.app import app, _failed_logins


def make_client():
    _failed_logins.clear()
    return TestClient(app)


def test_unauthenticated_root_redirects_to_login():
    client = make_client()
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_page_renders_form():
    client = make_client()
    response = client.get("/login")
    assert response.status_code == 200
    assert 'name="password"' in response.text


def test_wrong_password_rejected_with_no_session():
    client = make_client()
    response = client.post("/login", data={"password": "wrong"})
    assert "Incorrect password" in response.text

    status_response = client.get("/api/status")
    assert status_response.status_code == 401


def test_correct_password_grants_session():
    client = make_client()
    login_response = client.post("/login", data={"password": "test-password-123"},
                                  follow_redirects=False)
    assert login_response.status_code == 303
    assert login_response.headers["location"] == "/"

    status_response = client.get("/api/status")
    assert status_response.status_code == 200


def test_session_persists_across_requests_on_same_client():
    client = make_client()
    client.post("/login", data={"password": "test-password-123"})

    assert client.get("/").status_code == 200
    assert client.get("/backtest").status_code == 200
    assert client.get("/api/status").status_code == 200


def test_logout_clears_session():
    client = make_client()
    client.post("/login", data={"password": "test-password-123"})
    assert client.get("/api/status").status_code == 200

    client.get("/logout", follow_redirects=False)

    assert client.get("/api/status").status_code == 401


def test_throttle_blocks_after_max_failed_attempts():
    client = make_client()
    for _ in range(5):
        client.post("/login", data={"password": "wrong"})

    # 6th attempt, even with the CORRECT password, must still be throttled.
    response = client.post("/login", data={"password": "test-password-123"})
    assert "Too many failed attempts" in response.text

    status_response = client.get("/api/status")
    assert status_response.status_code == 401


def test_api_halt_requires_auth():
    client = make_client()
    response = client.post("/api/halt")
    assert response.status_code == 401
