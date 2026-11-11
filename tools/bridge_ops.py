"""冷咖啡 bridge 的运维工具集：起停、看日志、跑验收。

## 为什么有这一套

2026-11-11 那次排障留下的教训：诊断脚本和产品走的是**两套契约**，
探针自己把 `proxy` 传错形式，于是"三条链路一起失败"看起来像上游挂了，
实际是探针的 bug。验收脚本必须与产品共用同一份 env 与同一个契约，
并且要能被别人在另一台机器上原样跑起来。

原来的 `work/*.py` 有两类问题，这里都消掉了：
  * **硬编码凭据**：Fernet key 直接写在源码里。现在一律从环境变量读，
    缺失时明确报出来，不给默认值——给默认值等于把密钥再写一遍。
  * **绝对路径**：`D:\\deep seek\\...` 写死在源码里。现在全部相对仓库根推导，
    并且允许用 env 覆盖。

## 约定

env 前缀统一 `MUSE_TOOLS_`，与产品自己的 `MUSE_*` 分开，避免误覆盖：

    MUSE_TOOLS_DB          账号库路径（默认 <repo>/data/muse.db）
    MUSE_TOOLS_LOG         运行日志路径（默认 <repo>/data/bridge.log）
    MUSE_TOOLS_PORT        bridge 端口（默认 8788）
    MUSE_TOOLS_BASE        bridge 基址（默认 http://127.0.0.1:<port>）
    MUSE_TOOLS_API_KEY     Bearer key（未配置鉴权时可空）
    MUSE_TOOLS_MEDIA_DIR   成品目录（默认 <repo>/data/media）
    MUSE_TOOLS_CDP_URL     Chrome 调试端口（默认 http://127.0.0.1:9222）

凭据本身**不**在这里：`MUSE_BRIDGE_KEY`（Fernet）由调用方在环境里提供，
见 `.env.example`。工具只检查它在不在，不回显它的值。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    return os.environ.get(f"MUSE_TOOLS_{name}", default).strip()


def port() -> int:
    return int(_env("PORT", "8788"))


def base() -> str:
    explicit = _env("BASE")
    if explicit:
        return explicit.rstrip("/")
    return f"http://127.0.0.1:{port()}"


def api_key() -> str:
    """客户端 Bearer key。**不回显**：只在有没有上做分支。"""
    return _env("API_KEY")


def auth_headers() -> dict[str, str]:
    value = api_key()
    return {"Authorization": f"Bearer {value}"} if value else {}


def db_path() -> Path:
    raw = _env("DB")
    return Path(raw) if raw else REPO_ROOT / "data" / "muse.db"


def media_dir() -> Path:
    raw = _env("MEDIA_DIR")
    return Path(raw) if raw else REPO_ROOT / "data" / "media"


def log_path() -> Path:
    raw = _env("LOG")
    return Path(raw) if raw else REPO_ROOT / "data" / "bridge.log"


def cdp_url() -> str:
    return _env("CDP_URL", "http://127.0.0.1:9222").rstrip("/")


def require_bridge_key() -> str:
    """Fernet key 必须由调用方提供；这里只判在不在，绝不打印值。"""
    value = os.environ.get("MUSE_BRIDGE_KEY", "").strip()
    if not value:
        raise SystemExit(
            "缺少 MUSE_BRIDGE_KEY（账号 cookie 的 Fernet 密钥）。\n"
            "从 .env.example 复制一份 .env 并填好，或先 source 你的运行态环境。\n"
            "工具不会替你生成或回退到默认密钥：那会把密钥写进仓库。"
        )
    return value


def get_json(path: str, *, timeout: float = 20.0) -> dict:
    url = f"{base()}{path}"
    req = urllib.request.Request(url, headers=auth_headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise SystemExit(f"{url} -> HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"{url} 不可达：{exc.reason}") from exc


def read_log_tail(lines: int = 40) -> list[str]:
    """按 UTF-8 读日志尾部。

    为什么要专门一个函数：子进程以 `PYTHONUTF8=1` 写 UTF-8，而 PowerShell 的
    `Get-Content -Encoding utf8` 在这个环境里会读成 UTF-16、整屏乱码。
    诊断时按字节读再显式 UTF-8 解码，是唯一稳的做法。
    """
    path = log_path()
    if not path.exists():
        raise SystemExit(f"日志不存在：{path}")
    raw = path.read_bytes().decode("utf-8", errors="replace")
    kept = [ln for ln in raw.splitlines() if ln.strip()]
    print(f"共 {len(kept)} 行，显示最后 {lines} 行（UTF-8 解码）")
    return kept[-lines:]


# ---------------------------------------------------------------- 子命令

def cmd_health(argv: list[str]) -> int:
    info = get_json("/healthz")
    print(json.dumps(info, ensure_ascii=False, indent=1))
    return 0


def cmd_observability(argv: list[str]) -> int:
    info = get_json("/admin/observability")
    print(json.dumps(info, ensure_ascii=False, indent=1))
    return 0


def cmd_log(argv: list[str]) -> int:
    tail = int(argv[0]) if argv else 40
    for line in read_log_tail(tail):
        print(line)
    return 0


def cmd_wait(argv: list[str]) -> int:
    timeout = float(argv[0]) if argv else 40.0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base()}/healthz", timeout=3) as resp:
                info = json.loads(resp.read())
            print(f"就绪：version={info.get('version')} "
                  f"chat_driver={info.get('chat_driver')} "
                  f"dispatchable={info.get('accounts_dispatchable')}")
            return 0
        except Exception:
            time.sleep(1)
    print("等待超时")
    return 1


def cmd_preflight(argv: list[str]) -> int:
    """跑验收前的自检：把"环境没配好"和"功能有 bug"分开。"""
    print(f"仓库根   {REPO_ROOT}")
    print(f"鉴权     {'已配置' if api_key() else '未配置（匿名访问）'}")
    problems: list[str] = []

    try:
        require_bridge_key()
        print("Fernet   MUSE_BRIDGE_KEY 已提供（值不回显）")
    except SystemExit as exc:
        problems.append(str(exc))

    db = db_path()
    print(f"账号库   {db} {'存在' if db.exists() else '缺失'}")
    if not db.exists():
        problems.append("账号库不存在：先执行 attach 或检查 MUSE_TOOLS_DB")

    md = media_dir()
    count = len(list(md.iterdir())) if md.exists() else 0
    print(f"成品目录 {md} ({count} 个文件)")

    lp = log_path()
    print(f"运行日志 {lp} {'存在' if lp.exists() else '缺失（bridge 可能没在写文件）'}")

    try:
        info = get_json("/healthz")
        print(f"bridge   ok  version={info.get('version')} "
              f"driver={info.get('chat_driver')} "
              f"可派单={info.get('accounts_dispatchable')}")
    except SystemExit as exc:
        problems.append(str(exc))

    if problems:
        print("\n阻塞项：")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("\n自检通过")
    return 0


COMMANDS = {
    "health": cmd_health,
    "observability": cmd_observability,
    "log": cmd_log,
    "wait": cmd_wait,
    "preflight": cmd_preflight,
}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        print("子命令：" + " / ".join(COMMANDS))
        return 0
    name, rest = args[0], args[1:]
    handler = COMMANDS.get(name)
    if handler is None:
        print(f"未知子命令：{name}")
        print("可用：" + " / ".join(COMMANDS))
        return 2
    return handler(rest)


if __name__ == "__main__":
    raise SystemExit(main())

