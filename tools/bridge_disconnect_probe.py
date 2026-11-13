"""Bridge-level live disconnect probe: isolated instance + controlled proxy.

## 为什么有它

引擎层与驱动层的恢复逻辑已被单元测试和直连探针覆盖，但那些都绕过了 bridge 的
HTTP 面。真实用户看到的是 `/v1/chat/completions` 的 SSE：只有走完
``pool → driver → engine`` 整条链，才能证明"断线后用户仍拿到完整回答"。

本工具做一次完整复现，且不碰生产实例：

1. 把账号库**复制**到临时目录，用独立端口起一个 bridge 子进程
   （`MUSE_PROTOCOL_DRIVER=1`，代理指向本工具自建的 CONNECT 中继）；
2. 以 SSE 发起一次约 400 字回答，收到第一个正文块后**掐断中继里的所有隧道**
   （监听还在，所以重连能成功），模拟上游 WS 断线；
3. 断言 SSE 仍以 `finish_reason=stop` 结束、正文长度等于已提交历史里的长度、
   `/admin/observability` 的 `chat_history_recovered` 与 `chat_done_tail_emitted` ≥ 1。

输出只有长度、标志与计数，不打印正文、账号名、cookie 或令牌。

## 用法

    set MUSE_BRIDGE_KEY=...              # 账号库 Fernet 密钥（必需，不回显）
    python tools/bridge_disconnect_probe.py

可选环境变量：

    MUSE_TOOLS_DB               源账号库（默认 <repo>/data/muse.db）
    MUSE_TOOLS_PROTOCOL_PROXY   直连历史交叉核对用的真实代理（默认沿用
                                MUSE_PROTOCOL_PROXY）
    MUSE_TOOLS_RELAY_PORT       自建中继端口（默认 7891）
    MUSE_TOOLS_CONTROL_PORT     中继控制端口（默认 7892）
    MUSE_TOOLS_BRIDGE_PORT      临时 bridge 端口（默认 8799）

退出码：0 = 全链路验证通过；1 = 断言失败；2 = 环境不满足（端口占用/库缺失）。

注意：会真实消耗一次约 400 字回答的额度；只对自有/已授权账号使用。
bridge 日志落在 `<repo>/data/bridge-disconnect-probe.log`（不含提示词正文）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
API_KEY = "probe-key"
ADMIN_KEY = "probe-admin"
UPSTREAM = ("127.0.0.1", 7890)
PROMPT = (
    "请用中文写一段约 400 字的说明，分 5 个小段，每段带一个小标题，"
    "主题：为什么应用层 WebSocket 需要自己的序号（stream seq）来做断线续传。"
    "要求只用纯文本，不要列表符号。"
)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        raise SystemExit(f"{name} 必须是整数，当前值：{raw!r}") from None


def _src_db() -> Path:
    raw = os.environ.get("MUSE_TOOLS_DB", "").strip()
    return Path(raw) if raw else REPO_ROOT / "data" / "muse.db"


def _direct_proxy() -> str | None:
    return (
        os.environ.get("MUSE_TOOLS_PROTOCOL_PROXY", "").strip()
        or os.environ.get("MUSE_PROTOCOL_PROXY", "").strip()
        or None
    )


class Relay:
    """Minimal HTTP CONNECT proxy with a kill switch; listener stays up."""

    def __init__(self, port: int, control_port: int, upstream: tuple[str, int]) -> None:
        self.port = port
        self.control_port = control_port
        self.upstream = upstream
        self.tunnels: list[tuple[asyncio.StreamWriter, asyncio.StreamWriter]] = []
        self.server: asyncio.base_events.Server | None = None
        self.control: asyncio.base_events.Server | None = None
        self.kills = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        self.control = await asyncio.start_server(
            self._control, "127.0.0.1", self.control_port
        )

    async def _pump(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except Exception:  # noqa: BLE001 - tunnel teardown is normal
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except Exception:  # noqa: BLE001
            writer.close()
            return
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(*self.upstream)
        except Exception:  # noqa: BLE001
            writer.close()
            return
        self.tunnels.append((writer, upstream_writer))
        try:
            upstream_writer.write(head)
            await upstream_writer.drain()
            if head.upper().startswith(b"CONNECT "):
                response = await upstream_reader.readuntil(b"\r\n\r\n")
                writer.write(response)
                await writer.drain()
                if not response.upper().startswith((b"HTTP/1.1 200", b"HTTP/1.0 200")):
                    raise RuntimeError("upstream CONNECT refused")
            await asyncio.gather(
                self._pump(reader, upstream_writer),
                self._pump(upstream_reader, writer),
            )
        except Exception:  # noqa: BLE001
            pass
        finally:
            for writer_ in (writer, upstream_writer):
                with contextlib.suppress(Exception):
                    writer_.close()

    def kill_tunnels(self) -> None:
        self.kills += 1
        for client_writer, upstream_writer in list(self.tunnels):
            for writer in (client_writer, upstream_writer):
                with contextlib.suppress(Exception):
                    writer.transport.abort()
                with contextlib.suppress(Exception):
                    writer.close()
        self.tunnels.clear()

    async def _control(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            while (await reader.readline()) not in (b"\r\n", b""):
                continue
            if line.startswith(b"POST /kill"):
                self.kill_tunnels()
                writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok")
            else:
                writer.write(b"HTTP/1.1 404 Not Found\r\ncontent-length: 2\r\n\r\nno")
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def close(self) -> None:
        for server in (self.server, self.control):
            if server is not None:
                server.close()
        for client_writer, upstream_writer in self.tunnels:
            for writer in (client_writer, upstream_writer):
                with contextlib.suppress(Exception):
                    writer.close()
        self.tunnels.clear()


def _port_free(port: int) -> bool:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


async def _latest_committed_len(db_path: Path) -> int:
    """Cross-check: fetch the latest committed assistant length directly."""
    from cryptography.fernet import Fernet

    from muse_bridge.history import latest_assistant_text
    from muse_bridge.protocol_client import HatchProtocolClient

    key = os.environ.get("MUSE_BRIDGE_KEY", "").strip()
    if not key:
        return 0
    cipher = Fernet(key.encode())
    db = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
    try:
        rows = db.execute(
            "SELECT account, profile_enc FROM accounts ORDER BY account"
        ).fetchall()
    finally:
        db.close()
    cookies = None
    for _account, blob in rows:
        if not blob:
            continue
        candidate = json.loads(cipher.decrypt(blob)).get("cookies")
        if isinstance(candidate, dict) and candidate:
            cookies = candidate
            break
    if cookies is None:
        return 0
    proxy = _direct_proxy()

    last_error: Exception | None = None
    for attempt in range(3):
        client = HatchProtocolClient(
            cookies,
            proxy={"server": proxy} if proxy else None,
            reconnect_backoff=0.5,
            reconnect_backoff_max=4.0,
        )
        try:
            async with asyncio.timeout(90):
                await client.connect()
                for _ in range(5):
                    page = await client.chat_history(limit=5, timeout=20)
                    text = latest_assistant_text(page)
                    if text:
                        return len(text)
                    await asyncio.sleep(1.0)
                return 0
        except Exception as exc:  # noqa: BLE001 - cross-check is optional
            last_error = exc
            await asyncio.sleep(2.0 * (attempt + 1))
        finally:
            with contextlib.suppress(Exception):
                await client.close()
    if last_error is not None:
        raise last_error
    return 0


async def run(*, bridge_port: int, relay_port: int, control_port: int) -> int:
    started = time.monotonic()
    src_db = _src_db()
    if not src_db.exists():
        print(json.dumps({"ok": False, "reason": "db_missing"}))
        return 2
    for port in (relay_port, control_port, bridge_port):
        if not _port_free(port):
            print(json.dumps({"ok": False, "reason": f"port_busy:{port}"}))
            return 2
    work = Path(tempfile.mkdtemp(prefix="muse_bridge_probe_"))
    (work / "data").mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_db, work / "data" / "muse.db")
    media_dir = work / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    relay = Relay(relay_port, control_port, UPSTREAM)
    await relay.start()
    log_path = work / "bridge.log"
    log_file = log_path.open("wb")
    env = dict(os.environ)
    env.update(
        {
            "MUSE_BRIDGE_HOST": "127.0.0.1",
            "MUSE_BRIDGE_PORT": str(bridge_port),
            "MUSE_BRIDGE_DB": str(work / "data" / "muse.db"),
            "MUSE_MEDIA_DIR": str(media_dir),
            "MUSE_BRIDGE_API_KEYS": API_KEY,
            "MUSE_BRIDGE_ADMIN_KEYS": ADMIN_KEY,
            "MUSE_PROTOCOL_DRIVER": "1",
            "MUSE_WARM_POOL": "0",
            "MUSE_PROTOCOL_PROXY": f"http://127.0.0.1:{relay_port}",
            "MUSE_SESSION_PROXY": f"http://127.0.0.1:{relay_port}",
            "MUSE_STREAM_IDLE_TIMEOUT": "0",
            "PYTHONUNBUFFERED": "1",
        }
    )
    bridge = subprocess.Popen(
        [sys.executable, "-m", "muse_bridge"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    client = httpx.AsyncClient(
        trust_env=False, timeout=httpx.Timeout(240.0, connect=10.0)
    )
    result: dict = {"ok": False}
    try:
        base = f"http://127.0.0.1:{bridge_port}"
        ready = False
        for _ in range(120):
            if bridge.poll() is not None:
                break
            try:
                response = await client.get(f"{base}/healthz")
                if response.status_code == 200:
                    ready = True
                    break
            except Exception:  # noqa: BLE001 - startup polling
                pass
            await asyncio.sleep(0.25)
        if not ready:
            result = {"ok": False, "stage": "startup"}
            return _report(result, bridge, log_path)
        # Fresh copy may carry a stale probe timestamp: refresh via admin API.
        temp_db = sqlite3.connect(str(work / "data" / "muse.db"))
        try:
            names = [
                row[0]
                for row in temp_db.execute("SELECT account FROM accounts").fetchall()
            ]
        finally:
            temp_db.close()
        if names:
            try:
                response = await client.post(
                    f"{base}/admin/accounts/{names[0]}/keepalive",
                    headers={"Authorization": f"Bearer {ADMIN_KEY}"},
                    timeout=120.0,
                )
                result["keepalive_status"] = response.status_code
            except Exception as exc:  # noqa: BLE001
                result["keepalive_status"] = type(exc).__name__
        headers = {"Authorization": f"Bearer {API_KEY}"}
        body = {
            "model": "muse",
            "stream": True,
            "messages": [{"role": "user", "content": PROMPT}],
        }
        content_len = 0
        content_chunks = 0
        killed_at: float | None = None
        finish_reason: str | None = None
        error: str | None = None
        try:
            async with asyncio.timeout(240):
                async with client.stream(
                    "POST",
                    f"{base}/v1/chat/completions",
                    json=body,
                    headers=headers,
                    timeout=httpx.Timeout(240.0, connect=10.0),
                ) as response:
                    driver = response.headers.get("X-Muse-Driver")
                    if response.status_code != 200:
                        raw_body = (await response.aread()).decode("utf-8", "replace")
                        try:
                            detail = json.loads(raw_body)
                        except json.JSONDecodeError:
                            detail = {"raw_len": len(raw_body)}
                        result["http_status"] = response.status_code
                        result["detail"] = detail
                        return _report(result, bridge, log_path)
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        raw = line[6:].strip()
                        if raw == "[DONE]":
                            break
                        try:
                            payload = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        choices = payload.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        text = (choice.get("delta") or {}).get("content")
                        if text:
                            content_len += len(text)
                            content_chunks += 1
                            if killed_at is None:
                                await asyncio.sleep(0.3)
                                try:
                                    await client.post(
                                        f"http://127.0.0.1:{control_port}/kill"
                                    )
                                    killed_at = round(time.monotonic() - started, 2)
                                except Exception:  # noqa: BLE001
                                    pass
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
        except Exception as exc:  # noqa: BLE001 - report category only
            error = type(exc).__name__ + ": " + str(exc)[:160]
        counters: dict = {}
        try:
            adm = await client.get(
                f"{base}/admin/observability",
                headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            )
            counters = (adm.json() or {}).get("counters") or {}
        except Exception:  # noqa: BLE001
            pass
        try:
            committed = await _latest_committed_len(src_db)
            history_error = None
        except Exception as exc:  # noqa: BLE001
            committed = 0
            history_error = type(exc).__name__
        length_verified = committed > 0 and content_len == committed
        metrics_verified = (
            error is None
            and finish_reason == "stop"
            and content_chunks >= 2
            and killed_at is not None
            and counters.get("chat_history_recovered", 0) >= 1
            and counters.get("chat_done_tail_emitted", 0) >= 1
        )
        result = {
            "ok": metrics_verified and (length_verified or committed == 0),
            "length_verified": length_verified,
            "stage": "done",
            "driver": driver,
            "http_error": error,
            "content_chunks": content_chunks,
            "content_len": content_len,
            "committed_len": committed,
            "history_error": history_error,
            "finish_reason": finish_reason,
            "relay_killed_at_s": killed_at,
            "counters": {
                key: counters.get(key)
                for key in (
                    "protocol_driver_built",
                    "chat_history_recovered",
                    "chat_history_recovery_missed",
                    "chat_history_recovery_failed",
                    "chat_done_tail_emitted",
                    "protocol_fallback",
                )
            },
            "elapsed_s": round(time.monotonic() - started, 2),
        }
        return _report(result, bridge, log_path)
    finally:
        await client.aclose()
        bridge.terminate()
        try:
            bridge.wait(timeout=10)
        except subprocess.TimeoutExpired:
            bridge.kill()
        await relay.close()
        log_file.close()
        kept = REPO_ROOT / "data" / "bridge-disconnect-probe.log"
        try:
            shutil.copy2(log_path, kept)
            print(json.dumps({"bridge_log": str(kept)}))
        except OSError:
            pass
        shutil.rmtree(work, ignore_errors=True)


def _report(result: dict, bridge: subprocess.Popen, log_path: Path) -> int:
    del bridge, log_path
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Isolated bridge-level live disconnect probe (sanitized "
        "lengths/flags/counters only)."
    )
    parser.add_argument(
        "--bridge-port",
        type=int,
        default=_env_int("MUSE_TOOLS_BRIDGE_PORT", 8799),
        help="temporary bridge port (default 8799)",
    )
    parser.add_argument(
        "--relay-port",
        type=int,
        default=_env_int("MUSE_TOOLS_RELAY_PORT", 7891),
        help="controlled CONNECT relay port (default 7891)",
    )
    parser.add_argument(
        "--control-port",
        type=int,
        default=_env_int("MUSE_TOOLS_CONTROL_PORT", 7892),
        help="relay kill-switch port (default 7892)",
    )
    args = parser.parse_args()
    for value in (args.bridge_port, args.relay_port, args.control_port):
        if not 1 <= value <= 65535:
            parser.error("端口必须在 1..65535 之间")
    return asyncio.run(
        run(
            bridge_port=args.bridge_port,
            relay_port=args.relay_port,
            control_port=args.control_port,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())