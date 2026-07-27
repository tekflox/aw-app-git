"""End-to-end test of the /device/start + /device/poll routes through a real
FastAPI TestClient, with device_flow.start/poll monkeypatched (no real network
calls) and a minimal fake ``ctx`` (secrets facade only — the pieces the device
routes actually touch).

Run: .venv/aw/bin/python -m pytest tests/test_plugin_routes.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from git_app import device_flow, gh_auth, plugin  # noqa: E402


class FakeSecrets:
    def __init__(self):
        self.store: dict[str, str] = {}

    def read(self, key):
        return self.store.get(key)

    def write(self, key, value):
        self.store[key] = value
        return {"key": key, "written": True}

    def keys(self):
        return list(self.store)


class FakeCtx:
    def __init__(self, config=None):
        self.secrets = FakeSecrets()
        self.config = config or {}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(gh_auth, "login_with_token", lambda token: "Logged in as testuser")
    ctx = FakeCtx(config={"oauth_client_id": "test-client-id"})
    app = plugin.GitAppPlugin()
    api = app._build_routes(ctx)
    return TestClient(api), ctx


def test_device_start_not_configured(monkeypatch):
    monkeypatch.setattr(gh_auth, "login_with_token", lambda token: "ok")
    ctx = FakeCtx(config={})
    monkeypatch.delenv("AW_APP_GIT_OAUTH_CLIENT_ID", raising=False)
    app = plugin.GitAppPlugin()
    tc = TestClient(app._build_routes(ctx))
    resp = tc.post("/device/start")
    assert resp.json()["error"] == "not_configured"


def test_device_start_success(client, monkeypatch):
    tc, ctx = client

    def fake_start(client_id, scope=device_flow.DEFAULT_SCOPE):
        assert client_id == "test-client-id"
        return {
            "device_code": "dev-xyz",
            "user_code": "WDJB-MJHT",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        }

    monkeypatch.setattr(device_flow, "start", fake_start)
    resp = tc.post("/device/start")
    body = resp.json()
    assert body["user_code"] == "WDJB-MJHT"
    assert body["verification_uri"] == "https://github.com/login/device"
    assert body["interval"] == 5


def test_device_poll_without_start_returns_error(client):
    tc, _ = client
    resp = tc.post("/device/poll")
    assert resp.json()["status"] == "error"


def test_device_poll_pending_then_success(client, monkeypatch):
    tc, ctx = client
    monkeypatch.setattr(
        device_flow,
        "start",
        lambda client_id, scope=device_flow.DEFAULT_SCOPE: {
            "device_code": "dev-xyz",
            "user_code": "WDJB-MJHT",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        },
    )
    tc.post("/device/start")

    monkeypatch.setattr(
        device_flow, "poll", lambda client_id, device_code: {"status": "authorization_pending"}
    )
    resp = tc.post("/device/poll")
    assert resp.json()["status"] == "authorization_pending"

    monkeypatch.setattr(
        device_flow,
        "poll",
        lambda client_id, device_code: {"status": "success", "access_token": "gho_realtoken"},
    )
    resp = tc.post("/device/poll")
    body = resp.json()
    assert body["status"] == "success"
    assert body["logged_in"] is True
    assert ctx.secrets.read("github_token") == "gho_realtoken"

    # pending state cleared — a further poll with no new /device/start errors
    resp = tc.post("/device/poll")
    assert resp.json()["status"] == "error"


def test_device_poll_slow_down(client, monkeypatch):
    tc, _ = client
    monkeypatch.setattr(
        device_flow,
        "start",
        lambda client_id, scope=device_flow.DEFAULT_SCOPE: {
            "device_code": "dev-xyz",
            "user_code": "WDJB-MJHT",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        },
    )
    tc.post("/device/start")
    monkeypatch.setattr(
        device_flow, "poll", lambda client_id, device_code: {"status": "slow_down", "interval": 10}
    )
    resp = tc.post("/device/poll")
    body = resp.json()
    assert body["status"] == "slow_down"
    assert body["interval"] == 10


def test_device_poll_expired(client, monkeypatch):
    tc, _ = client
    monkeypatch.setattr(
        device_flow,
        "start",
        lambda client_id, scope=device_flow.DEFAULT_SCOPE: {
            "device_code": "dev-xyz",
            "user_code": "WDJB-MJHT",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        },
    )
    tc.post("/device/start")
    monkeypatch.setattr(device_flow, "poll", lambda client_id, device_code: {"status": "expired_token"})
    resp = tc.post("/device/poll")
    assert resp.json()["status"] == "expired_token"

    # cleared — next poll errors again until a fresh /device/start
    resp = tc.post("/device/poll")
    assert resp.json()["status"] == "error"


def test_device_poll_access_denied(client, monkeypatch):
    tc, _ = client
    monkeypatch.setattr(
        device_flow,
        "start",
        lambda client_id, scope=device_flow.DEFAULT_SCOPE: {
            "device_code": "dev-xyz",
            "user_code": "WDJB-MJHT",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        },
    )
    tc.post("/device/start")
    monkeypatch.setattr(device_flow, "poll", lambda client_id, device_code: {"status": "access_denied"})
    resp = tc.post("/device/poll")
    assert resp.json()["status"] == "access_denied"
