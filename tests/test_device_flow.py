"""Unit tests for git_app/device_flow.py — mocks the 2 GitHub endpoints
(device/code, oauth/access_token), no real network calls.

Run: .venv/aw/bin/python -m pytest tests/test_device_flow.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from git_app import device_flow  # noqa: E402


def _resp(payload, status=200):
    return httpx.Response(
        status, json=payload, request=httpx.Request("POST", "https://example.com")
    )


def test_start_no_client_id():
    with pytest.raises(device_flow.DeviceFlowError):
        device_flow.start("")


def test_start_success(monkeypatch):
    def fake_post(url, data=None, headers=None, timeout=None):
        assert url == device_flow.DEVICE_CODE_URL
        assert data["client_id"] == "abc123"
        assert data["scope"] == device_flow.DEFAULT_SCOPE
        return _resp(
            {
                "device_code": "dev-xyz",
                "user_code": "WDJB-MJHT",
                "verification_uri": "https://github.com/login/device",
                "expires_in": 900,
                "interval": 5,
            }
        )

    monkeypatch.setattr(device_flow.httpx, "post", fake_post)
    result = device_flow.start("abc123")
    assert result["user_code"] == "WDJB-MJHT"
    assert result["device_code"] == "dev-xyz"


def test_start_error_from_github(monkeypatch):
    monkeypatch.setattr(
        device_flow.httpx,
        "post",
        lambda *a, **k: _resp({"error": "invalid_client", "error_description": "bad client_id"}),
    )
    with pytest.raises(device_flow.DeviceFlowError, match="bad client_id"):
        device_flow.start("bad-id")


def test_poll_authorization_pending(monkeypatch):
    monkeypatch.setattr(
        device_flow.httpx, "post", lambda *a, **k: _resp({"error": "authorization_pending"})
    )
    result = device_flow.poll("abc123", "dev-xyz")
    assert result["status"] == "authorization_pending"


def test_poll_slow_down(monkeypatch):
    monkeypatch.setattr(device_flow.httpx, "post", lambda *a, **k: _resp({"error": "slow_down"}))
    result = device_flow.poll("abc123", "dev-xyz")
    assert result["status"] == "slow_down"


def test_poll_expired_token(monkeypatch):
    monkeypatch.setattr(
        device_flow.httpx, "post", lambda *a, **k: _resp({"error": "expired_token"})
    )
    result = device_flow.poll("abc123", "dev-xyz")
    assert result["status"] == "expired_token"


def test_poll_access_denied(monkeypatch):
    monkeypatch.setattr(
        device_flow.httpx, "post", lambda *a, **k: _resp({"error": "access_denied"})
    )
    result = device_flow.poll("abc123", "dev-xyz")
    assert result["status"] == "access_denied"


def test_poll_success(monkeypatch):
    monkeypatch.setattr(
        device_flow.httpx,
        "post",
        lambda *a, **k: _resp({"access_token": "gho_realtoken", "token_type": "bearer", "scope": "repo"}),
    )
    result = device_flow.poll("abc123", "dev-xyz")
    assert result["status"] == "success"
    assert result["access_token"] == "gho_realtoken"


def test_poll_missing_device_code():
    with pytest.raises(device_flow.DeviceFlowError):
        device_flow.poll("abc123", "")
