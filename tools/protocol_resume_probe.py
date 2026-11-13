"""断线续传探针：应答中途掐断 WS，重连后测量补发与正文连续性。

## 为什么有它

2026-11-14 的活网探针发现：一次约 400 字的回答在 2 段后掐断 WS，重连（带
`after_stream_seq` 游标）后只收到尾部状态；断线前最后 seq=880，重连后第一条
已收事件是 922，881–921 没有补发。另一次探针同理（正文 66 字符，不断线基线
为 456 字符）。同时，不断线基线里 seq 本身并不连续（存在 255 跳变），所以
**不能用 seq 缺口检测丢内容**。

因此本工具只报告事实：游标、重连次数、断线前后 seq、正文段数与长度、是否
收到权威 `delta.message_done`。它不根据这些数字断言"无损"，也不打印正文。

## 用法

    set MUSE_BRIDGE_KEY=...                 # 账号库 Fernet 密钥（必需，不回显）
    python tools/protocol_resume_probe.py
    python tools/protocol_resume_probe.py --drop-after 2 --timeout 150

可选环境变量：

    MUSE_TOOLS_ACCOUNT         要使用的账号（默认第一个有 cookie 的账号）
    MUSE_TOOLS_DB              账号库路径（默认 <repo>/data/muse.db）
    MUSE_TOOLS_PROTOCOL_PROXY  显式代理（不配置则不使用代理）

退出码：0 = 完成重连并观察到终态；1 = 协议失败；2 = 没有可用凭据。

注意：这会真实消耗一次约 400 字回答的聊天额度，并主动掐断一次连接。
只对自有/已授权账号使用；一次只发一个回合。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet  # noqa: E402

from muse_bridge.protocol_client import HatchProtocolClient  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPT = (
    "请用中文写一段约 400 字的说明，分 5 个小段，每段带一个小标题，"
    "主题：为什么应用层 WebSocket 需要自己的序号（stream seq）来做断线续传。"
    "要求只用纯文本，不要列表符号。"
)


def _db_path() -> Path:
    raw = os.environ.get("MUSE_TOOLS_DB", "").strip()
    return Path(raw) if raw else REPO_ROOT / "data" / "muse.db"


def load_cookies() -> dict[str, str] | None:
    """只读打开账号库，取第一个（或指定）有 cookie 的账号。"""
    key = os.environ.get("MUSE_BRIDGE_KEY", "").strip()
    db_path = _db_path()
    if not key or not db_path.exists():
        return None
    wanted = os.environ.get("MUSE_TOOLS_ACCOUNT", "").strip()
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


async def run(*, drop_after: int, timeout: float) -> int:
    started = time.monotonic()
    cookies = load_cookies()
    if cookies is None:
        print(json.dumps({"ok": False, "reason": "no_imported_credentials"}))
        return 2
    proxy = os.environ.get("MUSE_TOOLS_PROTOCOL_PROXY", "").strip() or None
    client = HatchProtocolClient(
        cookies,
        proxy={"server": proxy} if proxy else None,
        reconnect_backoff=0.5,
        reconnect_backoff_max=4.0,
    )
    del cookies

    subscribe_calls: list[dict] = []
    original = client.subscribe

    async def recorded_subscribe(**kwargs):
        subscribe_calls.append(
            {
                "after_stream_seq": kwargs.get("after_stream_seq"),
                "after_chat_event_seq": kwargs.get("after_chat_event_seq"),
            }
        )
        return await original(**kwargs)

    client.subscribe = recorded_subscribe  # type: ignore[method-assign]

    stats = {
        "events": 0,
        "deltas": 0,
        "text_len": 0,
        "dup_hashes": 0,
        "deltas_before_drop": 0,
        "dropped_at_s": None,
        "last_seq_before_drop": None,
        "reconnect_count": 0,
        "accepted_after_resume": 0,
        "first_seq_after_resume": None,
        "terminal_reason": None,
        "terminal_seq": None,
        "authoritative_done": False,
    }
    seen_hashes: set[str] = set()
    stream = None
    try:
        async with asyncio.timeout(timeout + 20):
            await client.connect()
            node = client.new_task_node()
            stream = client.read_stream(PROMPT, node_id=node, timeout=timeout)
            async for event in stream:
                stats["events"] += 1
                seq = event.get("seq")
                if not isinstance(seq, int):
                    seq = None
                if stats["dropped_at_s"] is None:
                    if seq is not None:
                        stats["last_seq_before_drop"] = seq
                else:
                    if seq is not None:
                        stats["accepted_after_resume"] += 1
                        if stats["first_seq_after_resume"] is None:
                            stats["first_seq_after_resume"] = seq
                payload = event.get("payload") or {}
                context = payload.get("chat_context")
                device = context.get("device_id") if isinstance(context, dict) else None
                if device is not None and str(device) != node:
                    continue
                if event.get("name") == "delta.text_append" and payload.get("text"):
                    stats["deltas"] += 1
                    stats["text_len"] += len(payload["text"])
                    if stats["dropped_at_s"] is None:
                        stats["deltas_before_drop"] += 1
                    digest = hashlib.sha256(
                        payload["text"].encode()
                    ).hexdigest()[:16]
                    if digest in seen_hashes:
                        stats["dup_hashes"] += 1
                    seen_hashes.add(digest)
                    if (
                        stats["dropped_at_s"] is None
                        and stats["deltas_before_drop"] >= drop_after
                    ):
                        stats["dropped_at_s"] = round(
                            time.monotonic() - started, 1
                        )
                        ws = getattr(client, "ws", None)
                        transport = getattr(ws, "transport", None) if ws else None
                        if transport is not None:
                            transport.abort()
                        elif ws is not None:
                            await ws.close()
                if event.get("name") == "delta.message_done":
                    status = payload.get("status")
                    if status in (None, "completed"):
                        stats["authoritative_done"] = True
                if getattr(client, "turn_terminal_reason", None):
                    stats["terminal_seq"] = seq
                    break
        stats["terminal_reason"] = getattr(client, "turn_terminal_reason", None)
        stats["reconnect_count"] = int(getattr(client, "_reconnect_count", 0))
        stats["turn_resumed"] = bool(getattr(client, "_turn_resumed", False))
        ok = (
            stats["reconnect_count"] >= 1
            and len(subscribe_calls) >= 2
            and stats["terminal_reason"] is not None
        )
        print(
            json.dumps(
                {
                    "ok": ok,
                    "via": "direct_noise_protocol",
                    "subscribe_calls": subscribe_calls,
                    **stats,
                    "unique_hashes": len(seen_hashes),
                    "elapsed_s": round(time.monotonic() - started, 3),
                }
            )
        )
        return 0 if ok else 1
    except Exception as exc:  # noqa: BLE001 - sanitized category only
        stats["terminal_reason"] = getattr(client, "turn_terminal_reason", None)
        stats["reconnect_count"] = int(getattr(client, "_reconnect_count", 0))
        stats["turn_resumed"] = bool(getattr(client, "_turn_resumed", False))
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "subscribe_calls": subscribe_calls,
                    **stats,
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
        description="Drop one self-created turn mid-answer, then report resume "
        "cursor/seq/count facts as sanitized JSON (no text, account or token)."
    )
    parser.add_argument(
        "--drop-after",
        type=int,
        default=2,
        help="abort the socket after this many own text deltas (default 2)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=150.0,
        help="per-turn read budget in seconds (default 150)",
    )
    args = parser.parse_args()
    if not 1 <= args.drop_after <= 20:
        parser.error("--drop-after must be between 1 and 20")
    if not 10 <= args.timeout <= 300:
        parser.error("--timeout must be between 10 and 300 seconds")
    return asyncio.run(run(drop_after=args.drop_after, timeout=args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())