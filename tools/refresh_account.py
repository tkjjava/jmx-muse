"""刷新账号探针，让账号回到可派单状态。

## 什么时候需要它

`is_dispatchable` 对 READY 状态还要求 `last_probe_ok_at` 在 `PROBE_TTL_SECONDS`
内。探针过期后账号会显示"不可派单"，`/healthz` 的
`accounts_dispatchable` 变成 0，所有请求回 503「没有可用的 muse 账号」。
这时不需要重新登录，刷一次探针即可。

## 用法

    python tools/refresh_account.py            # 默认刷 muse1
    python tools/refresh_account.py 账号名

鉴权读 `MUSE_TOOLS_API_KEY`（`/admin/*` 若没配管理密钥，走普通 key 或匿名）。
基线地址读 `MUSE_TOOLS_BASE` / `MUSE_TOOLS_PORT`。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from tools.bridge_ops import auth_headers, base, get_json  # noqa: E402


def refresh(account: str) -> int:
    url = f"{base()}/admin/accounts/{account}/refresh"
    try:
        resp = httpx.post(url, headers=auth_headers(), timeout=180, trust_env=False)
    except httpx.HTTPError as exc:
        print(f"{url} 不可达：{exc}")
        return 1
    body = resp.text[:400]
    print(f"refresh -> {resp.status_code} {body}")
    if resp.status_code != 200:
        return 1

    info = get_json("/healthz")
    dispatchable = info.get("accounts_dispatchable")
    total = info.get("accounts_total")
    print(f"可派单 {dispatchable}/{total}")
    if not dispatchable:
        print(
            "账号仍不可派单。看它的 last_error：\n"
            "  * 会话过期 -> 需要重新导入 cookie\n"
            "  * 浏览器不可用 -> 检查 CDP 端点与出口网络"
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    account = args[0] if args else "muse1"
    return refresh(account)


if __name__ == "__main__":
    raise SystemExit(main())
