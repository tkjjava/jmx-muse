"""多人部署安全阀：非回环绑定 + 空访问密钥，启动必须出声。"""

from __future__ import annotations

from muse_bridge.__main__ import _exposure_warning


def test_loopback_stays_quiet(monkeypatch):
    for env in ('MUSE_BRIDGE_API_KEYS', 'MUSE_BRIDGE_KEY'):
        monkeypatch.delenv(env, raising=False)
    assert _exposure_warning('127.0.0.1', 'MUSE_BRIDGE_API_KEYS') is None
    assert _exposure_warning('localhost', 'MUSE_BRIDGE_API_KEYS') is None


def test_exposed_bridge_with_keys_stays_quiet(monkeypatch):
    monkeypatch.setenv('MUSE_BRIDGE_API_KEYS', 'k1,k2')
    assert _exposure_warning('0.0.0.0', 'MUSE_BRIDGE_API_KEYS') is None


def test_exposed_bridge_without_keys_warns(monkeypatch):
    """require_key 在密钥表为空时直接放行——裸奔必须警告。"""
    monkeypatch.delenv('MUSE_BRIDGE_API_KEYS', raising=False)
    msg = _exposure_warning('0.0.0.0', 'MUSE_BRIDGE_API_KEYS')
    assert msg is not None and '0.0.0.0' in msg and 'MUSE_BRIDGE_API_KEYS' in msg
    assert 'Muse-Relay' in msg, '多人使用的正确姿势必须写进警告'


def test_whitespace_keys_do_not_count_as_configured(monkeypatch):
    """环境变量设成空格不等于启用了鉴权：仍然要警告。"""
    monkeypatch.setenv('MUSE_BRIDGE_API_KEYS', '   ')
    assert _exposure_warning('0.0.0.0', 'MUSE_BRIDGE_API_KEYS') is not None
