"""`tools/` 里会消耗额度的验收工具，参数契约必须严格。

背景（2026-11-11 真踩过）：写 portability 测试时顺手对每支工具跑了 `--help`，
结果 `accept_video.py` 不认识 `--help`，把它**当成提示词**提交成视频生成任务
（虽因账号不可派单而失败、未消耗上游额度，但留下两条 `prompt='--help'` 的任务
记录）；`accept_relay_media.py` 则把未知参数**静默忽略**，直接跑起了整轮验收。

所以这里用进程内调用钉住两条：`-h/--help` 只打用法并返回 0；未知参数返回 2，
**且在返回前不发生任何网络请求**（两个实现都把 help/未知参数处理放在发请求之前）。

用 importlib 按路径加载，避免把 `tools/` 变成 package。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"tools_{name}", TOOLS / name)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def accept_video():
    return _load("accept_video.py")


@pytest.fixture(scope="module")
def accept_relay():
    return _load("accept_relay_media.py")


def test_accept_video_help_prints_usage_and_exits(accept_video, capsys) -> None:
    assert accept_video.main(["--help"]) == 0
    assert "用法" in capsys.readouterr().out


def test_accept_video_rejects_unknown_flag_instead_of_using_it_as_prompt(
    accept_video, capsys
) -> None:
    """这就是当初的 bug：`--help` 被当成提示词。"""
    assert accept_video.main(["--nope"]) == 2
    captured = capsys.readouterr()
    assert "不认识的参数" in captured.err


def test_accept_relay_help_prints_usage_and_exits(accept_relay, capsys) -> None:
    assert accept_relay.main(["--help"]) == 0
    assert "用法" in capsys.readouterr().out


def test_accept_relay_rejects_unknown_flag(accept_relay, capsys) -> None:
    assert accept_relay.main(["--nope"]) == 2
    assert "不认识的参数" in capsys.readouterr().err
