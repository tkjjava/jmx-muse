"""muse_bridge 的环境变量解析。

坏值必须是给人看的一句话，而不是 traceback。在此之前：

    MUSE_REPLY_TIMEOUT=180s
    -> ValueError: could not convert string to float: '180s'
       （报错位置是 app.py:22，运营者得自己推断是哪个变量写错了）

    MUSE_BRIDGE_PORT=""
    -> ValueError: invalid literal for int() with base 10: ''

现在统一成点名报错 + 干净退出。
"""

from __future__ import annotations

import os


class EnvError(SystemExit):
    """配置错误：打印一句话并退出，不留 traceback。

    继承 SystemExit 是刻意的——模块级常量在导入期就要求值，那时没有 app
    可以返回 503，唯一正确的行为就是带着明确信息退出。
    """


def _raw(name: str, default: str) -> str:
    """取原始值。空串（含全空白）按"没配"处理——与 .env 里 `VAR=` 的直觉一致。"""
    value = os.environ.get(name, "").strip()
    return value or default


def env_int(name: str, default: str) -> int:
    raw = _raw(name, default)
    try:
        return int(raw)
    except ValueError:
        raise EnvError(f"{name} 必须是整数，当前值：{raw!r}") from None


def env_float(name: str, default: str) -> float:
    raw = _raw(name, default)
    try:
        return float(raw)
    except ValueError:
        raise EnvError(f"{name} 必须是数字，当前值：{raw!r}") from None


def env_positive_int(name: str, default: str) -> int:
    """超时、冷却、间隔这类值必须为正：0 或负数是误配，不是"关掉功能"。"""
    value = env_int(name, default)
    if value <= 0:
        raise EnvError(f"{name} 必须大于 0，当前值：{value}")
    return value


def env_positive_float(name: str, default: str) -> float:
    value = env_float(name, default)
    if value <= 0:
        raise EnvError(f"{name} 必须大于 0，当前值：{value}")
    return value


def env_port(name: str, default: str) -> int:
    """端口必须在 1..65535：0 不是"随便挑一个"，那会让服务静默绑到随机端口。"""
    value = env_int(name, default)
    if not 1 <= value <= 65535:
        raise EnvError(f"{name} 必须在 1..65535 之间，当前值：{value}")
    return value

# ---- 生成管线（0.4.0 新增）----------------------------------------------
#
# 排队预算与生成预算刻意拆成两个变量：上游 app.py:1100-1101 把两者共用同一个
# timeout（最坏 2×timeout 才失败，且被 test_async_images.py:116 钉成契约）。
# 这里 QUEUE_WAIT_SECONDS 只管"抢账号锁等多久"，IMAGE/VIDEO_TIMEOUT 只管
# "拿到锁之后生成等多久"，互不挤占。
IMAGE_TIMEOUT = env_positive_int("MUSE_IMAGE_TIMEOUT", "240")
VIDEO_TIMEOUT = env_positive_int("MUSE_VIDEO_TIMEOUT", "600")
QUEUE_WAIT_SECONDS = env_positive_int("MUSE_QUEUE_WAIT_SECONDS", "300")
GENERATION_CONCURRENCY = env_positive_int("MUSE_GENERATION_CONCURRENCY", "2")
TASK_TTL_SECONDS = env_positive_int("MUSE_TASK_TTL", "7200")
REFERENCE_MAX = env_positive_int("MUSE_REFERENCE_MAX", "10")


def _flag(name: str, default: str) -> bool:
    return _raw(name, default).strip().lower() not in ("0", "false", "no", "off")


# 生成能力总开关：关掉时 /v1/images*、/v1/videos* 直接 503，聊天路径不受影响。
GENERATION_ENABLED = _flag("MUSE_GENERATION_ENABLED", "1")

# ---- 协议驱动（本轮新增）--------------------------------------------------
#
# 派单驱动二选一：默认浏览器（既有部署行为一个字节都不变），显式打开后聊天才
# 走纯协议；生成**永远**走浏览器（协议生成还没有真机验收）。
#
# 这里刻意用"显式真值集合"而不是 _flag：拼错一个值（例如
# MUSE_PROTOCOL_DRIVER=enable）必须落到"关"，不能把生产流量悄悄切到一条
# 尚未验收的链路上。真值集合与 pool._protocol_driver_enabled() 保持一致。
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def env_flag(name: str, default: str = "0") -> bool:
    """只认 1/true/yes/on（大小写不敏感、忽略两端空白）的布尔开关。

    与 `_flag` 的区别是**方向**：`_flag` 把不认识的词当开（适合能力总开关），
    本函数把不认识的词当关——默认关闭的开关必须这样。
    """
    return _raw(name, default).strip().lower() in _TRUE_VALUES


PROTOCOL_DRIVER = env_flag("MUSE_PROTOCOL_DRIVER", "0")
# 订阅流 idle 预算（秒）：连续这么久没有任何新字节就结束本回合。默认 0 = 关闭，
# 行为与旧版完全一致；打开后一条僵死订阅不会空转到总 deadline。`MUSE_REPLY_TIMEOUT`
# 管的是"整回合多久算超时"，这里管的是"多久没动静算死"——两把尺子不能合并。
# 用 env_float 而不是 env_positive_float：0 在这里是合法值（=关闭），不是误配。
PROTOCOL_STREAM_IDLE_TIMEOUT = env_float("MUSE_STREAM_IDLE_TIMEOUT", "0")
# 代理地址：真机 2026-11-11 实测 WS 必须经本机代理到 hatch.metaaivm.com
# （直连 TLS 不通）。空值按"没配"处理成 None，客户端于是走直连——可达性由
# 运营者自己确认，这里不替它猜一个默认代理。
PROTOCOL_PROXY = os.environ.get("MUSE_PROTOCOL_PROXY", "").strip() or None
# 会话面（`GET /api/session`）的出口。单独一个变量是因为两条链路的可达性可以不同：
# 真机 2026-11-11 13:4x 的实测是"直连 /api/session 被墙、经 7890 可达"，而协议 WS
# 早就走 MUSE_PROTOCOL_PROXY 了。没单独配时回落到协议代理——同一个出口是最常见
# 的部署形态，这里只是让它可以被覆盖，不是另立一套默认值。
SESSION_PROXY = (
    os.environ.get("MUSE_SESSION_PROXY", "").strip() or PROTOCOL_PROXY
)
