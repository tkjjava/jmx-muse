"""协议链路一条龙：从已登录的 Chrome 抓 cookie → 导入桥 → 纯 HTTP 续期 → 验证。

## 为什么有它

桥现在协议优先（Noise 直连 muse.ai），但协议的凭据是浏览器里的 `hatch_sess`
等 cookie。以前要"开浏览器 → attach → 再续期"，运行期还挂着 CDP；正确姿势是
**抓一次 cookie 导入桥**，之后 bridge/relay 的运行期只有协议，不需要浏览器。

本工具把这一串动作合成一条命令，幂等：重复跑只是重新导入 + 重新续期。

## 用法

    python tools/protocol_flow.py                 # 默认账号 muse1，导入并验证
    python tools/protocol_flow.py --dry-run       # 只抓 cookie 看名字，不写桥
    python tools/protocol_flow.py --verify-chat   # 额外跑一次最小聊天，确认 driver=protocol
    python tools/protocol_flow.py 账号名

鉴权：`MUSE_TOOLS_API_KEY`，未设置时依次回退 `MUSE_BRIDGE_API_KEYS`、
`RELAY_MUSE_BRIDGE_KEY`（start-stack / setup-multiuser 写的就是最后那个）。
Chrome 调试端口读 `MUSE_TOOLS_CDP_URL`（默认 9222），桥基址读 `MUSE_TOOLS_BASE`。

**不打印 cookie 值**：只打印名字与数量，响应体也只回声名字。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from tools.bridge_ops import auth_headers, base, cdp_url, get_json  # noqa: E402

DEFAULT_ACCOUNT = "muse1"
CHAT_PROMPT = "只回复 OK"
AUTH_ENV_FALLBACKS = ("MUSE_BRIDGE_API_KEYS", "RELAY_MUSE_BRIDGE_KEY")


def _seed_auth() -> None:
    """bridge_ops 只认 MUSE_TOOLS_API_KEY；把运维环境里的等价密钥接过来。"""
    if os.environ.get("MUSE_TOOLS_API_KEY"):
        return
    for name in AUTH_ENV_FALLBACKS:
        raw = os.environ.get(name, "").strip()
        if raw:
            os.environ["MUSE_TOOLS_API_KEY"] = raw.split(",")[0].strip()
            return


def grab_cookies() -> dict[str, str]:
    """从 CDP 连上已登录的 Chrome，取 muse.ai 的 cookie（含 hatch_sess 优先）。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            "缺少 playwright：pip install playwright（连多个 Chrome 只需驱动，不必装浏览器）"
        ) from None
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(cdp_url())
        except Exception as exc:
            raise SystemExit(f"连不上 Chrome 调试端口 {cdp_url()}：{exc}") from exc
        try:
            candidates: list[dict[str, str]] = []
            for ctx in browser.contexts:
                muse = {
                    c["name"]: c["value"]
                    for c in ctx.cookies()
                    if "muse.ai" in (c.get("domain") or "") and c.get("value")
                }
                if muse.get("hatch_sess"):
                    return muse
                if muse:
                    candidates = candidates or [muse]
            return candidates[0] if candidates else {}
        finally:
            browser.close()


def _httpx() -> httpx.Client:
    return httpx.Client(timeout=180, trust_env=False)


def run(account: str, *, dry_run: bool, verify_chat: bool) -> int:
    cookies = grab_cookies()
    if not cookies:
        print("muse.ai 没有任何 cookie：这个 Chrome 大概没登录 muse.ai。", file=sys.stderr)
        return 1
    print(f"抓到 {len(cookies)} 条 cookie：{sorted(cookies)}")
    if "hatch_sess" not in cookies:
        print("缺少 hatch_sess（协议会话必需），不导入。", file=sys.stderr)
        return 1
    if dry_run:
        print("--dry-run：不写桥。")
        return 0

    with _httpx() as client:
        r = client.post(
            f"{base()}/admin/accounts/{account}/cookies",
            headers=auth_headers(),
            json={"cookies": cookies},
        )
        print(f"导入 -> {r.status_code} {r.text[:200]}")
        if r.status_code != 200:
            return 1

        r = client.post(f"{base()}/admin/accounts/{account}/keepalive", headers=auth_headers())
        print(f"续期 -> {r.status_code} {r.text[:200]}")
        if r.status_code != 200:
            return 1

    health = get_json("/healthz")
    dispatchable = health.get("accounts_dispatchable", 0)
    driver = health.get("chat_driver")
    print(f"health: chat_driver={driver} accounts_dispatchable={dispatchable}/{health.get('accounts_total')}")
    if driver != "protocol" or not dispatchable:
        print(
            "协议链路还没就绪：确认 MUSE_PROTOCOL_DRIVER/MUSE_PROTOCOL_PROXY 已配置，"
            "且这个账号的 cookie 是新抓的。",
            file=sys.stderr,
        )
        return 1

    if verify_chat:
        with _httpx() as client:
            r = client.post(
                f"{base()}/v1/chat/completions",
                headers=auth_headers(),
                json={"model": "muse-1", "messages": [{"role": "user", "content": CHAT_PROMPT}]},
            )
        used = r.headers.get("x-muse-driver")
        try:
            content = r.json()["choices"][0]["message"]["content"][:40]
        except (KeyError, IndexError, ValueError):
            content = r.text[:120]
        print(f"聊天 -> {r.status_code} driver={used} content={content!r}")
        if r.status_code != 200 or used != "protocol":
            print("期望 200 且 X-Muse-Driver: protocol。", file=sys.stderr)
            return 1

    print("协议链路就绪：运行期不需要浏览器/CDP。")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    dry_run = verify_chat = False
    account = ""
    for token in args:
        if token == "--dry-run":
            dry_run = True
        elif token == "--verify-chat":
            verify_chat = True
        elif token.startswith("-"):
            print(f"不认识的参数：{token}", file=sys.stderr)
            return 2
        elif not account:
            account = token
        else:
            print(f"多余的位置参数：{token}", file=sys.stderr)
            return 2
    _seed_auth()
    return run(account or DEFAULT_ACCOUNT, dry_run=dry_run, verify_chat=verify_chat)


if __name__ == "__main__":
    raise SystemExit(main())
