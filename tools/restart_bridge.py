"""重启 bridge：杀掉占端口的旧进程，用当前环境起新进程并等 `/healthz` 就绪。

## 为什么要有这个工具

重启看起来是 `python -m muse_bridge` 一条命令，实际每次都要重配十来个 env
（协议开关、代理、暖池、超时），PowerShell 内联引号一改就错。固化成脚本后，
"重启"这个动作可复现，验收才有意义——否则每次重启换一套配置，
上一次的绿结果就不代表这一次。

## 凭据怎么处理

**不写进源码**。原 `work/probe_restart_bridge.py` 把 Fernet key 硬编码在文件里，
那是要在仓库里留密钥。现在改为：

  * `MUSE_BRIDGE_KEY` 从**当前环境**继承；缺失时直接报错退出，
    不生成、不回退默认值（回退等于又把密钥写进仓库）。
  * 其余配置有合理默认值，全部可用 env 覆盖。

想固定一套运行态，用 `.env`（`.env.example` 有完整清单）：
本工具**不读** `.env` 文件，与产品保持一致——由调用方先把变量放进环境。

## 用法

    export MUSE_BRIDGE_KEY=<你的 key>            # 必须
    python tools/restart_bridge.py               # 前台起，日志打到 stdout
    python tools/restart_bridge.py --wait-only   # 只等就绪，不重启
    python tools/restart_bridge.py --log data/bridge.log   # 追加写文件

进程退出后不会自动重启；需要常驻请用进程管理器托管。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 默认运行态。每一条后面写"为什么是它"，换机器/换网络时知道该改哪个。
DEFAULTS = {
    # 生成能力：不开就只能聊天
    "MUSE_GENERATION_ENABLED": "1",
    # 生成预算：图片通常 20~40 s，视频会到分钟级
    "MUSE_IMAGE_TIMEOUT": "240",
    "MUSE_VIDEO_TIMEOUT": "600",
    # 抢号排队预算：muse 只能串行，多请求会排队
    "MUSE_QUEUE_WAIT_SECONDS": "300",
    # 任务保留：48 h 内能回看成品
    "MUSE_TASK_TTL": "7200",
    "MUSE_REFERENCE_MAX": "10",
    # 协议优先：聊天与生成都走 Noise 协议，失败回退一次浏览器
    "MUSE_PROTOCOL_DRIVER": "1",
    # 订阅流 idle 预算：连续 60 s 没有新字节就结束本回合，避免空转到总 deadline
    "MUSE_STREAM_IDLE_TIMEOUT": "60",
    # 暖池：跨派单复用同一条 Noise 会话，省掉每回合约 3 s 的 WS+握手
    "MUSE_WARM_POOL": "1",
    "MUSE_WARM_POOL_TTL": "300",
    "PYTHONUTF8": "1",
}


def _env(name: str, fallback: str = "") -> str:
    return os.environ.get(name, fallback).strip()


def port() -> int:
    return int(_env("MUSE_BRIDGE_PORT", "8788"))


def base() -> str:
    return f"http://{_env('MUSE_BRIDGE_HOST', '127.0.0.1')}:{port()}"


def build_env(proxy: str) -> dict[str, str]:
    """把默认值与当前环境合并。环境优先——工具不该覆盖调用方的显式选择。"""
    env = dict(os.environ)
    for key, value in DEFAULTS.items():
        env.setdefault(key, value)
    # 协议链路的两条出口。分开配是因为它们的可达性可能不同：会话面被墙而 WS 面
    # 能通的情况真实存在（见 docs/PROTOCOL.md）。这里默认共用同一个代理，
    # 想分开就用 MUSE_PROTOCOL_PROXY / MUSE_SESSION_PROXY 各自指定。
    if proxy:
        env.setdefault("MUSE_PROTOCOL_PROXY", proxy)
        env.setdefault("MUSE_SESSION_PROXY", proxy)
    return env


def require_key() -> None:
    if not _env("MUSE_BRIDGE_KEY"):
        raise SystemExit(
            "缺少 MUSE_BRIDGE_KEY（账号 cookie 的 Fernet 密钥）。\n"
            "先生成一把并放进环境，例如：\n"
            '  python -c "from cryptography.fernet import Fernet;'
            ' print(Fernet.generate_key().decode())"\n'
            "注意：**换 key 会让已存的 cookie 无法解密**，已有账号要重新导入 cookie。\n"
            "本工具不生成默认密钥：那会把密钥写进仓库。"
        )


def listeners_on(port_number: int) -> list[int]:
    """找占用端口的 pid。优先 psutil，没有就退回到系统工具。"""
    pids: list[int] = []
    try:
        import psutil  # type: ignore
    except ImportError:
        psutil = None  # type: ignore
    if psutil is not None:
        for conn in psutil.net_connections(kind="inet"):
            if (
                conn.laddr
                and conn.laddr.port == port_number
                and conn.status == "LISTEN"
                and conn.pid
            ):
                pids.append(conn.pid)
        return sorted(set(pids))

    if sys.platform == "win32":
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-NetTCPConnection -State Listen -LocalPort {port_number}"
             " -ErrorAction SilentlyContinue).OwningProcess"],
            capture_output=True, text=True,
        ).stdout
        pids = [int(x) for x in out.split() if x.strip().isdigit()]
    else:
        out = subprocess.run(["lsof", "-ti", f"tcp:{port_number}", "-sTCP:LISTEN"],
                             capture_output=True, text=True).stdout
        pids = [int(x) for x in out.split() if x.strip().isdigit()]
    return sorted(set(pids))


def kill_listeners(port_number: int) -> None:
    for pid in listeners_on(port_number):
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"已发送终止信号给 pid={pid}")
        except OSError as exc:
            print(f"无法终止 pid={pid}：{exc}")
    # 给进程一点时间释放端口
    for _ in range(20):
        if not listeners_on(port_number):
            return
        time.sleep(0.25)
    for pid in listeners_on(port_number):
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            print(f"强制终止 pid={pid}")
        except OSError:
            pass


def wait_ready(timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base()}/healthz", timeout=3) as resp:
                return json.loads(resp.read())
        except Exception:
            time.sleep(1)
    raise SystemExit(f"{base()}/healthz 在 {timeout:.0f}s 内没有就绪")


def summarise(info: dict) -> str:
    warm = info.get("warm_pool") or {}
    accounts = warm.get("accounts") if isinstance(warm, dict) else None
    return (
        f"version={info.get('version')} "
        f"driver={info.get('chat_driver')} "
        f"可派单={info.get('accounts_dispatchable')}/{info.get('accounts_total')} "
        f"协议代理={'已配置' if info.get('protocol_proxy_configured') else '未配置'} "
        f"暖池={accounts if accounts is not None else '-'} "
        f"权限面分离={info.get('admin_key_separated')}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重启 muse-bridge 并等待就绪")
    parser.add_argument("--wait-only", action="store_true", help="只等就绪，不重启")
    parser.add_argument("--log", default="", help="把子进程输出追加写到这里")
    parser.add_argument("--proxy", default=_env("MUSE_PROTOCOL_PROXY"),
                        help="协议与会话面的代理地址（默认读 MUSE_PROTOCOL_PROXY）")
    parser.add_argument("--timeout", type=float, default=45.0, help="等就绪的秒数")
    args = parser.parse_args(argv)

    if args.wait_only:
        info = wait_ready(args.timeout)
        print(f"就绪：{summarise(info)}")
        return 0

    require_key()
    kill_listeners(port())

    env = build_env(args.proxy)
    cmd = [sys.executable, "-Xutf8", "-m", "muse_bridge"]
    if args.log:
        log_path = Path(args.log)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # 用 with 管理句柄：`Popen` 会把句柄继承给子进程，父进程这侧随即可以关，
        # 所以生命周期是闭合的，不需要手工 close + try/finally。
        with open(log_path, "ab", buffering=0) as sink:
            sink.write(
                f"\n=== restart at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode()
            )
            proc = subprocess.Popen(
                cmd, cwd=str(REPO_ROOT), env=env, stdout=sink, stderr=sink,
            )
    else:
        proc = subprocess.Popen(
            cmd, cwd=str(REPO_ROOT), env=env, stdout=sys.stdout, stderr=sys.stderr,
        )

    print(f"已启动 pid={proc.pid} 端口={port()} 日志={args.log or 'stdout'}")
    info = wait_ready(args.timeout)
    print(f"就绪：{summarise(info)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
