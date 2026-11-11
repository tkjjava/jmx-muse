"""冒烟脚本必须认识 API 密钥。

配了密钥的部署里，冒烟不带密钥会拿到 401——那是配置问题，不该报成
"服务没起来"或者丢一个裸 HTTPError traceback。
"""

from __future__ import annotations

import urllib.error

import pytest

from muse_bridge import smoke


def test_auth_headers_empty_without_keys(monkeypatch):
    monkeypatch.delenv(smoke.API_KEYS_ENV, raising=False)
    assert smoke._auth_headers() == {}


def test_auth_headers_uses_first_key(monkeypatch):
    monkeypatch.setenv(smoke.API_KEYS_ENV, " sk1 , sk2 ")
    assert smoke._auth_headers() == {"Authorization": "Bearer sk1"}


def test_auth_headers_prefers_admin_key_for_admin_paths(monkeypatch):
    """0.4.0 起管理面可分钥匙：冒烟拿调用 key 打 /admin 会 401。"""
    monkeypatch.setenv(smoke.API_KEYS_ENV, "client-key")
    monkeypatch.setenv(smoke.ADMIN_KEYS_ENV, "admin-key")
    assert smoke._auth_headers("/admin/accounts") == {
        "Authorization": "Bearer admin-key"
    }
    assert smoke._auth_headers("/v1/models") == {
        "Authorization": "Bearer client-key"
    }


def test_auth_headers_admin_falls_back_to_client_keys(monkeypatch):
    """没设管理密钥的既有部署（未分离）仍然能跑冒烟。"""
    monkeypatch.setenv(smoke.API_KEYS_ENV, "client-key")
    monkeypatch.delenv(smoke.ADMIN_KEYS_ENV, raising=False)
    assert smoke._auth_headers("/admin/accounts") == {
        "Authorization": "Bearer client-key"
    }


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_get_sends_authorization_when_key_set(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["auth"] = request.get_header("Authorization")
        return _FakeResponse(b'{"data": []}')

    monkeypatch.setenv(smoke.API_KEYS_ENV, "sk1")
    monkeypatch.setattr(smoke.urllib.request, "urlopen", fake_urlopen)
    assert smoke._get("/v1/models") == {"data": []}
    assert seen["auth"] == "Bearer sk1"


def test_get_without_key_sends_no_authorization(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["auth"] = request.get_header("Authorization")
        return _FakeResponse(b'{"data": []}')

    monkeypatch.delenv(smoke.API_KEYS_ENV, raising=False)
    monkeypatch.setattr(smoke.urllib.request, "urlopen", fake_urlopen)
    smoke._get("/v1/models")
    assert seen["auth"] is None


def test_401_is_reported_as_auth_problem(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.delenv(smoke.API_KEYS_ENV, raising=False)
    monkeypatch.setattr(smoke.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(smoke.AuthRequired) as excinfo:
        smoke._get("/v1/models")
    assert smoke.API_KEYS_ENV in str(excinfo.value)
