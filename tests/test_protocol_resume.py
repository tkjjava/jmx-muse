r"""protocol_client 的断线重连 + 订阅 resume 离线契约测试。

证据等级（与 `muse_bridge.protocol_client.RESUME_EVIDENCE` 对齐，别把三者混读）：

  * `after_stream_seq` / `after_chat_event_seq` 这两个字段名，以及"传已消费事件
    的单调最大 seq"这种做法：**前端静态锚点**（打包 chunk
    `D:\deep seek\work\muse-bundles\24thn7in_976_.js` 里的 `Z`/`M`/`T` 三个函数），
    不是真机抓包；
  * "服务端从该 seq 之后（严格大于）回放"：**推断**（前端传回已应用事件的
    最大 seq，相当于说"这条别再发了"；社区实现文档同语义）；
  * "服务端接受这两个字段"与"断线后真的补发断开期间的事件"：**未证实** ——
    真机订阅实测过的 body 只有 `{}`，本项目也没在真机制造过断线。

所以本文件断言的是**客户端在两种边界语义下都成立的性质**：
已消费事件不重复（本地按 seq 裁剪）、尾部收尾事件不丢、重连有上限、
关闭期间不重连。它不宣称"真机上服务端一定会 resume"。

覆盖分组：
  R-01 重连后已消费事件不重复（服务端按"包含游标"重放也挡住）
  R-02 断线后的尾部收尾事件（`agent.status` 多条件）仍能到达
  R-03 重连次数/退避上限：超限抛错（带原断线原因），不无限重试
  R-04 `close()` 期间不重连
  R-05 resume 请求体形状 + 前端 `Z` 夹取 + 初始订阅保持实测 `{}`
  R-06 resume 游标 = 已消费事件 seq 的单调最大值（含 `_seek_to_end` 跳过部分）
  R-07 服务端拒 resume 字段 → 两段式降级（普通订阅 + 本地去重）
  R-08 重连后的任务/句柄回收（旧 ws 关闭、旧 receiver/心跳结束、close() 归零）
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from muse_bridge import noise, proto
from muse_bridge.protocol_client import (
    RESUME_EVIDENCE,
    HatchProtocolClient,
    ProtocolError,
)

COOKIES = {"hatch_sess": "sess-value", "hatch_vml": "vml-value"}
SITE = "https://muse.example"
TOKEN_BODY = {"token": "auth-token"}

# Agent Teams：本文件只做离线测试，禁止真机 / 代理 / Chrome（那是 Lead 的事）。
# 注入点仍然是那两件：http_client（httpx.MockTransport）与 ws_opener（假 WS）。


# ---------------------------------------------------------------- HTTP 假件


def session_json(**overrides):
    body = {
        "status": "assigned",
        "vm_id": "vm-1",
        "vm_state": "RUNNING",
        "endpoint_url": "https://vm-1.example",
        "cvm_eligibility": {"state": "NOT_ELIGIBLE"},
    }
    body.update(overrides)
    return body


def http_json(payload, *, status=200):
    return httpx.Response(status, json=payload)


class ScriptedHTTP:
    """按 (method, path) 出队预设响应；单项会被重复使用（重连要再走一遍）。"""

    def __init__(self, routes):
        self.routes = {key: list(value) for key, value in dict(routes).items()}
        self.requests = []

    def __call__(self, request):
        key = (request.method, request.url.path)
        self.requests.append(key)
        queue = self.routes.get(key)
        if not queue:
            raise AssertionError(f"未预设响应：{key}")
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    @property
    def paths(self):
        return [path for _method, path in self.requests]


# ---------------------------------------------------------------- 帧与 NDJSON


def wire(stream_id, **oneof):
    """一条明文 WS 帧：NoiseTransportFrame{payload=ServiceResponse{ServiceFrame}}。"""
    frame = proto.ServiceFrame(stream_id=stream_id, **oneof)
    service = proto.ServiceResponse(payload=frame.encode())
    return proto.NoiseTransportFrame(
        chunk_id=1, chunk_index=0, total_chunks=1, payload=service.encode()
    ).encode()


def reply(*, status=200, body=b"", end_body=True, headers=()):
    response = proto.ApplicationResponse(
        status=status, headers=tuple(headers), body=body, end_body=end_body
    )
    return [{"response": response}]


def json_reply(payload, *, status=200, end_body=True):
    return reply(
        status=status,
        body=json.dumps(payload, ensure_ascii=False).encode(),
        end_body=end_body,
    )


class Responder:
    """按请求 path 返回预设帧列表（与 test_protocol_client 同一套路）。"""

    def __init__(self, table):
        self.table = {path: list(items) for path, items in table.items()}
        self.paths = []

    def __call__(self, stream_id, request):
        self.paths.append(request.path)
        queue = self.table.get(request.path)
        if queue is None:
            raise AssertionError(f"未预设帧：{request.path}")
        return queue[0] if len(queue) == 1 else queue.pop(0)


def ndjson(*events):
    return b"".join(
        json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n"
        for event in events
    )


def ev(seq, *, text=None, activity=None, name="delta.text_append", **payload):
    """一条订阅流事件（真机形状：event/payload/seq/type）。"""
    if text is not None:
        payload.setdefault("text", text)
        payload.setdefault("message_id", "assistant-msg-1")
    if activity is not None:
        name = "agent.status"
        payload.setdefault("activity_code", activity)
    return {"event": name, "payload": payload, "seq": seq, "type": "event"}


def res_ack():
    """订阅流的第一行：`{"type":"res",...}`（服务端 subscribe 的 ack）。"""
    return {
        "id": "http-subscribe-1",
        "payload": {"subscribed": True, "stream_scope": "all"},
        "status": "ok",
        "type": "res",
    }


async def until(predicate, *, timeout=2.0, what="等待条件"):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"超时：{what}")
        await asyncio.sleep(0.01)


async def drain(stream, *, timeout=2.0):
    async def consume():
        return [event async for event in stream]

    return await asyncio.wait_for(consume(), timeout)


# ---------------------------------------------------------------- Noise 假件


def pub_of(private):
    return private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


class NoiseResponder:
    """XX pattern 的被动方，独立实现（不借 noise.NoiseXX，避免对拍自证）。"""

    def __init__(self):
        self.s = noise.Symmetric()
        self.static = X25519PrivateKey.generate()
        self.e = None
        self.re_init = None
        self.remote_static = None

    def read_msg1(self, message):
        self.re_init = message[:32]
        self.s.mix_hash(self.re_init)
        return self.s.decrypt_and_hash(message[32:])

    def write_msg2(self, payload=b""):
        self.e = X25519PrivateKey.generate()
        ephemeral = pub_of(self.e)
        self.s.mix_hash(ephemeral)
        self.s.mix_key(noise.dh(self.e, self.re_init))
        encrypted_static = self.s.encrypt_and_hash(pub_of(self.static))
        self.s.mix_key(noise.dh(self.static, self.re_init))
        return ephemeral + encrypted_static + self.s.encrypt_and_hash(payload)

    def read_msg3(self, message):
        self.remote_static = self.s.decrypt_and_hash(message[:48])
        self.s.mix_key(noise.dh(self.e, self.remote_static))
        return self.s.decrypt_and_hash(message[48:])

    def split(self):
        first, second = self.s.split()
        return second, first


_FAIL = object()  # 塞进 inbox 的哨兵：让阻塞中的 recv() 抛"连接被断"


class NoiseWebSocket:
    """真握手 / 真加解密的假 WS，可被测试主动"掐断"（模拟服务端关连接）。"""

    def __init__(self, respond=None):
        self.respond = respond
        self._responder = NoiseResponder()
        self._inbox = asyncio.Queue()
        self.frames = []
        self.closed = False
        self.phase = 0
        self._send_cipher = None
        self._recv_cipher = None

    async def send(self, data):
        raw = bytes(data)
        if self.phase == 0:
            self._responder.read_msg1(raw)
            await self._inbox.put(self._responder.write_msg2(b"server-challenge"))
            self.phase = 1
        elif self.phase == 1:
            self._responder.read_msg3(raw)
            self._send_cipher, self._recv_cipher = self._responder.split()
            self.phase = 2
        else:
            transport = proto.NoiseTransportFrame.decode(
                self._recv_cipher.dec(b"", raw)
            )
            service = proto.ServiceRequest.decode(transport.payload)
            frame = proto.ServiceFrame.decode(service.payload)
            self.frames.append(frame)
            if self.respond is not None and frame.request is not None:
                for oneof in self.respond(frame.stream_id, frame.request) or ():
                    plain = wire(frame.stream_id, **oneof)
                    await self._inbox.put(self._send_cipher.enc(b"", plain))

    async def recv(self):
        item = await self._inbox.get()
        if item is _FAIL:
            raise ConnectionResetError("模拟服务端把 WS 关掉")
        return item

    async def close(self):
        self.closed = True

    # ---- 测试专用 ----
    def die(self):
        """模拟断线：正在等 recv() 的接收循环下一次拿到哨兵就抛异常。"""
        self._inbox.put_nowait(_FAIL)

    def push(self, stream_id, body: bytes):
        """把一段 NDJSON body_chunk 塞给接收循环（已加密）。"""
        plain = wire(stream_id, body_chunk=proto.BodyChunk(data=body))
        self._inbox.put_nowait(self._send_cipher.enc(b"", plain))


class SequenceOpener:
    """按序发 WS 的 opener；`fail_after` 之后一律抛（模拟重连握手失败）。"""

    def __init__(self, sockets, *, fail_after=None):
        self.sockets = list(sockets)
        self.fail_after = fail_after
        self.calls = []

    async def __call__(self, url, **kwargs):
        index = len(self.calls)
        self.calls.append((url, kwargs))
        if self.fail_after is not None and index >= self.fail_after:
            raise ConnectionError("模拟重连握手失败")
        return self.sockets.pop(0)


@contextlib.asynccontextmanager
async def hatched(script, *, opener, **client_kwargs):
    async with script.client() as http:
        client = HatchProtocolClient(
            COOKIES, site=SITE, http_client=http, ws_opener=opener, **client_kwargs
        )
        try:
            yield client
        finally:
            await client.close()


# ---------------------------------------------------------------- 明文旅（看请求体）


class PlainWebSocket:
    """无加密假 WS（同 test_protocol_client 的 PipeWebSocket 套路）。

    只用于"看请求体 / 看游标"这类不需要真 Noise 的断言。
    """

    def __init__(self, table):
        self.table = {path: list(items) for path, items in table.items()}
        self.inbox = asyncio.Queue()
        self.frames = []
        self.closed = False

    async def send(self, data):
        transport = proto.NoiseTransportFrame.decode(bytes(data))
        service = proto.ServiceRequest.decode(transport.payload)
        frame = proto.ServiceFrame.decode(service.payload)
        self.frames.append(frame)
        if frame.request is not None:
            for oneof in self.table.get(frame.request.path, ()):
                await self.inbox.put(wire(frame.stream_id, **oneof))

    async def recv(self):
        return await self.inbox.get()

    async def close(self):
        self.closed = True

    def push(self, stream_id, body: bytes):
        plain = wire(stream_id, body_chunk=proto.BodyChunk(data=body))
        self.inbox.put_nowait(plain)


@contextlib.asynccontextmanager
async def plain_client(table):
    client = HatchProtocolClient(COOKIES, site=SITE)
    ws = PlainWebSocket(table)
    client.ws = ws
    client.send_cipher = client.recv_cipher = noise.Cipher()
    client._handshake_done = True
    client._ever_connected = True
    client.receiver_task = asyncio.create_task(client._receiver())
    try:
        yield client, ws
    finally:
        await client.close()


def subscribe_frames(ws):
    return [
        frame.request
        for frame in ws.frames
        if frame.request is not None and frame.request.path == "/chat/subscribe"
    ]


def body_of(request) -> dict:
    return json.loads(request.body)


# ---------------------------------------------------------------- 常用脚本


def chat_table(*, subscribe=None):
    """一次正常回合需要的三条路径（首连与重连都用同一份）。"""
    return {
        "/client/register-capabilities": [json_reply({"ok": True})],
        "/chat/subscribe": subscribe or [reply(status=200, end_body=False)],
        "/chat/stream": [json_reply({"session_id": "s-1"})],
    }


async def consume(client, events, *, timeout=3.0):
    """引擎姿势：读一回合，把事件攒进 events（异常原样冒泡给测试）。"""
    async for event in client.read_stream(
        "hi", node_id=client.node_id, timeout=timeout
    ):
        events.append(event)


# ---------------------------------------------------------------- R-05 请求体


async def test_initial_subscribe_body_stays_measured_empty_object():
    """初始订阅必须还是真机实测的 `{}`（resume 字段只在重连时加）。

    真机 2026-11-11 的 200 就是拿 `{}` 换来的（§1.6 第 4 条）；把未验证过的
    resume 字段塞进**每一次**订阅，等于让所有正常回合都承担一次未证实风险。
    """
    async with plain_client({"/chat/subscribe": reply(end_body=False)}) as (
        client,
        ws,
    ):
        await client.subscribe()
        request = subscribe_frames(ws)[-1]
        assert body_of(request) == {}
        assert request.end_body is True, "end_body=False 会让服务端永不回响应头"


async def test_resume_subscribe_body_uses_static_anchor_fields_and_clamps():
    """resume body 只加静态锚点里的两个字段，数值按前端 `Z` 夹取。

    锚点：`function Z(e){return Number.isFinite(e)?Math.max(0,Math.trunc(e)):0}`
    （chunk 24thn7in_976_.js）。所以负数/非数一律发 0，不是自己发明输入。
    """
    async with plain_client({"/chat/subscribe": reply(end_body=False)}) as (
        client,
        ws,
    ):
        await client.subscribe()
        await client.subscribe(after_stream_seq=17, after_chat_event_seq=5)
        assert body_of(subscribe_frames(ws)[-1]) == {
            "after_stream_seq": 17,
            "after_chat_event_seq": 5,
        }

        for bad in (-3, float("inf"), "9"):
            await client.subscribe(after_stream_seq=bad, after_chat_event_seq=bad)
            assert body_of(subscribe_frames(ws)[-1]) == {
                "after_stream_seq": 0,
                "after_chat_event_seq": 0,
            }
        # 不给主游标 = 不是 resume 调用：保持实测的 `{}` 形态（`None` 不能当 0 发）。
        await client.subscribe(after_stream_seq=None, after_chat_event_seq=5)
        assert body_of(subscribe_frames(ws)[-1]) == {}


async def test_resume_evidence_levels_stay_recorded():
    """证据等级是交付物的一部分：不许被"清理注释"顺手删掉/改成已证实。

    2026-11-11 11:2x 真机断线实验后，两条"未证实"升级为"真机实测"——所以这里
    改为分别钉住**新的**证据等级与实验路径，而不是继续断言旧结论（旧结论已作废，
    `server_accepts_resume_fields` / `server_replays_after_disconnect` 两键仍必须
    存在，且必须写明实测证据与仍属推断的边界）。
    """
    assert set(RESUME_EVIDENCE) == {
        "after_stream_seq",
        "after_chat_event_seq",
        "server_accepts_resume_fields",
        "server_replays_after_disconnect",
    }
    assert "前端静态锚点" in RESUME_EVIDENCE["after_stream_seq"]
    assert "推断" in RESUME_EVIDENCE["after_stream_seq"]
    # 断线实验后：接受/补发已是真机实测，证据路径必须留在常量里。
    assert "真机实测" in RESUME_EVIDENCE["server_accepts_resume_fields"]
    assert "resume-live2" in RESUME_EVIDENCE["server_accepts_resume_fields"]
    assert "真机实测" in RESUME_EVIDENCE["server_replays_after_disconnect"]
    # 但游标边界仍属推断，不许把"边界已验证"顺手写进去。
    assert "推断" in RESUME_EVIDENCE["server_replays_after_disconnect"]


# ---------------------------------------------------------------- R-06 游标


async def test_resume_cursor_is_monotonic_max_of_consumed_events():
    """游标 = 已消费事件 seq 的单调最大值（前端 `Math.max` 语义），只增不减。"""
    async with plain_client({"/chat/subscribe": reply(end_body=False)}) as (
        client,
        ws,
    ):
        stream_id = await client.subscribe()
        ws.push(stream_id, ndjson(ev(3, name="noop"), ev(9, name="noop")))
        ws.push(stream_id, ndjson(ev(7, name="noop")))
        events = await drain(client.read_stream(timeout=0.05))
        assert [event["seq"] for event in events] == [3, 9, 7]
        assert client._last_seen_seq == 9, "最大 seq 是 9，不能被后到的 7 回退"

        # `_seek_to_end` 跳过的尾巴也算已消费：游标必须跟上，否则重连会要求
        # 服务端补发一堆本回合根本不要的历史。
        ws.push(stream_id, ndjson(ev(12, name="noop")))
        await until(
            lambda: bytes(client.responses[stream_id].body).count(b"\n") == 4,
            what="跳尾事件进 body",
        )
        client._seek_to_end(stream_id)
        assert client._last_seen_seq == 12
        assert client._scan_offset == len(client.responses[stream_id].body)


# ---------------------------------------------------------------- R-01/R-02/R-08


async def test_reconnect_mid_turn_resumes_without_duplicating_text():
    """断线 → 重连 → 带 seq resume：正文不重复，尾部收尾事件照旧到达。

    服务端在这里被故意写成"按包含游标重放"（把已消费的 seq=2 又发了一遍）：
    客户端必须靠本地 seq 裁剪挡住它。语义边界未真机验证，所以两边都得防。
    """
    script = ScriptedHTTP(
        {
            ("GET", "/api/session"): [http_json(session_json())],
            ("POST", "/api/hatch/token"): [http_json(TOKEN_BODY)],
        }
    )
    ws1 = NoiseWebSocket(Responder(chat_table()))
    ws2 = NoiseWebSocket(Responder(chat_table()))
    opener = SequenceOpener([ws1, ws2])
    events = []

    async with hatched(script, opener=opener, reconnect_backoff=0.0) as client:
        await client.connect()
        first_ws = client.ws
        old_receiver = client.receiver_task
        old_heartbeat = client.heartbeat_task

        task = asyncio.create_task(consume(client, events))
        await until(lambda: client._sub_stream_id is not None, what="订阅建立")
        first_id = client._sub_stream_id
        ws1.push(first_id, ndjson(res_ack(), ev(1, name="delta.message_start"),
                                 ev(2, text="上半")))
        await until(
            lambda: any(event.get("text") == "上半" for event in events),
            what="断线前的正文",
        )

        # 服务端/代理把 WS 关掉（接收循环下一次 recv 抛异常）。
        ws1.die()
        await until(
            lambda: len(opener.calls) == 2
            and client._sub_stream_id not in (None, first_id),
            what="重连并重订阅",
        )
        second_id = client._sub_stream_id

        # 重放里的 seq=2 是**已消费**事件（客户端必须丢弃）；3/4 才是尾部。
        ws2.push(second_id, ndjson(
            res_ack(),
            ev(2, text="上半"),
            ev(3, text="下半"),
            ev(4, activity="idle"),
        ))
        await asyncio.wait_for(task, 2.0)

        texts = [event["text"] for event in events if event.get("kind") == "delta"]
        assert texts == ["上半", "下半"], "已消费的正文不得被重放成第二份"
        # 尾部收尾事件必须到达（多条件收尾靠它，不靠 delta.message_done）。
        assert any(
            event.get("name") == "agent.status"
            and event["payload"].get("activity_code") == "idle"
            for event in events
        ), "断线后的收尾事件不得丢失"
        # 重订阅流自己的 `res` ack 不再吐第二次（本回合只有首连那一个）。
        assert len([e for e in events if e.get("name") == "res"]) == 1
        # 重连请求带的是断线前已消费的最大 seq（=2）。
        resume_body = body_of(subscribe_frames(ws2)[-1])
        assert resume_body["after_stream_seq"] == 2
        assert resume_body["after_chat_event_seq"] == 0, (
            "真机事件样本里没有 chat_event_seq；没见过就只能报 0（未证实）"
        )
        # 旧连接与旧任务必须已经收掉：否则会出现两个接收循环抢同一个 ws。
        assert ws1.closed
        assert old_receiver is not None and old_receiver.done()
        assert old_heartbeat is not None and old_heartbeat.done()
        assert client.receiver_task is not old_receiver
        assert client.receiver_task is not None and not client.receiver_task.done()
        assert client.heartbeat_task is not None and not client.heartbeat_task.done()
        assert client.ws is not first_ws
        await client.close()
        assert client.ws is None and ws2.closed
        assert client.receiver_task is None and client.heartbeat_task is None
        assert client._sub_stream_id is None
        assert client._last_seen_seq == 0, "关闭后重连状态归零"


async def test_idle_disconnect_before_next_turn_reconnects_before_sending():
    """上一回合结束后断线：下一回合先修连接，再发消息（不重发 /chat/stream）。"""
    script = ScriptedHTTP(
        {
            ("GET", "/api/session"): [http_json(session_json())],
            ("POST", "/api/hatch/token"): [http_json(TOKEN_BODY)],
        }
    )
    ws1 = NoiseWebSocket(Responder(chat_table()))
    ws2 = NoiseWebSocket(Responder(chat_table()))
    opener = SequenceOpener([ws1, ws2])
    events = []

    async with hatched(script, opener=opener, reconnect_backoff=0.0) as client:
        await client.connect()
        # 第一回合：只读一条就走（模拟调用方读到需要的部分就收手），订阅留着。
        stream = client.read_stream("first", node_id=client.node_id, timeout=1.0)
        reader = asyncio.create_task(anext(stream))
        await until(lambda: client._sub_stream_id is not None, what="首回合订阅")
        first_id = client._sub_stream_id
        ws1.push(first_id, ndjson(res_ack(), ev(1, name="noop")))
        await asyncio.wait_for(reader, 2.0)
        await stream.aclose()

        # 空闲期断线。
        ws1.die()
        await until(lambda: client._disconnect_reason is not None, what="断线被记录")

        task = asyncio.create_task(consume(client, events))
        await until(
            lambda: len(opener.calls) == 2
            and client._sub_stream_id not in (None, first_id),
            what="新回合前完成重连",
        )
        second_id = client._sub_stream_id
        assert body_of(subscribe_frames(ws2)[-1])["after_stream_seq"] == 1, (
            "空闲期断线时游标就是上一回合已消费的最大 seq"
        )
        ws2.push(second_id, ndjson(ev(5, text="新回合"), ev(6, activity="idle")))
        await asyncio.wait_for(task, 2.0)

        texts = [event["text"] for event in events if event.get("kind") == "delta"]
        assert texts == ["新回合"]
        paths = [frame.request.path for frame in ws2.frames if frame.request]
        assert paths == [
            "/client/register-capabilities",
            "/chat/subscribe",
            "/chat/stream",
        ], "修连接后仍要先注册再订阅，最后才发消息"


# ---------------------------------------------------------------- R-03 上限


async def test_reconnect_limit_raises_instead_of_retrying_forever():
    """重连有上限：超限抛错，且消息里带原断线原因与最后一次失败原因。

    无限重试最糟的形态是把上层 deadline 烧完，然后给出一个比"连不上"更难诊断
    的超时——所以这里宁可抛一条能直接读的协议错误。
    """
    script = ScriptedHTTP(
        {
            ("GET", "/api/session"): [http_json(session_json())],
            ("POST", "/api/hatch/token"): [http_json(TOKEN_BODY)],
        }
    )
    ws1 = NoiseWebSocket(Responder(chat_table()))
    opener = SequenceOpener([ws1], fail_after=1)  # 首连成功，之后握手必失败
    events = []

    async with hatched(
        script, opener=opener, reconnect_backoff=0.0, max_reconnects=2
    ) as client:
        await client.connect()
        task = asyncio.create_task(consume(client, events))
        await until(lambda: client._sub_stream_id is not None, what="订阅建立")
        ws1.push(
            client._sub_stream_id,
            ndjson(res_ack(), ev(1, text="甲")),
        )
        await until(
            lambda: any(event.get("text") == "甲" for event in events), what="正文"
        )
        ws1.die()

        with pytest.raises(ProtocolError) as caught:
            await asyncio.wait_for(task, 2.0)
        message = str(caught.value)
        assert "断线重连失败（已尝试 2/2 次）" in message
        assert "Noise 接收失败" in message, "原断线原因必须带出来"
        assert "Noise WebSocket 连接失败" in message, "最后一次失败原因必须带出来"
        assert len(opener.calls) == 3, "1 次首连 + 2 次上限内的重试"

        # 放弃之后不应该再挂着假装能用的状态。
        assert client.ws is None


async def test_reconnect_disabled_raises_immediately():
    """关掉重连（排障/对照用）时断线就报错，不偷偷重试。"""
    script = ScriptedHTTP(
        {
            ("GET", "/api/session"): [http_json(session_json())],
            ("POST", "/api/hatch/token"): [http_json(TOKEN_BODY)],
        }
    )
    ws1 = NoiseWebSocket(Responder(chat_table()))
    opener = SequenceOpener([ws1])
    events = []

    async with hatched(
        script, opener=opener, reconnect_enabled=False, reconnect_backoff=0.0
    ) as client:
        await client.connect()
        task = asyncio.create_task(consume(client, events))
        await until(lambda: client._sub_stream_id is not None, what="订阅建立")
        ws1.push(client._sub_stream_id, ndjson(res_ack(), ev(1, text="甲")))
        await until(
            lambda: any(event.get("text") == "甲" for event in events), what="正文"
        )
        ws1.die()
        with pytest.raises(ProtocolError, match="连接已断开且未启用重连"):
            await asyncio.wait_for(task, 2.0)
        assert len(opener.calls) == 1


# ---------------------------------------------------------------- R-04 关闭


async def test_close_during_reconnect_never_opens_another_connection():
    """close() 期间不重连：正在退避的重连看到 `closing` 就收手，迭代器正常结束。"""
    script = ScriptedHTTP(
        {
            ("GET", "/api/session"): [http_json(session_json())],
            ("POST", "/api/hatch/token"): [http_json(TOKEN_BODY)],
        }
    )
    ws1 = NoiseWebSocket(Responder(chat_table()))
    ws2 = NoiseWebSocket(Responder(chat_table()))
    opener = SequenceOpener([ws1, ws2])
    events = []

    async with hatched(
        script, opener=opener, reconnect_backoff=0.2
    ) as client:
        await client.connect()
        task = asyncio.create_task(consume(client, events))
        await until(lambda: client._sub_stream_id is not None, what="订阅建立")
        ws1.push(client._sub_stream_id, ndjson(res_ack(), ev(1, text="甲")))
        await until(
            lambda: any(event.get("text") == "甲" for event in events), what="正文"
        )
        ws1.die()
        await until(lambda: client._disconnect_reason is not None, what="断线")
        await client.close()
        await asyncio.wait_for(task, 2.0)  # 正常结束，不抛

        assert len(opener.calls) == 1, "closing 之后不得再建连接"
        assert client.ws is None
        assert client.receiver_task is None and client.heartbeat_task is None
        assert client._sub_stream_id is None
        assert client.responses == {}
        assert ws1.closed and not ws2.closed


async def test_connect_after_close_is_refused():
    """关闭是终态：close() 之后再 connect() 必须报错，而不是悄悄新建连接。"""
    client = HatchProtocolClient(COOKIES, site=SITE)
    await client.close()
    with pytest.raises(ProtocolError, match="客户端已关闭"):
        await client.connect()


# ---------------------------------------------------------------- R-07 两段式降级


async def test_rejected_resume_fields_degrade_to_plain_subscribe():
    """服务端不认 resume 字段时退回实测过的 `{}` 订阅 + 本地 seq 去重。

    真机只验证过 body `{}` 会 200；万一带字段被 400，整条重连不能跟着死——
    降级后仍然靠本地裁剪保证"不重复正文"，且尾部事件照收。
    """
    script = ScriptedHTTP(
        {
            ("GET", "/api/session"): [http_json(session_json())],
            ("POST", "/api/hatch/token"): [http_json(TOKEN_BODY)],
        }
    )
    ws1 = NoiseWebSocket(Responder(chat_table()))
    ws2 = NoiseWebSocket(
        Responder(
            chat_table(
                subscribe=[
                    reply(status=400, body=b'{"error":"invalid_params"}'),
                    reply(status=200, end_body=False),
                ],
            )
        )
    )
    opener = SequenceOpener([ws1, ws2])
    events = []

    async with hatched(script, opener=opener, reconnect_backoff=0.0) as client:
        await client.connect()
        task = asyncio.create_task(consume(client, events))
        await until(lambda: client._sub_stream_id is not None, what="订阅建立")
        first_id = client._sub_stream_id
        ws1.push(first_id, ndjson(res_ack(), ev(2, text="上半")))
        await until(
            lambda: any(event.get("text") == "上半" for event in events), what="正文"
        )
        ws1.die()
        await until(
            lambda: len(opener.calls) == 2
            and client._sub_stream_id not in (None, first_id),
            what="降级重连完成",
        )
        second_id = client._sub_stream_id
        resumes = subscribe_frames(ws2)
        assert len(resumes) == 2, "第一次带字段被拒，第二次退回普通订阅"
        assert body_of(resumes[0])["after_stream_seq"] == 2
        assert body_of(resumes[1]) == {}

        # 普通订阅会把历史重放回来：已消费的 seq=2 必须被本地挡住。
        ws2.push(second_id, ndjson(
            res_ack(),
            ev(2, text="上半"),
            ev(3, text="下半"),
            ev(4, activity="idle"),
        ))
        await asyncio.wait_for(task, 2.0)
        texts = [event["text"] for event in events if event.get("kind") == "delta"]
        assert texts == ["上半", "下半"], "降级路径同样不得重复正文"
        assert any(
            event.get("name") == "agent.status"
            and event["payload"].get("activity_code") == "idle"
            for event in events
        )


# ---------------------------------------------------------------- 引擎契约


async def test_engine_collect_chat_completes_across_reconnect():
    """引擎层契约不变：断线重连后 `collect_chat` 仍拿到完整正文并正常收尾。

    这条把客户端改动与 `protocol_engine._run` 的收尾契约（"收到正文 + 流结束
    即完成"）钉在一起：重连是客户端内部的事，引擎不该看到漏掉或重复的正文。
    """
    from muse_bridge.protocol_engine import ProtocolEngine

    script = ScriptedHTTP(
        {
            ("GET", "/api/session"): [http_json(session_json())],
            ("POST", "/api/hatch/token"): [http_json(TOKEN_BODY)],
        }
    )
    ws1 = NoiseWebSocket(Responder(chat_table()))
    ws2 = NoiseWebSocket(Responder(chat_table()))
    opener = SequenceOpener([ws1, ws2])

    async with hatched(script, opener=opener, reconnect_backoff=0.0) as client:
        await client.connect()
        engine = ProtocolEngine(client)
        reader = asyncio.create_task(engine.collect_chat("9+9", timeout=3.0))

        await until(lambda: client._sub_stream_id is not None, what="订阅建立")
        first_id = client._sub_stream_id
        ws1.push(first_id, ndjson(ev(1, text="9+9"), ev(2, text="等于18。")))
        await until(lambda: client._last_seen_seq == 2, what="正文已消费")
        ws1.die()

        await until(
            lambda: len(opener.calls) == 2
            and client._sub_stream_id not in (None, first_id),
            what="重连并重订阅",
        )
        second_id = client._sub_stream_id
        ws2.push(second_id, ndjson(
            ev(2, text="等于18。"),  # 包含游标的重复事件：必须被裁掉
            ev(3, activity="online"),
        ))
        text, media = await asyncio.wait_for(reader, 2.0)
        assert text == "9+9等于18。", "重连不得让引擎看到重复或缺失的正文"
        assert media == []
        assert client._last_seen_seq == 3
