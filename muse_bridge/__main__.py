"""启动入口。"""

from __future__ import annotations

import os
import sys

import uvicorn

from .env import env_port

# 回环之外的绑定地址，配合空密钥，等于把共享账号池交给整个网络。
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def _exposure_warning(host: str, keys_env: str) -> str | None:
    """非回环绑定 + 空访问密钥时的启动警告。

    多人使用的正确姿势是走 Muse-Relay：每人一把 client key，配额、审计、
    预算都在前门管，bridge 只监听本机。把 bridge 直接绑到 0.0.0.0 还不设
    访问密钥时，任何能连到这个端口的人都能驱动账号池——
    require_key 在密钥表为空时直接放行。警告只说事实，不改默认行为。
    """
    if host in _LOOPBACK:
        return None
    if os.environ.get(keys_env, "").strip():
        return None
    return (
        f"警告：绑定 {host}（非回环）且 {keys_env} 为空——"
        "网络上任何能访问本端口的人都能消耗共享账号池。"
        f"多人使用请设 {keys_env}，或把流量收敛到 Muse-Relay 统一鉴权与配额。"
    )


def main() -> None:
    host = os.environ.get("MUSE_BRIDGE_HOST", "127.0.0.1")
    port = env_port("MUSE_BRIDGE_PORT", "8788")
    warning = _exposure_warning(host, "MUSE_BRIDGE_API_KEYS")
    if warning:
        print(warning, file=sys.stderr)
    uvicorn.run("muse_bridge.app:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()