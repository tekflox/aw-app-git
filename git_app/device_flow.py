"""GitHub OAuth Device Flow (RFC 8628) — API-driven login, no client_secret,
no interactive TUI. Two calls:

  1. ``start(client_id)``           -> POST /login/device/code
  2. ``poll(client_id, device_code)`` -> POST /login/oauth/access_token

``client_id`` is public (device flow never uses a client_secret) — resolved
by the caller (``plugin.py``) from config/env, not read from here.
"""

from __future__ import annotations

import httpx

DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
DEFAULT_SCOPE = "repo read:org gist workflow"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

_HEADERS = {"Accept": "application/json"}
_TIMEOUT = 10.0


class DeviceFlowError(RuntimeError):
    pass


def start(client_id: str, scope: str = DEFAULT_SCOPE) -> dict:
    """Returns ``{device_code, user_code, verification_uri, expires_in, interval}``."""
    if not client_id:
        raise DeviceFlowError("no OAuth client_id configured")
    try:
        resp = httpx.post(
            DEVICE_CODE_URL,
            data={"client_id": client_id, "scope": scope},
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as e:
        raise DeviceFlowError(f"could not reach GitHub: {e}") from e
    if resp.status_code >= 400:
        raise DeviceFlowError(f"GitHub returned HTTP {resp.status_code}: {resp.text}")
    result = resp.json()
    if "error" in result:
        raise DeviceFlowError(result.get("error_description") or result["error"])
    return result


def poll(client_id: str, device_code: str) -> dict:
    """One poll attempt. Returns ``{"status": ..., ...}`` where status is one of
    ``success`` (with ``access_token``), ``authorization_pending``, ``slow_down``
    (with a possibly-updated ``interval``), ``expired_token``, ``access_denied``.
    """
    if not client_id:
        raise DeviceFlowError("no OAuth client_id configured")
    if not device_code:
        raise DeviceFlowError("no device_code to poll")
    try:
        resp = httpx.post(
            ACCESS_TOKEN_URL,
            data={
                "client_id": client_id,
                "device_code": device_code,
                "grant_type": GRANT_TYPE,
            },
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as e:
        raise DeviceFlowError(f"could not reach GitHub: {e}") from e
    if resp.status_code >= 400 and not resp.headers.get("content-type", "").startswith("application/json"):
        raise DeviceFlowError(f"GitHub returned HTTP {resp.status_code}: {resp.text}")
    result = resp.json()
    error = result.get("error")
    if error:
        return {"status": error, **result}
    if "access_token" in result:
        return {"status": "success", **result}
    raise DeviceFlowError(f"unexpected response from GitHub: {result}")
