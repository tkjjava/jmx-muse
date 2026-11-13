"""测试会话级隔离：把「运维环境的协议变量」从测试环境里摘掉。

为什么需要这一层
----------------
2026-11-13 重启机器后全量套件突然 21 个红：`test_protocol_driver` / `test_protocol_staging`
/ `test_concurrency` 里那些「全离线、不联网」的用例真的发起了连接，拿回 401。

根因不在代码，在环境：运维按部署说明把 `MUSE_PROTOCOL_DRIVER=1`、`MUSE_WARM_POOL=1`、
`MUSE_PROTOCOL_PROXY` 等写进了**用户级环境变量**；机器重启后，测试进程继承了它们，
`ProtocolDriver.connect()` 于是走进暖池分支，把测试里注入的假 client 覆盖成真 client
——假件被真实网络顶掉，测试既变慢又依赖账号存活。

结论：单元测试的输入必须来自测试自己，不能来自操作者的机器配置。
这里在**每个用例前**删掉这批变量（`monkeypatch` 保证用例结束自动还原），
需要它们的用例仍旧自己 `monkeypatch.setenv`，不受影响。
"""

from __future__ import annotations

import pytest

# 只列「一旦存在于环境就会改变代码路径」的变量。纯展示用的不在此列。
_PROTOCOL_RUNTIME_ENV = (
    "MUSE_PROTOCOL_DRIVER",     # 协议/浏览器开关
    "MUSE_PROTOCOL_PROXY",      # 协议侧出口
    "MUSE_SESSION_PROXY",       # HTTP 续签出口
    "MUSE_WARM_POOL",           # 暖池复用（会覆盖注入的假 client）
    "MUSE_STREAM_IDLE_TIMEOUT",  # 流空闲超时
    "MUSE_GENERATION_ENABLED",  # 生成能力开关
    "MUSE_REPLY_TIMEOUT",       # 回合超时
)


@pytest.fixture(autouse=True)
def _no_operator_protocol_env(monkeypatch):
    """测试默认运行在「操作者没配任何协议变量」的基线上。"""
    for name in _PROTOCOL_RUNTIME_ENV:
        monkeypatch.delenv(name, raising=False)
