"""环境变量解析：坏值要点名报错，不能留 traceback。"""

from __future__ import annotations

import pytest

from muse_bridge import app, env


def test_bad_int_names_the_variable(monkeypatch):
    monkeypatch.setenv("MUSE_BAD_INT", "八八")
    with pytest.raises(env.EnvError) as excinfo:
        env.env_int("MUSE_BAD_INT", "600")
    message = str(excinfo.value)
    assert "MUSE_BAD_INT" in message and "八八" in message


def test_bad_float_names_the_variable(monkeypatch):
    monkeypatch.setenv("MUSE_BAD_FLOAT", "180s")
    with pytest.raises(env.EnvError) as excinfo:
        env.env_float("MUSE_BAD_FLOAT", "180")
    message = str(excinfo.value)
    assert "MUSE_BAD_FLOAT" in message and "180s" in message


def test_empty_means_unset(monkeypatch):
    monkeypatch.setenv("MUSE_EMPTY", "")
    assert env.env_int("MUSE_EMPTY", "8788") == 8788
    monkeypatch.setenv("MUSE_BLANK", "   ")
    assert env.env_float("MUSE_BLANK", "1.5") == 1.5


def test_env_error_is_a_system_exit(monkeypatch):
    """没 traceback 才算成功：SystemExit 只会打印那一句话。"""
    monkeypatch.setenv("MUSE_BAD", "nope")
    with pytest.raises(SystemExit):
        env.env_int("MUSE_BAD", "1")
    error = env.EnvError("x")
    assert isinstance(error, SystemExit)


def test_port_range_rejected(monkeypatch):
    for bad in ("0", "65536", "-1"):
        monkeypatch.setenv("MUSE_PORT", bad)
        with pytest.raises(env.EnvError):
            env.env_port("MUSE_PORT", "8788")
    monkeypatch.setenv("MUSE_PORT", "8788")
    assert env.env_port("MUSE_PORT", "1") == 8788


def test_positive_required(monkeypatch):
    monkeypatch.setenv("MUSE_T", "0")
    with pytest.raises(env.EnvError):
        env.env_positive_float("MUSE_T", "180")
    with pytest.raises(env.EnvError):
        env.env_positive_int("MUSE_T", "600")


def test_shipped_constants_are_parsed_through_env():
    """模块级常量走的是同一套解析：默认值仍与文档一致。"""
    assert app.REPLY_TIMEOUT == 180.0
