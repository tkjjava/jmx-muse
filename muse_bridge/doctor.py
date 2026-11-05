"""自检工具：验证 muse-bridge 的运行前提是否齐备。

用法：

    python -m muse_bridge.doctor

它回答五个问题：
  1. 加密密钥能用吗
  2. 数据库能读写吗
  3. playwright 装了吗
  4. Chrome 调试端口通吗
  5. 那个 Chrome 里登录 muse.ai 了吗

**不登录、不注册、不改任何上游状态。**
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import httpx

from . import __author__, __mark__, __version__
from .driver import CDP_URL, SITE


class CheckResult:
    def __init__(self, name: str) -> None:
        self.name = name
        self.ok = False
        self.detail = ""
        self.hint = ""

    def __str__(self) -> str:
        mark = "✓" if self.ok else "✗"
        line = f"  {mark} {self.name}"
        if self.detail:
            line += f" — {self.detail}"
        if not self.ok and self.hint:
            line += f"\n      提示：{self.hint}"
        return line


def check_key() -> CheckResult:
    result = CheckResult("加密密钥")
    raw = os.environ.get("MUSE_BRIDGE_KEY", "").strip()
    if not raw:
        result.detail = "未设置 MUSE_BRIDGE_KEY"
        result.hint = (
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
        return result

    try:
        from cryptography.fernet import Fernet

        cipher = Fernet(raw.encode())
        if cipher.decrypt(cipher.encrypt(b"probe")) != b"probe":
            result.detail = "加解密往返失败"
            return result
    except Exception as exc:
        result.detail = f"{type(exc).__name__}: {exc}"
        result.hint = "重新生成密钥并完整复制"
        return result

    result.ok = True
    result.detail = "加解密往返正常"
    return result


def check_store() -> CheckResult:
    result = CheckResult("数据库读写")
    try:
        from .state import AccountState
        from .store import AccountStore

        with tempfile.TemporaryDirectory() as tmp:
            store = AccountStore(os.path.join(tmp, "probe.db"))
            store.upsert(
                "__probe__",
                cdp_url="http://127.0.0.1:9222",
                state=AccountState.READY,
                profile={"marker": "probe-secret-value"},
                vm_ready=True,
            )
            record = store.get("__probe__")
            with open(os.path.join(tmp, "probe.db"), "rb") as handle:
                raw = handle.read()
            store.close()

        if record is None or record.account != "__probe__":
            result.detail = "写入后读回不一致"
            return result
        if b"probe-secret-value" in raw:
            result.detail = "profile 以明文落盘"
            result.hint = "这是严重问题，请提 issue"
            return result
    except Exception as exc:
        result.detail = f"{type(exc).__name__}: {exc}"
        result.hint = "检查 data 目录写权限"
        return result

    result.ok = True
    result.detail = "写入、加密、读回均正常"
    return result


def check_playwright() -> CheckResult:
    result = CheckResult("playwright 依赖")
    try:
        import playwright  # noqa: F401
    except ImportError:
        result.detail = "未安装"
        result.hint = "python -m pip install playwright"
        return result

    result.ok = True
    try:
        from importlib.metadata import version

        result.detail = f"已安装 v{version('playwright')}"
    except Exception:
        result.detail = "已安装"
    return result


async def check_cdp(cdp_url: str) -> CheckResult:
    result = CheckResult("Chrome 调试端口")
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(f"{cdp_url}/json/version")
    except httpx.HTTPError as exc:
        result.detail = f"连不上 {cdp_url}（{type(exc).__name__}）"
        result.hint = (
            "用 --remote-debugging-port=9222 和独立 --user-data-dir "
            "启动 Chrome"
        )
        return result

    if response.status_code != 200:
        result.detail = f"HTTP {response.status_code}"
        return result

    try:
        payload = response.json()
        browser = payload.get("Browser", "未知")
    except ValueError:
        browser = "未知"

    result.ok = True
    result.detail = f"{browser}"
    return result


async def check_logged_in() -> CheckResult:
    result = CheckResult("muse.ai 登录态")
    try:
        from .driver import MuseDriver

        driver = MuseDriver()
        try:
            status = await driver.status()
        finally:
            await driver.close()
    except Exception as exc:
        result.detail = f"{type(exc).__name__}: {exc}"
        result.hint = "先确保上一项通过"
        return result

    if not status.connected:
        result.detail = status.note or "浏览器不可用"
        return result
    if not status.logged_in:
        result.detail = "页面未显示登录态元素"
        result.hint = f"在那个 Chrome 里打开 {SITE} 并登录"
        return result

    result.ok = True
    result.detail = f"已登录（{status.url}）"
    return result


async def _run(cdp_url: str) -> int:
    print(f"muse-bridge {__version__}  ·  {__author__}  ·  {__mark__}")
    print(f"CDP：{cdp_url}")
    print(f"站点：{SITE}\n")

    results = [check_key(), check_store(), check_playwright()]
    cdp = await check_cdp(cdp_url)
    results.append(cdp)

    # 前四项都过了才检查登录态，否则报错会层层叠加
    if all(item.ok for item in results):
        results.append(await check_logged_in())

    for item in results:
        print(item)

    failed = [item for item in results if not item.ok]
    print()
    if failed:
        print(f"{len(failed)} 项未通过")
        return 1
    print("全部通过")
    return 0


def main() -> int:
    return asyncio.run(_run(CDP_URL))


if __name__ == "__main__":
    raise SystemExit(main())
