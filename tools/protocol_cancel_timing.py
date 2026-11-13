"""取消时序探针：显式取消一个**自己发起的**回合，记录 ACK 与终态之间的行为。

## 为什么有它

2026-11-14 的活网核验发现：`POST /chat/cancel` 返回 `cancelled=true` 之后，
仍会收到已缓冲的正文增量，回合最终可能以 `agent.status:online` 收尾，而不是
`delta.message_done`。这条结论必须能被第三方独立复现，否则"ACK 之后还有正文"
只能算一次性的观察。

本工具只做一件事：发一条短文本 → 收到第一个本回合正文后显式取消 → 继续消费到
终态或观察窗口结束，输出**只有布尔值、计数与耗时**的 JSON。

## 不泄露什么

- 不打印正文、提示词、账号名、设备 ID、token URL 或异常原文；
- 账号库以 SQLite 只读 URI 打开，不写库、不改账号状态；
- 失败只输出阶段名、异常类型与数字 HTTP 状态码。

## 用法

    set MUSE_BRIDGE_KEY=...            # 账号库 Fernet 密钥（必需，不回显）
    python tools/protocol_cancel_timing.py
    python tools/protocol_cancel_timing.py --account muse1
    python tools/protocol_cancel_timing.py --observe 10    # ACK 后观察秒数

可选环境变量：

    MUSE_TOOLS_ACCOUNT       要使用的账号（默认第一个有 cookie 的账号）
    MUSE_TOOLS_DB            账号库路径（默认 <repo>/data/muse.db）
    MUSE_TOOLS_PROTOCOL_PROXY 显式代理（不配置则不使用代理）

退出码：0 = 收到 ACK；1 = 协议失败；2 = 没有可用凭据。

注意：这会真实消耗一次短聊天额度；只对自有/已授权账号使用，一次只发一个回合。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet  # noqa: E402

from muse_bridge.protocol_client import HatchProtocolClient  # noqa: E402

PROMPT = (
    "For a protocol cancellation test, write a detailed plain-text explanation "
    "of 30 separate everyday uses of a paper notebook, one paragraph per use."
)
REPO_ROOT = Path(__file__).resolve().parent.parent


def _db_path() -> Path:
    raw = os.environ.get("MUSE_TOOLS_DB", "").strip()
    return Path(raw) if raw else REPO_ROOT / "data" / "muse.db"


def _account() -> str:
    return os.environ.get("MUSE_TOOLS_ACCOUNT", "").strip()


def load_cookies() -> dict[str, str] | None:
    """只读打开账号库，取第一个（或指定）有 cookie 的账号。"""
    key = os.environ.get("MUSE_BRIDGE_KEY", "").strip()
    if not key:
        return None
    db_path = _db_path()
    if not db_path.exists():
        return None
    wanted = _account()
    cipher = Fernet(key.encode())
    db = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
    try:
        rows = db.execute(
            "SELECT account, profile_enc FROM accounts ORDER BY account"
        ).fetchall()
    finally:
        db.close()
    for account, blob in rows:
        if not blob or (wanted and account != wanted):
            continue
        profile = json.loads(cipher.decrypt(blob))
        cookies = profile.get("cookies")
        if isinstance(cookies, dict) and cookies:
            return cookies
    return None


async def run(observe: float) -> int:
    started = time.monotonic()
    cookies = load_cookies()
    if cookies is None:
        print(json.dumps({"ok": False, "reason": "no_imported_credentials"}))
        return 2
    proxy = os.environ.get("MUSE_TOOLS_PROTOCOL_PROXY", "").strip() or None
    client = HatchProtocolClient(cookies, proxy={"server": proxy} if proxy else None)
    del cookies

    stage = "connect"
    ack: dict | None = None
    own_text_events = 0
    after_ack_text_events = 0
    done_statuses: list[str] = []
    saw_message_done = False
    ack_at: float | None = None
    cancel_started: float | None = None
    stream = None
    observed_terminal = False
    try:
        async with asyncio.timeout(120):
            await client.connect()
            stage = "read_own_turn"
            node = client.new_task_node()
            stream = client.read_stream(PROMPT, node_id=node, timeout=90)
            async for event in stream:
                payload = event.get("payload") or {}
                context = payload.get("chat_context")
                device = context.get("device_id") if isinstance(context, dict) else None
                if device is not None and str(device) != node:
                    continue  # Another client's turn must not drive our cancel.
                name = event.get("name")
                if name == "delta.text_append" and payload.get("text"):
                    own_text_events += 1
                    if ack_at is not None:
                        after_ack_text_events += 1
                    if ack is None:
                        stage = "cancel"
                        cancel_started = time.monotonic()
                        ack = await client.cancel_chat(timeout=15)
                        ack_at = time.monotonic()
                        stage = "observe_after_cancel"
                if name == "delta.message_done":
                    saw_message_done = True
                    status = payload.get("status")
                    if isinstance(status, str):
                        done_statuses.append(status)
                if client.turn_terminal_reason is not None:
                    observed_terminal = True
                    break
                if ack_at is not None and time.monotonic() - ack_at > observe:
                    break
        result = {
            "ok": ack is not None,
            "via": "direct_noise_protocol",
            "cancelled_ack": ack.get("cancelled") if ack else None,
            "own_text_before_cancel": own_text_events > 0,
            "ack_latency_s": round(ack_at - cancel_started, 3)
            if ack_at is not None and cancel_started is not None
            else None,
            "after_ack_text_events": after_ack_text_events,
            "post_ack_observation_s": round(time.monotonic() - ack_at, 3)
            if ack_at is not None
            else None,
            "terminal_reason": client.turn_terminal_reason,
            "recognized_terminal": observed_terminal,
            "saw_message_done": saw_message_done,
            "done_statuses": done_statuses,
            "elapsed_s": round(time.monotonic() - started, 3),
        }
        print(json.dumps(result))
        return 0 if ack is not None else 1
    except Exception as exc:  # noqa: BLE001 - sanitized category only
        print(
            json.dumps(
                {
                    "ok": False,
                    "stage": stage,
                    "error_type": type(exc).__name__,
                    "cancelled_ack": ack.get("cancelled") if ack else None,
                    "elapsed_s": round(time.monotonic() - started, 3),
                }
            )
        )
        return 1
    finally:
        if stream is not None:
            await stream.aclose()
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Cancel one self-created turn and report ACK-to-terminal timing "
        "(sanitized JSON; no text, account, token or URL)."
    )
    parser.add_argument(
        "--observe",
        type=float,
        default=8.0,
        help="seconds to keep reading after the cancel ACK (default 8)",
    )
    args = parser.parse_args()
    if not 0.1 <= args.observe <= 60:
        parser.error("--observe must be between 0.1 and 60 seconds")
    return asyncio.run(run(args.observe))


if __name__ == "__main__":
    raise SystemExit(main())