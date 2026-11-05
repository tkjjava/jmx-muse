"""启动冒烟测试：拉起真实服务，打一次 /healthz，然后关掉。

CI 用这个代替 shell 里的后台进程控制——跨平台，且失败时能看到
实际响应内容而不是只有一个退出码。

muse 的冒烟不连接浏览器：只验证服务本身能起来、路由能响应。

用法：

    python -m muse_bridge.smoke
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

PORT = int(os.environ.get("MUSE_BRIDGE_PORT", "18788"))
BASE = f"http://127.0.0.1:{PORT}"
TIMEOUT_SECONDS = 40.0
INTERVAL = 0.5


def ensure_utf8_stdout() -> None:
    """把 stdout/stderr 切到 UTF-8。

    GitHub Actions 的 windows-latest 上，Python 的 stdout 是 cp1252。
    本项目的作者署名是中文，任何 print 都会抛 UnicodeEncodeError 让构建挂掉。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=5) as response:
        return json.loads(response.read().decode())


def wait_until_ready() -> bool:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    last_error = ""
    while time.monotonic() < deadline:
        try:
            payload = _get("/healthz")
            if payload.get("status") == "ok":
                return True
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last_error = str(exc)
        time.sleep(INTERVAL)
    print(f"服务在 {TIMEOUT_SECONDS}s 内未就绪: {last_error}", file=sys.stderr)
    return False


def main() -> int:
    ensure_utf8_stdout()

    import uvicorn

    from .app import app

    config = uvicorn.Config(
        app, host="127.0.0.1", port=PORT, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    try:
        if not wait_until_ready():
            return 1

        health = _get("/healthz")
        print("healthz:", json.dumps(health, ensure_ascii=False))

        models = _get("/v1/models")
        count = len(models.get("data") or [])
        print(f"models: {count}")
        if count == 0:
            print("模型列表为空", file=sys.stderr)
            return 1

        # 所有模型别名必须指向同一个上游
        upstreams = {item.get("upstream") for item in models.get("data") or []}
        print(f"upstream: {upstreams}")
        if len(upstreams) != 1:
            print("模型别名指向了多个上游，与预期不符", file=sys.stderr)
            return 1

        print("冒烟测试通过")
        return 0
    finally:
        server.should_exit = True
        thread.join(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
