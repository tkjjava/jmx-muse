"""启停恢复校验：证明"停得掉、起得来、数据不丢"。

## 为什么需要它

发布前最容易被口头糊弄过去的恰恰是这一条：`/healthz` 200 不等于重启过，重启过
也不等于账号库和成品目录完好。2026-11-11 的 C3 轮是靠一串手工命令做的，结论
只能留在文档里；换台机器或换个人就得重来一遍，而且很容易只看 healthz 就宣布
"恢复通过"。

这个脚本把那次实验固化成一条可重复的命令：

1. **快照**：监听端口 / pid / `/healthz` / 账号总数 / 成品文件数与字节 / 账号库大小；
2. **重启**：委托 `tools/restart_bridge.py`（杀掉占端口的旧进程、用当前环境起新的、等就绪）；
3. **对比**：把第 2 步之后的快照与第 1 步逐项比，任何一项"退化"都算失败：
   - 端口没起来、或 `/healthz` 不是 200；
   - 账号总数变少（说明库被重建或读不出来）；
   - 成品文件数或总字节变少（说明重启路径顺手删了东西）；
   - 账号库文件消失或明显变小。

## 用法

    python tools/lifecycle_check.py            # 快照 -> 重启 -> 对比
    python tools/lifecycle_check.py snapshot   # 只打快照，不重启
    python tools/lifecycle_check.py --chat     # 额外验证"重启后真的能聊天"

`--chat` 会先刷账号探针再发一句最短中文，**不消耗上游生成额度**（只是聊天）。
没配 `MUSE_TOOLS_API_KEY` 时匿名跑（本地开发态常见，产品默认也不校验）。

## 边界

* 需要 `MUSE_BRIDGE_KEY` 在环境里（重启工具从当前环境继承它，缺失会直接报错）；
* 重启会让服务短暂不可用（默认几秒），别在生产时段跑；
* 它**不**清理任何数据，也不改 `.env`——只读快照 + 重启进程。
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]


def _env(name: str, default: str = "") -> str:
    import os

    return os.environ.get(name, default).strip()


def port() -> int:
    return int(_env("MUSE_TOOLS_PORT", _env("MUSE_BRIDGE_PORT", "8788")))


def base() -> str:
    explicit = _env("MUSE_TOOLS_BASE")
    return explicit.rstrip("/") if explicit else f"http://127.0.0.1:{port()}"


def auth_headers() -> dict[str, str]:
    key = _env("MUSE_TOOLS_API_KEY")
    return {"Authorization": f"Bearer {key}"} if key else {}


def media_dir() -> Path:
    explicit = _env("MUSE_TOOLS_MEDIA_DIR")
    return Path(explicit) if explicit else REPO / "data" / "media"


def db_path() -> Path:
    explicit = _env("MUSE_TOOLS_DB")
    return Path(explicit) if explicit else REPO / "data" / "muse.db"


def log_path() -> Path:
    explicit = _env("MUSE_TOOLS_LOG")
    return Path(explicit) if explicit else REPO / "data" / "bridge.log"


def port_is_open(p: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", p)) == 0


def listening_pids(p: int) -> list[int]:
    """Windows: 用 Get-NetTCPConnection 拿监听者；其它平台返回空列表（不致命）。"""
    if sys.platform != "win32":
        return []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-NetTCPConnection -State Listen -LocalPort {p} "
             "-ErrorAction SilentlyContinue).OwningProcess"],
            capture_output=True, text=True, timeout=30,
        )
        return [int(x) for x in out.stdout.split() if x.strip().isdigit()]
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


def healthz(timeout: float = 10.0) -> dict:
    """返回 {status_code, body}；连不上时 status_code=None。"""
    try:
        resp = httpx.get(base() + "/healthz", trust_env=False, timeout=timeout)
        return {"status_code": resp.status_code, "body": resp.json()}
    except (httpx.HTTPError, ValueError) as exc:
        return {"status_code": None, "body": None, "error": f"{type(exc).__name__}: {exc}"}


def snapshot(label: str) -> dict:
    state: dict = {"label": label, "port": port(), "port_open": port_is_open(port())}
    state["listening_pids"] = listening_pids(port())
    state.update(healthz())

    directory = media_dir()
    files = [p for p in directory.glob("*") if p.is_file()] if directory.exists() else []
    state["media_files"] = len(files)
    state["media_bytes"] = sum(p.stat().st_size for p in files)
    state["media_dir"] = str(directory)

    db = db_path()
    state["db_exists"] = db.exists()
    state["db_bytes"] = db.stat().st_size if db.exists() else 0
    return state


def print_snapshot(state: dict) -> None:
    body = state.get("body") or {}
    print(f"[{state['label']}]")
    print(f"  port {state['port']}: "
          + ("listening pid=" + ",".join(map(str, state["listening_pids"]))
             if state["port_open"] else "closed"))
    print(f"  /healthz: {state['status_code']}"
          + (f" error={state['error']}" if state.get("error") else ""))
    if body:
        print("  accounts_total={} dispatchable={} chat_driver={}".format(
            body.get("accounts_total"), body.get("accounts_dispatchable"),
            body.get("chat_driver")))
        warm = body.get("warm_pool") or {}
        print(f"  warm_pool.enabled={warm.get('enabled')}")
    print(f"  media: {state['media_files']} files / {state['media_bytes']} B  ({state['media_dir']})")
    print(f"  db: exists={state['db_exists']} size={state['db_bytes']} B")


def compare(before: dict, after: dict) -> list[str]:
    """返回失败项列表；空列表 = 恢复通过。"""
    problems: list[str] = []
    if not after["port_open"]:
        problems.append(f"端口 {after['port']} 没有在监听")
    if after["status_code"] != 200:
        problems.append(f"/healthz 返回 {after['status_code']}（期望 200）")

    def accounts(s: dict):
        return (s.get("body") or {}).get("accounts_total")

    b_total, a_total = accounts(before), accounts(after)
    if isinstance(b_total, int) and isinstance(a_total, int) and a_total < b_total:
        problems.append(f"账号总数变少：{b_total} -> {a_total}")
    if after["media_files"] < before["media_files"]:
        problems.append(f"成品文件变少：{before['media_files']} -> {after['media_files']}")
    if after["media_bytes"] < before["media_bytes"]:
        problems.append(f"成品总字节变少：{before['media_bytes']} -> {after['media_bytes']}")
    if before["db_exists"] and not after["db_exists"]:
        problems.append("账号库文件消失了")
    if before["db_bytes"] and after["db_bytes"] and after["db_bytes"] < before["db_bytes"] // 2:
        problems.append(f"账号库明显变小：{before['db_bytes']} -> {after['db_bytes']} B")
    return problems


def restart() -> int:
    if not _env("MUSE_BRIDGE_KEY"):
        print("缺 MUSE_BRIDGE_KEY：重启工具从当前环境继承它，没有就起不来。", file=sys.stderr)
        return 2
    cmd = [sys.executable, "-u", "-Xutf8", str(REPO / "tools" / "restart_bridge.py"),
           "--log", str(log_path())]
    print("$ " + " ".join(cmd))
    completed = subprocess.run(cmd, cwd=REPO)
    return completed.returncode


def refresh_account(account: str = "muse1") -> bool:
    try:
        resp = httpx.post(f"{base()}/admin/accounts/{account}/refresh",
                          headers=auth_headers(), trust_env=False, timeout=180)
        print(f"  refresh {account}: {resp.status_code} {resp.text[:100]}")
        return resp.status_code == 200
    except httpx.HTTPError as exc:
        print(f"  refresh 失败：{type(exc).__name__}: {exc}")
        return False


def chat_once() -> bool:
    """最短的聊天往返：只证明"重启后这条链路真的能用"。"""
    try:
        resp = httpx.post(
            base() + "/v1/chat/completions",
            headers={**auth_headers(), "Content-Type": "application/json"},
            json={"model": "muse-1",
                  "messages": [{"role": "user", "content": "只回复两个字母 OK"}],
                  "stream": False},
            trust_env=False, timeout=300,
        )
        if resp.status_code != 200:
            print(f"  chat: {resp.status_code} {resp.text[:160]}")
            return False
        content = resp.json()["choices"][0]["message"].get("content", "")
        print(f"  chat: 200 content={content[:60]!r}")
        return bool(content.strip())
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
        print(f"  chat 失败：{type(exc).__name__}: {exc}")
        return False


def run_full(do_chat: bool) -> int:
    before = snapshot("重启前")
    print_snapshot(before)

    code = restart()
    if code != 0:
        print(f"\n重启工具退出码 {code}：没有继续做对比（先解决它。)", file=sys.stderr)
        return code

    # 就绪行由重启工具打印；这里再等 healthz 稳一下，避免刚起来就抓快照。
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and healthz()["status_code"] != 200:
        time.sleep(0.5)

    after = snapshot("重启后")
    print_snapshot(after)

    problems = compare(before, after)
    ok = not problems

    if do_chat:
        print("\n[可聊性：刷探针 -> 发一句 -> 读正文]")
        ok = refresh_account() and chat_once() and ok

    print("\n=== 结论 ===")
    if problems:
        print("恢复**未通过**：")
        for item in problems:
            print("  ✗ " + item)
    else:
        print("恢复通过：端口、/healthz、账号总数、成品与账号库均未退化。")
    print("提示：重启不会清理成品，也不会重建账号库；如果账号变少，先查是不是换了 MUSE_BRIDGE_DB。")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启停恢复校验：停得掉、起得来、数据不丢")
    parser.add_argument("action", nargs="?", default="verify",
                        choices=["verify", "snapshot"],
                        help="verify=快照+重启+对比；snapshot=只打快照")
    parser.add_argument("--chat", action="store_true", help="额外验证重启后真的能聊天")
    parser.add_argument("--json", action="store_true", help="把快照以 JSON 打印（便于存档）")
    args = parser.parse_args(argv)

    if args.action == "snapshot":
        state = snapshot("当前")
        if args.json:
            print(json.dumps(state, ensure_ascii=False, indent=2))
        else:
            print_snapshot(state)
        return 0

    return run_full(args.chat)


if __name__ == "__main__":
    raise SystemExit(main())
