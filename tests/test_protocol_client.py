r"""protocol_client.py 的离线契约测试：假 HTTP（httpx.MockTransport）＋ 假 WebSocket。

为什么这样钉：本模块真正的契约不是"能连上 muse.ai"（CI 里没有账号、也不该连外网），
而是三阶段 bootstrap 的字段要求、CVM 硬停（绝不回退浏览器）、HTTP 状态分型，
以及 Noise/protobuf 之上的 HTTP 语义（帧分块、NDJSON 增量扫描、fs 分页与回滚）。
这些都能用两个注入点（``http_client`` / ``ws_opener``）在完全离线的条件下复现：
HTTP 走 httpx.MockTransport，WS 用一个自己完成 Noise XX 被动方的假对象
（被动方独立实现，避免"两边用同一套代码、一起错还全绿"）。

覆盖分组（对应 task-1 必测清单）：
  C-01 connect() 三阶段：/api/session → /api/hatch/vm/wake → /api/hatch/token
  C-02 每阶段缺字段必须炸，且消息点名缺哪个字段；status != assigned 也必须炸
  C-03 CVM 硬停：delegation. notary / confidential vm_type / cvm_eligibility 非 NOT_ELIGIBLE
       —— 抛错且假 WS 打开次数为 0（协议层没有"偷偷回退浏览器"这条路径）
  C-04 401→ProtocolAuthError、403→ProtocolEgressError、429→ProtocolRateLimited
  C-05 read_stream：半行 / 一行 / 多行三次到达，事件条数与顺序正确；坏行跳过但不杀流
  C-06 Reset 帧：response.reset 非 None 时抛 ProtocolError，code 进消息
  C-07 fs_write 失败回滚：/fs/write 成功但 /fs/stat 失败 → 必须调 /fs/delete 再抛错
  C-08 心跳：先等满 heartbeat_interval（默认 30s）再 ping，单次失败不终止循环
  C-09 WS 目标来源：**反过来了** —— 2026-11-11 真机打通用的是常量
       `hatch.metaaivm.com` + `/v1/noise`；`/api/session` 的 endpoint_url 只作记录
       （其 per-VM 子域 TLS 不通：SSLV3_ALERT_HANDSHAKE_FAILURE）。endpoint 派生
       host 保留为默认关闭的备选开关 `use_endpoint_ws_host`。
  C-10 真机契约：register 只发 client_id（字符串 capabilities 会 400）、
       subscribe 必须 end_body=True + `{}`、delta.text_append.payload.text 的增量
       消费与多条件收尾、以及非 200 时异常里带服务端 body 片段。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from muse_bridge import noise, proto, protocol_client
from muse_bridge.protocol_client import (
    DEFAULT_CAPABILITIES,
    DISPLAY_NAME,
    NOISE_WS_HOST,
    NOISE_WS_PATH,
    PLATFORM,
    TIMEZONE,
    ApplicationResponse,
    BodyChunk,
    HatchProtocolClient,
    ProtocolAuthError,
    ProtocolEgressError,
    ProtocolError,
    ProtocolRateLimited,
    ProtocolResponse,
    Reset,
    collect_media_candidates,
    normalize_media_path,
    resource_kind,
)
from muse_bridge.session_http import (
    EgressBlocked,
    RateLimited,
    SessionExpired,
    SessionUnconfirmed,
)

COOKIES = {"hatch_sess": "sess-value", "hatch_vml": "vml-value"}
SITE = "https://muse.example"
TOKEN_BODY = {"token": "auth-token"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


# ---------------------------------------------------------------- HTTP 假件


def session_json(**overrides):
    """一份 assigned 的成功 /api/session 体；override 成 None 用来制造"缺字段"。"""
    body = {
        "status": "assigned",
        "vm_id": "vm-1",
        "vm_state": "RUNNING",
        "endpoint_url": "https://vm-1.example",
        "cvm_eligibility": {"state": "NOT_ELIGIBLE"},
    }
    body.update(overrides)
    return body


def http_json(payload, *, status=200, headers=None):
    return httpx.Response(status, json=payload, headers=dict(headers or {}))


def http_text(text, *, status=200, headers=None):
    return httpx.Response(status, text=text, headers=dict(headers or {}))


def http_empty(*, status=200, headers=None):
    return httpx.Response(status, content=b"", headers=dict(headers or {}))


class ScriptedHTTP:
    """按 (method, path) 出队预设响应；程序化记录收到的每个请求。

    末项响应会被重复使用（例如 wake 之后反复确认 /api/session 的场景），
    这样测试可以精确断言"到底发了哪几个请求、顺序是什么"。
    """

    def __init__(self, routes):
        self.routes = {key: list(value) for key, value in dict(routes).items()}
        self.requests = []

    def __call__(self, request):
        key = (request.method, request.url.path)
        self.requests.append((request.method, request.url.path, request))
        queue = self.routes.get(key)
        if not queue:
            raise AssertionError(f"未预设响应：{key}")
        if isinstance(queue[0], Exception):
            raise queue[0]
        outcome = queue.pop(0) if len(queue) > 1 else queue[0]
        return outcome

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    @property
    def paths(self):
        return [path for _method, path, _request in self.requests]


# ---------------------------------------------------------------- WS 假件


def wire(stream_id, **oneof):
    """一条明文 WS 帧：NoiseTransportFrame{payload=ServiceResponse{ServiceFrame}}。

    假 WS 直接免加密（客户端被注入 Cipher() 的明文直通形态），
    这样断言的是帧结构与 HTTP 语义，而 Noise 层本身由 test_noise.py 对拍。
    """
    frame = proto.ServiceFrame(stream_id=stream_id, **oneof)
    service = proto.ServiceResponse(payload=frame.encode())
    return proto.NoiseTransportFrame(
        chunk_id=1, chunk_index=0, total_chunks=1, payload=service.encode()
    ).encode()


def reply(*, status=200, body=b"", end_body=True, headers=()):
    """一条 ApplicationResponse 帧（包成 Responder 可返回的 oneof kwargs 列表）。"""
    response = ApplicationResponse(
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
    """按请求 path 返回预设帧列表；每个请求的帧由 oneof kwargs 描述。"""

    def __init__(self, table):
        self.table = {path: list(items) for path, items in table.items()}
        self.paths = []

    def __call__(self, stream_id, request):
        self.paths.append(request.path)
        queue = self.table.get(request.path)
        if queue is None:
            raise AssertionError(f"未预设帧：{request.path}")
        return queue[0] if len(queue) == 1 else queue.pop(0)


class PipeWebSocket:
    """无加密假 WS：把客户端帧解成 ServiceRequest，交给 respond 决定回什么。"""

    def __init__(self, respond=None):
        self.respond = respond
        self.inbox = asyncio.Queue()
        self.frames = []
        self.closed = False

    async def send(self, data):
        raw = bytes(data)
        transport = proto.NoiseTransportFrame.decode(raw)
        service = proto.ServiceRequest.decode(transport.payload)
        frame = proto.ServiceFrame.decode(service.payload)
        self.frames.append(frame)
        if self.respond is not None and frame.request is not None:
            for oneof in self.respond(frame.stream_id, frame.request) or ():
                await self.inbox.put(wire(frame.stream_id, **oneof))

    async def recv(self):
        return await self.inbox.get()

    async def close(self):
        self.closed = True


@contextlib.asynccontextmanager
async def pipe(respond=None):
    """一个"已经连上"的客户端：注入明文 cipher + 假 WS + 正在跑的接收循环。"""
    client = HatchProtocolClient(COOKIES, site=SITE)
    ws = PipeWebSocket(respond)
    client.ws = ws
    client.send_cipher = client.recv_cipher = noise.Cipher()
    client._handshake_done = True
    client.receiver_task = asyncio.create_task(client._receiver())
    try:
        yield client, ws
    finally:
        await client.close()


def request_json(request):
    return json.loads(request.body)


async def until(predicate, *, timeout=2.0, what="等待条件"):
    """小工具：等假接收循环把帧消化完（只用于测试自身的同步，不测时间语义）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"超时：{what}")
        await asyncio.sleep(0.01)


def ndjson(*events):
    """把事件 dict 拼成订阅流的 NDJSON body（一行一条，尾随换行）。"""
    return b"".join(
        json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n"
        for event in events
    )


async def drain(stream, *, timeout=2.0):
    """读完一个异步事件流（带上限：实现 bug 不该让测试挂死）。"""

    async def consume():
        return [event async for event in stream]

    return await asyncio.wait_for(consume(), timeout)


# ---------------------------------------------------------------- Noise 握手假件


def pub_of(private):
    return private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


class NoiseResponder:
    """按 XX pattern 独立实现的被动方（不借 noise.NoiseXX，避免对拍自证）。"""

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


class NoiseWebSocket:
    """真握手、真加解密的假 WS：先走完 Noise XX，再接 HTTP-over-Noise 帧。"""

    def __init__(self, respond=None):
        self._responder = NoiseResponder()
        self._inbox = asyncio.Queue()
        self.frames = []
        self.closed = False
        self.phase = 0
        self.next_msg2 = None
        self._send_cipher = None
        self._recv_cipher = None

    async def send(self, data):
        raw = bytes(data)
        if self.phase == 0:
            self._responder.read_msg1(raw)
            msg2 = self.next_msg2 or self._responder.write_msg2(b"server-challenge")
            await self._inbox.put(msg2)
            self.phase = 1
        elif self.phase == 1:
            self._responder.read_msg3(raw)
            self._send_cipher, self._recv_cipher = self._responder.split()
            self.phase = 2
        else:
            transport = proto.NoiseTransportFrame.decode(self._recv_cipher.dec(b"", raw))
            service = proto.ServiceRequest.decode(transport.payload)
            frame = proto.ServiceFrame.decode(service.payload)
            self.frames.append(frame)
            if self.respond is not None and frame.request is not None:
                for oneof in self.respond(frame.stream_id, frame.request) or ():
                    plain = wire(frame.stream_id, **oneof)
                    await self._inbox.put(self._send_cipher.enc(b"", plain))

    async def recv(self):
        return await self._inbox.get()

    async def close(self):
        self.closed = True


class WSOpener:
    """记录 url/kwargs 的异步 opener（等价于 websockets.connect 的签名面）。"""

    def __init__(self, factory):
        self.factory = factory
        self.calls = []

    async def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.factory()


class FailingOpener:
    def __init__(self, exc):
        self.exc = exc
        self.calls = []

    async def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        raise self.exc


@contextlib.asynccontextmanager
async def hatched(script, *, opener=None, **client_kwargs):
    async with script.client() as http:
        client = HatchProtocolClient(
            COOKIES, site=SITE, http_client=http, ws_opener=opener, **client_kwargs
        )
        try:
            yield client
        finally:
            await client.close()


# ---------------------------------------------------------------- 异常分型


def test_protocol_error_status_codes_are_app_mapping_contract():
    """协议层异常自带 status_code，app 层据此映射 HTTP 状态。

    这四个数是 app.py 与协议层之间的接口；改掉会让"cookie 失效"被当成 502 重试。
    """
    assert ProtocolError.status_code == 502
    assert ProtocolAuthError.status_code == 401
    assert ProtocolEgressError.status_code == 403
    assert ProtocolRateLimited.status_code == 429
    assert issubclass(ProtocolAuthError, ProtocolError)
    assert issubclass(ProtocolEgressError, ProtocolError)
    assert issubclass(ProtocolRateLimited, ProtocolError)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (SessionExpired("cookie 坏了"), ProtocolAuthError),
        (EgressBlocked("出口被拒"), ProtocolEgressError),
        (RateLimited("稍后重试"), ProtocolRateLimited),
        (SessionUnconfirmed("状态未确认"), ProtocolError),
    ],
)
def test_map_session_error_keeps_classification(exc, expected):
    """session_http 的四类失败必须一对一分型，不能归并成通用 ProtocolError。"""
    mapped = protocol_client._map_session_error(exc)
    assert type(mapped) is expected
    assert mapped.status_code == expected.status_code


def test_map_session_error_labels_unconfirmed_as_workspace_not_assigned():
    """规格点名：非 assigned / 缺 endpoint_url 归到 "workspace not assigned"。"""
    mapped = protocol_client._map_session_error(SessionUnconfirmed("status=pending"))
    assert type(mapped) is ProtocolError
    assert str(mapped).startswith("workspace not assigned")
    assert "status=pending" in str(mapped)


@pytest.mark.parametrize(
    ("code", "exc_type", "needle"),
    [
        (401, ProtocolAuthError, "cookie"),
        (403, ProtocolEgressError, "出口"),
        (429, ProtocolRateLimited, "限流"),
        (500, ProtocolError, "HTTP 500"),
        (302, ProtocolError, "HTTP 302"),
    ],
)
def test_classify_token_status_pins_http_family(code, exc_type, needle):
    """token 端点的状态分型：401/403/429 处置完全不同（重登 / 换出口 / 退避）。"""
    with pytest.raises(exc_type) as caught:
        protocol_client._classify_status(code, what="/api/hatch/token")
    assert needle in str(caught.value)
    if exc_type is ProtocolError:
        assert caught.value.status_code == 502
    else:
        assert caught.value.status_code == code


def test_classify_token_status_accepts_200():
    assert protocol_client._classify_status(200, what="/api/hatch/token") is None


# ---------------------------------------------------------------- 媒体路径与候选


def test_normalize_media_path_accepts_sandbox_and_workspace_only():
    """只认 sandbox:// 与 workspace/；其它形态一律返回 None（不猜路径）。"""
    assert normalize_media_path("sandbox://workspace/a.png") == (
        "sandbox://workspace/a.png",
        "workspace/a.png",
    )
    assert normalize_media_path("workspace/a.png") == (
        "sandbox://workspace/a.png",
        "workspace/a.png",
    )
    for value in ("https://cdn/x.png", "/tmp/x.png", "workspace", "", 123, None):
        assert normalize_media_path(value) is None


def test_normalize_media_path_excludes_user_upload_area():
    """workspace/user/ 是输入区；把它当产物会"回显"用户上传的图（上游踩过）。"""
    assert normalize_media_path("workspace/user/ref.png") is None


def test_resource_kind_prefers_mime_then_extension():
    assert resource_kind({"mime": "video/mp4"}) == "video"
    assert resource_kind({"mime": "image/png"}) == "image"
    assert resource_kind({"path": "workspace/a.mp4"}) == "video"
    assert resource_kind({"path": "workspace/a.png"}) == "image"
    assert resource_kind({}) == "image"


def test_collect_media_candidates_dedupes_and_merges_metadata():
    """presentation 信封里 metadata 可能是 JSON 字符串；同一路径只收一次。

    这两条都是线上观测形态：不合并 JSON 字符串会丢掉宽高/类型，不去重会让
    多轮补发把同一张图收成多份。
    """
    payload = {
        "resources": [
            {"path": "workspace/gen/a.png", "mime": "image/png"},
            {"path": "workspace/gen/a.png", "mime": "image/png"},
            {
                "metadata": json.dumps(
                    {"path": "workspace/gen/b.png", "mime_type": "image/png", "width": 10}
                )
            },
            {"path": "workspace/user/upload.png"},
        ]
    }
    found = collect_media_candidates(payload)
    assert [item["path"] for item in found] == [
        "sandbox://workspace/gen/a.png",
        "sandbox://workspace/gen/b.png",
    ]
    assert found[0]["kind"] == "image"
    assert found[1]["mime"] == "image/png"
    assert found[1]["width"] == 10


def test_collect_media_candidates_skips_request_and_inline_payloads():
    """request / data_base64 / metadata 子树不参与递归：它们不是可取的成品路径。

    metadata 只作为"当前资源自身的附加信息"被合并（见上一个测试），
    不会再被当成子树重新遍历——否则一张图的 metadata 里嵌个路径就会被收成两份。
    """
    payload = {
        "request": {"path": "workspace/gen/inner.png"},
        "data_base64": "AAAA",
        "resources": [
            {
                "path": "workspace/gen/real.png",
                "mime": "image/png",
                "metadata": {"path": "workspace/gen/shadow.png"},
            }
        ],
    }
    found = collect_media_candidates(payload)
    assert [item["path"] for item in found] == ["sandbox://workspace/gen/real.png"]


# ---------------------------------------------------------------- 路径与文件片段


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("a b?.png", "a_b_.png"),
        ("-_.png", "-_.png"),
        ("", "reference.bin"),
        (".hidden", "hidden"),
        ("ok_1.png", "ok_1.png"),
    ],
)
def test_safe_name_strips_traversal_and_illegal_chars(filename, expected):
    """文件名会被拼进远端路径；不压成安全片段就等于允许目录穿越。"""
    assert protocol_client._safe_name(filename) == expected


@pytest.mark.parametrize(
    ("filename", "mime", "expected"),
    [
        ("cat.png", "image/png", "workspace/user/files/cat.png"),
        ("cat", "image/png", "workspace/user/files/cat.png"),
        ("cat", "application/x-unlikely", "workspace/user/files/cat.bin"),
        ("../../etc/passwd", "image/png", "workspace/user/files/passwd.png"),
    ],
)
def test_reference_path_derives_extension_from_mime(filename, mime, expected):
    """引擎形态上传必须落在 workspace/user/files/（会话读参考素材的唯一目录）。"""
    assert HatchProtocolClient._reference_path(filename, mime) == expected


def test_require_workspace_path_rejects_uploads_unless_explicitly_allowed():
    """读路径默认排除用户上传区；写路径显式 allow_user=True 才放行。"""
    assert HatchProtocolClient._require_workspace_path("workspace/gen.png") == (
        "workspace/gen.png"
    )
    assert HatchProtocolClient._require_workspace_path("sandbox://workspace/gen.png") == (
        "workspace/gen.png"
    )
    assert HatchProtocolClient._require_workspace_path(
        "workspace/user/x.png", allow_user=True
    ) == "workspace/user/x.png"
    for path, needle in (
        ("workspace/user/x.png", "用户上传区"),
        ("/tmp/x.png", "workspace/"),
        ("", "路径为空"),
        (None, "路径为空"),
    ):
        with pytest.raises(ProtocolError, match=needle):
            HatchProtocolClient._require_workspace_path(path)


def test_decode_fs_chunk_validates_declared_length():
    """fs/read 每页都要按服务端声明的 len 校验实收字节数。

    长度不符往往意味着分页参数被上游改写，放过去会拿到截断文件：
    渲染成半张图，失败点离原因很远。
    """
    good = {"data_base64": base64.b64encode(b"abc").decode(), "len": 3}
    assert protocol_client._decode_fs_chunk(good) == b"abc"
    assert protocol_client._decode_fs_chunk({"data_base64": "YWJj"}) == b"abc"

    for payload, needle in (
        ({"len": 3}, "缺少 data_base64"),
        ({"data_base64": "!!!!", "len": 4}, "无法解码"),
        ({"data_base64": "YWJj", "len": 4}, "长度不符"),
        ({"data_base64": "YWJj", "len": "abc"}, "不是整数"),
    ):
        with pytest.raises(ProtocolError, match=needle):
            protocol_client._decode_fs_chunk(payload)


# ---------------------------------------------------------------- 代理


@pytest.mark.parametrize(
    ("proxy", "expected"),
    [
        (None, None),
        ({}, None),
        ({"server": ""}, None),
        # requests 风格的两键形状。**必须返回 None**：本客户端的契约是
        # `{"server": ...}`。2026-11-11 排障踩过——探针传这个形状，
        # `_proxy_url()` 取不到 `server` 静默返回 None，于是 HTTP 与 WS
        # 双双直连，表现为 ConnectionResetError，被误判成"上游故障"。
        # 钉住它，是为了让"形状错"永远停在测试这一层，而不是变成线上假象。
        ({"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}, None),
        ({"server": "proxy.example:8080"}, "http://proxy.example:8080"),
        (
            {"server": "proxy.example:8080", "username": "u"},
            "http://u@proxy.example:8080",
        ),
        (
            {"server": "proxy.example:8080", "username": "u", "password": "p"},
            "http://u:p@proxy.example:8080",
        ),
        (
            {"server": "socks5://proxy.example:1080", "username": "u", "password": "p"},
            "socks5://u:p@proxy.example:1080",
        ),
        (
            {"server": "http://u:p@proxy.example:8080", "username": "x"},
            "http://u:p@proxy.example:8080",
        ),
    ],
)
def test_proxy_url_forms(proxy, expected):
    """代理串带凭据，只交给 httpx；形态错会让连接直接失败且很难从日志看出原因。"""
    client = HatchProtocolClient(COOKIES, proxy=proxy)
    assert client._proxy_url() == expected


@pytest.mark.asyncio
async def test_ws_opener_receives_proxy_and_user_agent():
    """WS 升级必须真的带上代理与 UA。

    为什么单独钉住：`connect()` 的失败会被压成一句
    `Noise WebSocket 连接失败（ConnectionResetError）`，**代理没被用上**
    和**代理节点坏了**在这句话里长得一模一样。只能在 opener 这一层
    回读 kwargs 才分得开——2026-11-11 的误判就出在这里。
    """
    captured: dict = {}

    async def opener(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        raise ProtocolError("stop here")

    client = HatchProtocolClient(
        COOKIES,
        proxy={"server": "http://127.0.0.1:7890"},
        user_agent="UA/1.0",
        ws_opener=opener,
    )
    client._ws_url = "wss://hatch.example/v1/noise?x=1"
    with pytest.raises(ProtocolError):
        await client._open_noise_ws("hatch_vm")

    assert captured["kwargs"]["proxy"] == "http://127.0.0.1:7890"
    assert captured["kwargs"]["user_agent"] == "UA/1.0"


@pytest.mark.asyncio
async def test_ws_opener_omits_proxy_when_not_configured():
    """没配代理时不得凭空塞一个：`proxy=None` 在 websockets 里有额外语义。"""
    captured: dict = {}

    async def opener(url, **kwargs):
        captured["kwargs"] = kwargs
        raise ProtocolError("stop here")

    client = HatchProtocolClient(COOKIES, ws_opener=opener, user_agent=None)
    client._ws_url = "wss://hatch.example/v1/noise"
    with pytest.raises(ProtocolError):
        await client._open_noise_ws("hatch_vm")

    assert "proxy" not in captured["kwargs"]
    assert "user_agent" not in captured["kwargs"]


# ---------------------------------------------------------------- 请求封装


def test_build_headers_has_protocol_defaults_and_camel_case_override():
    """请求头是协议面的一部分；调用方同名覆盖必须"替换"而不是"追加"。
    重复的 x-request-id 会让服务端把两条请求关联到同一个日志 id。
    """
    client = HatchProtocolClient(COOKIES, model="hatch-web")
    headers = client._build_headers({"X-Request-ID": "fixed"}, has_body=True)
    keys = [item.key for item in headers]
    assert "x-app-id" in keys
    assert "Accept-Language" in keys
    assert "Accept-Encoding" in keys
    assert "Content-Type" in keys
    assert sum(1 for key in keys if key.lower() == "x-request-id") == 1
    values = {item.key: item.value for item in headers}
    assert values["X-Request-ID"] == "fixed"
    assert values["x-app-id"] == "hatch-web"

    without_body = client._build_headers(None, has_body=False)
    assert "Content-Type" not in [item.key for item in without_body]


def test_pack_chunks_oversized_message_and_reassembles():
    """MAX_PLAINTEXT_FRAME 是硬约束：超一帧服务端直接断连而不是报错。

    这里只验证分块形状与可逆性（chunk_id 一致、序号连续、拼回原字节）。
    """
    client = HatchProtocolClient(COOKIES)
    client.send_cipher = noise.Cipher()
    payload = b"x" * 70000
    message = proto.ServiceRequest(
        service=proto.ServiceType.DAEMON, payload=payload
    )
    blobs = client._pack(message)
    assert len(blobs) == 2
    frames = [proto.NoiseTransportFrame.decode(blob) for blob in blobs]
    assert [frame.chunk_index for frame in frames] == [0, 1]
    assert [frame.total_chunks for frame in frames] == [2, 2]
    assert len({frame.chunk_id for frame in frames}) == 1
    assert all(len(frame.payload) <= noise.MAX_PLAINTEXT_FRAME for frame in frames)
    assert b"".join(frame.payload for frame in frames) == message.encode()


def test_pack_empty_message_yields_single_empty_chunk():
    client = HatchProtocolClient(COOKIES)
    client.send_cipher = noise.Cipher()
    message = proto.ServiceRequest(service=proto.ServiceType.DAEMON, payload=b"")
    blobs = client._pack(message)
    assert len(blobs) == 1
    frame = proto.NoiseTransportFrame.decode(blobs[0])
    assert frame.total_chunks == 1
    assert frame.payload == b""


def test_frame_kind_supports_proto_kind_and_protobuf_whichoneof():
    """`_frame_kind` 允许换成 protoc 生成类：兼容分支不能悄悄删掉。"""
    kinds = {
        "request": proto.ServiceFrame(stream_id=1, request=proto.ApplicationRequest()),
        "response": proto.ServiceFrame(
            stream_id=1, response=ApplicationResponse(status=200)
        ),
        "body_chunk": proto.ServiceFrame(stream_id=1, body_chunk=BodyChunk(data=b"a")),
        "reset": proto.ServiceFrame(stream_id=1, reset=Reset()),
    }
    for expected, frame in kinds.items():
        assert protocol_client.HatchProtocolClient._frame_kind(frame) == expected

    class Generated:
        request = None
        response = object()

        @staticmethod
        def WhichOneof(name):
            return "response"

    assert protocol_client.HatchProtocolClient._frame_kind(Generated()) == "response"
    assert protocol_client.HatchProtocolClient._frame_kind(object()) == ""


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (b'{"event":"e","seq":1}', {"event": "e", "seq": 1, "name": "e", "payload": {}}),
        (b'{"type":"t"}', {"type": "t", "name": "t", "payload": {}}),
        (b'{"event":123}', {"event": 123, "name": "123", "payload": {}}),
        (b'{"event":"e","payload":{"x":1}}', {"event": "e", "payload": {"x": 1}, "name": "e"}),
    ],
)
def test_parse_event_keeps_origin_keys_and_adds_name(line, expected):
    """下游按 event/type/payload/seq 读事件；额外补 name 让调用方不必关心先后。"""
    assert protocol_client.HatchProtocolClient._parse_event(line) == expected


@pytest.mark.parametrize(
    "line", [b"", b"   ", b"not-json", b"[1,2]", b'"string"', b"null"]
)
def test_parse_event_skips_unusable_lines(line):
    """坏行返回 None 而不是抛错：单行损坏不能杀掉整条事件流。"""
    assert protocol_client.HatchProtocolClient._parse_event(line) is None


def test_trim_response_drops_consumed_prefix():
    """长连接 body 会无限增长；超过阈值后必须丢弃已消费前缀，否则一个号跑一天吃光内存。"""
    client = HatchProtocolClient(COOKIES)
    client.responses[3] = ProtocolResponse(body=bytearray(b"0123456789abcdef"))
    client._scan_offset = 10
    client._trim_response(3, 10)
    assert bytes(client.responses[3].body) == b"abcdef"
    assert client._scan_offset == 0
    client._trim_response(3, 0)  # no-op
    client._trim_response(99, 5)  # 不存在的流：不炸
    assert bytes(client.responses[3].body) == b"abcdef"


# ---------------------------------------------------------------- connect：三阶段


async def test_connect_bootstraps_and_opens_noise_when_vm_already_running():
    """三阶段顺序 + WS URL 查询参数 + 握手后的状态全部钉住。

    VM 本来就 RUNNING 时 wake_ok 必须是 False：没发过 wake 就不能声称"唤醒成功"，
    否则运营会以为真做过一次唤醒动作。
    """
    script = ScriptedHTTP(
        {
            ("GET", "/api/session"): [http_json(session_json())],
            ("POST", "/api/hatch/token"): [http_json(TOKEN_BODY)],
        }
    )
    ws = NoiseWebSocket()
    opener = WSOpener(lambda: ws)
    async with hatched(script, opener=opener) as client:
        await client.connect()
        assert client.connected
        assert client.ws is ws
        assert client.vm_id == "vm-1"
        assert client.vm_state == "RUNNING"
        assert client.auth_token == "auth-token"
        assert client.notary_token is None
        assert client.wake_ok is False
        assert script.paths == ["/api/session", "/api/hatch/token"]
        assert client.heartbeat_task is not None
        assert not client.heartbeat_task.done()

        url, kwargs = opener.calls[0]
        # 夹具的 endpoint_url 是 https（非 WS 形态），但**无论 endpoint 是什么**，
        # 默认路径都只认常量 LB host（真机唯一握手成功的形态，见 C-09 那一组）。
        assert url.startswith(f"wss://{NOISE_WS_HOST}{NOISE_WS_PATH}?")
        query = dict(parse_qsl(urlsplit(url).query))
        assert query["vm_id"] == "vm-1"
        assert query["auth_token"] == "auth-token"
        assert query["app_id"] == client.model
        assert len(query["request_id"]) == 36
        assert "notary_token" not in query
        assert kwargs["origin"] == SITE
        assert kwargs["max_size"] == 4 * 1024 * 1024
        assert kwargs["open_timeout"] == 30.0
        assert kwargs["close_timeout"] == 3.0
        assert "Accept-Language" in kwargs["additional_headers"]
        assert "proxy" not in kwargs
        assert "user_agent" not in kwargs
        assert client.ws_url == url
    assert ws.closed, "close() 必须把 WS 关掉，否则进程退出时留下悬空连接"


async def test_connect_wakes_vm_then_confirms_running_before_handshake():
    """wake 的 HTTP 200 只是"受理"；必须再读 /api/session 确认 RUNNING 才算成功。"""
    script = ScriptedHTTP(
        {
            ("GET", "/api/session"): [
                http_json(session_json(vm_state="BOOTING")),
                http_json(session_json()),
            ],
            ("POST", "/api/hatch/vm/wake"): [http_json({"ok": True})],
            ("POST", "/api/hatch/token"): [http_json(TOKEN_BODY)],
        }
    )
    ws = NoiseWebSocket()
    opener = WSOpener(lambda: ws)
    async with hatched(script, opener=opener) as client:
        await client.connect()
        assert client.wake_ok is True
        assert client.vm_state == "RUNNING"
        assert client.connected
        assert script.paths == [
            "/api/session",
            "/api/hatch/vm/wake",
            "/api/session",
            "/api/hatch/token",
        ]
        wake_request = script.requests[1][2]
        body = json.loads(wake_request.content)
        assert body["vm_id"] == "vm-1"
        assert body["retry_count"] == 0
        assert len(body["connect_attempt_id"]) == 36


async def test_unconfirmed_wake_never_reaches_handshake():
    """wake 受理了但确认不到 RUNNING 时必须直接失败。

    在一条起不来的 VM 上继续握手，症状会变成"随机超时"，比明确报错难诊断一个量级。
    """
    script = ScriptedHTTP(
        {
            ("GET", "/api/session"): [http_json(session_json(vm_state="BOOTING"))],
            ("POST", "/api/hatch/vm/wake"): [http_json({"ok": True})],
        }
    )
    opener = WSOpener(NoiseWebSocket)
    async with hatched(script, opener=opener) as client:
        with pytest.raises(ProtocolError, match="未确认 RUNNING") as caught:
            await client.connect()
        assert "vm_state=BOOTING" in str(caught.value)
        assert client.wake_ok is False
        assert opener.calls == [], "确认失败后不得偷偷开 WS 继续握手"
        assert client.connected is False


async def test_wake_rejected_with_500_is_protocol_error_not_wake_ok():
    """非 401/403/429 的 wake 失败不算异常升级，但绝不能报"唤醒成功"。"""
    script = ScriptedHTTP(
        {
            ("GET", "/api/session"): [http_json(session_json(vm_state="BOOTING"))],
            ("POST", "/api/hatch/vm/wake"): [http_empty(status=500)],
        }
    )
    opener = WSOpener(NoiseWebSocket)
    async with hatched(script, opener=opener) as client:
        with pytest.raises(ProtocolError, match="wake 未被受理"):
            await client.connect()
        assert client.wake_ok is False
        assert opener.calls == []


async def test_session_missing_vm_id_names_the_field():
    """/api/session 已 assigned 但没 vm_id：必须炸，且消息里点名 vm_id。"""
    script = ScriptedHTTP(
        {("GET", "/api/session"): [http_json(session_json(vm_id=None))]}
    )
    opener = WSOpener(NoiseWebSocket)
    async with hatched(script, opener=opener) as client:
        with pytest.raises(ProtocolError) as caught:
            await client.connect()
        assert "vm_id" in str(caught.value)
        assert opener.calls == []
        assert script.paths == ["/api/session"]


async def test_session_non_running_vm_state_wakes_then_names_vm_state():
    """vm_state 不是 RUNNING 会触发 wake；wake 后确认不到就必须点名 vm_state。

    注意 vm_state 完全缺失（None）的路径见报告：当前 connect 既不 wake 也不在
    最终 assigned 检查里补 vm_state，会把未知状态当成可连（与 RenewResult.connectable
    的四条件不一致），这一条无法在不改产品代码的前提下写成绿测。
    """
    script = ScriptedHTTP(
        {
            ("GET", "/api/session"): [http_json(session_json(vm_state="STARTING"))],
            ("POST", "/api/hatch/vm/wake"): [http_json({"ok": True})],
        }
    )
    opener = WSOpener(NoiseWebSocket)
    async with hatched(script, opener=opener) as client:
        with pytest.raises(ProtocolError) as caught:
            await client.connect()
        assert "vm_state" in str(caught.value)
        assert "未确认 RUNNING" in str(caught.value)
        assert "STARTING" in str(caught.value)
        assert opener.calls == []


async def test_session_missing_endpoint_url_is_workspace_not_assigned():
    """缺 endpoint_url 时即便 vm_state 是 RUNNING 也判不可连。

    网关还没下发接入点时直连只会 404；"看起来能连然后超时"比明确报错难诊断。
    """
    script = ScriptedHTTP(
        {("GET", "/api/session"): [http_json(session_json(endpoint_url=None))]}
    )
    opener = WSOpener(NoiseWebSocket)
    async with hatched(script, opener=opener) as client:
        with pytest.raises(ProtocolError, match="workspace not assigned") as caught:
            await client.connect()
        assert "endpoint_url" in str(caught.value)
        assert opener.calls == []
        assert script.paths == ["/api/session"]


async def test_session_status_not_assigned_names_status():
    """status != assigned 必须炸；消息里要带实际 status，否则运营无法判断账号状态。"""
    script = ScriptedHTTP(
        {("GET", "/api/session"): [http_json(session_json(status="pending"))]}
    )
    opener = WSOpener(NoiseWebSocket)
    async with hatched(script, opener=opener) as client:
        with pytest.raises(ProtocolError, match="workspace not assigned") as caught:
            await client.connect()
        assert "status=pending" in str(caught.value)
        assert opener.calls == []
        assert script.paths == ["/api/session"]


async def test_notary_token_is_forwarded_in_ws_query():
    """标准 hatch_vm 的 notary_token 必须原样带进 WS 查询串（对端要它做背书）。"""
    script = ScriptedHTTP(
        {
            ("GET", "/api/session"): [http_json(session_json())],
            ("POST", "/api/hatch/token"): [
                http_json({"token": "auth-token", "notary_token": "notary.ok"})
            ],
        }
    )
    ws = NoiseWebSocket()
    opener = WSOpener(lambda: ws)
    async with hatched(script, opener=opener) as client:
        await client.connect()
        assert client.notary_token == "notary.ok"
        query = dict(parse_qsl(urlsplit(client.ws_url).query))
        assert query["notary_token"] == "notary.ok"


# ---------------------------------------------------------------- WS 目标：常量优先
# 2026-11-11 真机（本机 mihomo 代理 127.0.0.1:18000 → Meta 的 LB）：能握手的是
# `wss://hatch.metaaivm.com/v1/noise?...`（TLS+WS 升级 101 → Noise XX 成功 →
# connect() 约 3.3s）。同一代理下 `/api/session` 下发的
# `wss://<vm_id>.metaaivm.com/` 这个 per-VM 子域 TLS 直接失败
# （`ssl.SSLError: [SSL: SSLV3_ALERT_HANDSHAKE_FAILURE]`）。
#
# 所以这一组把旧断言**整组反转**：旧版钉的是"endpoint_url 派生 host 为主、常量只
# 回退"，真机推翻了它 —— 常量是默认路径，endpoint 派生降级为默认关闭的备选。


def test_ws_url_uses_lb_constant_even_when_endpoint_is_wss():
    """endpoint 是 `wss://abc-123.metaaivm.com/` 时，host/path 仍是常量。

    旧实现会拿这个子域做 host；真机证明该子域 TLS 不通，只有 LB 能握手。
    顺带钉住 query 四件套与 request_id 的透传（纯函数不自己生成 id）。
    """
    url = protocol_client.ws_url_from_endpoint(
        "wss://abc-123.metaaivm.com/",
        vm_id="vm-1",
        auth_token="auth-token",
        app_id="hatch-web",
        request_id="req-1",
    )
    parts = urlsplit(url)
    assert parts.scheme == "wss"
    assert parts.hostname == NOISE_WS_HOST
    assert parts.path == NOISE_WS_PATH
    # 五件套：浏览器建连恒定带 app_version=1（docs/NOISE_BUNDLE_NOTES.md）。
    assert dict(parse_qsl(parts.query)) == {
        "vm_id": "vm-1",
        "auth_token": "auth-token",
        "app_id": "hatch-web",
        "app_version": "1",
        "request_id": "req-1",
    }


def test_ws_url_endpoint_derivation_is_opt_in_escape_hatch():
    """`use_endpoint_host=True` 才从 endpoint 派 host/path —— 默认必须关闭。

    保留这条路径只为未来 LB 切换时开一次开关就能验证，免得把"只验证过一种来源"
    的结构写死。它**没有**真机握手证据（那个子域 TLS 就不通），所以默认 False；
    打开时 scheme 强制 `wss`（`ws` 原样透传等于让 auth_token 走明文），
    endpoint 自带 path 原样保留（含尾斜杠），空或 `/` 才补默认。
    """
    derived = protocol_client.ws_url_from_endpoint(
        "wss://abc-123.metaaivm.com/",
        vm_id="vm-1",
        auth_token="t",
        app_id="hatch-web",
        request_id="req-1",
        use_endpoint_host=True,
    )
    parts = urlsplit(derived)
    assert parts.hostname == "abc-123.metaaivm.com"
    assert parts.path == NOISE_WS_PATH

    quoted = protocol_client.ws_url_from_endpoint(
        "wss://host/some/path/",
        vm_id="vm-1",
        auth_token="t",
        app_id="hatch-web",
        request_id="req-1",
        use_endpoint_host=True,
    )
    parts = urlsplit(quoted)
    assert parts.hostname == "host"
    assert parts.path == "/some/path/"

    plain = protocol_client.ws_url_from_endpoint(
        "ws://host/some/path",
        vm_id="vm-1",
        auth_token="t",
        app_id="hatch-web",
        request_id="req-1",
        use_endpoint_host=True,
    )
    assert urlsplit(plain).scheme == "wss"
    assert urlsplit(plain).path == "/some/path"


@pytest.mark.parametrize(
    "endpoint",
    [None, "", "   ", "http://vm-1.example", "https://vm-1.example", "not-a-url",
     "wss://", 123],
)
def test_ws_url_default_target_is_constant_for_every_endpoint(endpoint):
    """不管 endpoint 是什么（缺失/空/http(s)/坏串），默认目标都是常量 LB host。

    旧版把这条描述成"回退"，真机之后它是**默认路径**：行为没变，语义变了。
    注释必须写清楚，否则下一个人会以为常量只是兜底、又去"修"成 endpoint 优先。
    """
    url = protocol_client.ws_url_from_endpoint(
        endpoint,
        vm_id="vm-1",
        auth_token="t",
        app_id="hatch-web",
        request_id="req-1",
    )
    parts = urlsplit(url)
    assert parts.scheme == "wss"
    assert parts.hostname == NOISE_WS_HOST
    assert parts.path == NOISE_WS_PATH
    assert dict(parse_qsl(parts.query))["vm_id"] == "vm-1"


def test_ws_url_includes_notary_token_only_when_present():
    """notary 缺省时查询串里没有该键（与现状一致）；空串同样不带。

    空 `notary_token=` 会被对端当成"带了但无效"，比不带更难诊断。
    """
    without = protocol_client.ws_url_from_endpoint(
        None, vm_id="vm-1", auth_token="t", app_id="hatch-web",
        request_id="req-1",
    )
    assert "notary_token" not in dict(parse_qsl(urlsplit(without).query))

    with_notary = protocol_client.ws_url_from_endpoint(
        None, vm_id="vm-1", auth_token="t", app_id="hatch-web",
        request_id="req-1", notary="notary.ok",
    )
    assert dict(parse_qsl(urlsplit(with_notary).query))["notary_token"] == "notary.ok"


async def test_connect_targets_lb_host_and_keeps_endpoint_url_recorded():
    """connect() 必须真的把常量喂给 opener，同时仍记录 endpoint_url。

    只测纯函数会漏掉"规则对了、connect 却还按 endpoint 拼 URL"这种接线错误，
    所以这里走完整 bootstrap 并断言 opener 实际收到的 URL。
    endpoint_url 仍要记录：它是 `/api/hatch/token` 的 vmAddress 参数，只是不再当 host。
    """
    endpoint = "wss://abc-123.metaaivm.com/"
    script = ScriptedHTTP(
        {
            ("GET", "/api/session"): [http_json(session_json(endpoint_url=endpoint))],
            ("POST", "/api/hatch/token"): [http_json(TOKEN_BODY)],
        }
    )
    ws = NoiseWebSocket()
    opener = WSOpener(lambda: ws)
    async with hatched(script, opener=opener) as client:
        await client.connect()
        assert client.endpoint_url == endpoint
        assert client.use_endpoint_ws_host is False, "endpoint 派生默认必须关闭"
        url, _kwargs = opener.calls[0]
        parts = urlsplit(url)
        assert parts.hostname == NOISE_WS_HOST
        assert parts.path == NOISE_WS_PATH
        query = dict(parse_qsl(parts.query))
        assert query["vm_id"] == "vm-1"
        assert query["auth_token"] == "auth-token"
        assert query["app_id"] == client.model
        assert client.ws_url == url
    assert ws.closed


async def test_connect_escape_hatch_can_use_endpoint_host_when_enabled():
    """把 `use_endpoint_ws_host` 显式置 True（备选路径）才取 endpoint 的 host。

    真机没证明这条路能握手，所以它默认关闭；这里钉住"开关确实接到了纯函数"，
    避免将来 LB 切换时打开开关却发现 connect 根本没传进去。
    """
    endpoint = "wss://abc-123.metaaivm.com/"
    script = ScriptedHTTP(
        {
            ("GET", "/api/session"): [http_json(session_json(endpoint_url=endpoint))],
            ("POST", "/api/hatch/token"): [http_json(TOKEN_BODY)],
        }
    )
    ws = NoiseWebSocket()
    opener = WSOpener(lambda: ws)
    async with hatched(script, opener=opener) as client:
        client.use_endpoint_ws_host = True
        await client.connect()
        url, _kwargs = opener.calls[0]
        assert urlsplit(url).hostname == "abc-123.metaaivm.com"
        assert client.endpoint_url == endpoint


async def test_connect_uses_constant_host_for_http_endpoint():
    """端点是 http(s) 时 connect 仍然连得上常量 LB host（不因目标不可用而变脆）。"""
    script = ScriptedHTTP(
        {
            ("GET", "/api/session"): [
                http_json(session_json(endpoint_url="http://vm-1.example"))
            ],
            ("POST", "/api/hatch/token"): [http_json(TOKEN_BODY)],
        }
    )
    ws = NoiseWebSocket()
    opener = WSOpener(lambda: ws)
    async with hatched(script, opener=opener) as client:
        await client.connect()
        assert client.endpoint_url == "http://vm-1.example"
        url, _kwargs = opener.calls[0]
        parts = urlsplit(url)
        assert parts.hostname == NOISE_WS_HOST
        assert parts.path == NOISE_WS_PATH
    assert ws.closed


@pytest.mark.parametrize(
    ("status", "exc_type", "needle"),
    [
        (401, ProtocolAuthError, "cookie"),
        (403, ProtocolEgressError, "出口"),
        (429, ProtocolRateLimited, "限流"),
    ],
)
async def test_session_http_failures_map_to_protocol_errors(status, exc_type, needle):
    """/api/session 的 401/403/429 必须各自成型：处置动作（重登/换出口/退避）完全不同。"""
    script = ScriptedHTTP({("GET", "/api/session"): [http_empty(status=status)]})
    opener = WSOpener(NoiseWebSocket)
    async with hatched(script, opener=opener) as client:
        with pytest.raises(exc_type) as caught:
            await client.connect()
        assert caught.value.status_code == status
        assert needle in str(caught.value)
        assert caught.value.__cause__ is None, "映射异常不应把底层请求细节挂在 cause 上"
        assert opener.calls == []
        assert script.paths == ["/api/session"]


@pytest.mark.parametrize(
    ("status", "exc_type"),
    [(401, ProtocolAuthError), (403, ProtocolEgressError), (429, ProtocolRateLimited)],
)
async def test_token_endpoint_failures_map_to_protocol_errors(status, exc_type):
    """/api/hatch/token 的状态分型要独立于 /api/session 那条链。"""
    script = ScriptedHTTP(
        {
            ("GET", "/api/session"): [http_json(session_json())],
            ("POST", "/api/hatch/token"): [http_empty(status=status)],
        }
    )
    opener = WSOpener(NoiseWebSocket)
    async with hatched(script, opener=opener) as client:
        with pytest.raises(exc_type) as caught:
            await client.connect()
        assert caught.value.status_code == status
        assert opener.calls == []
        assert script.paths == ["/api/session", "/api/hatch/token"]


@pytest.mark.parametrize(
    ("response", "needle"),
    [
        (http_empty(status=500), "HTTP 500"),
        (http_text("<html>login</html>"), "不是 JSON"),
        (http_json([1, 2]), "不是对象"),
    ],
)
async def test_token_payload_failures_are_plain_protocol_errors(response, needle):
    """token 返回 200 但不是可用 JSON 对象时必须报错；静默当成空 payload 会拿不到 token。"""
    script = ScriptedHTTP(
        {
            ("GET", "/api/session"): [http_json(session_json())],
            ("POST", "/api/hatch/token"): [response],
        }
    )
    opener = WSOpener(NoiseWebSocket)
    async with hatched(script, opener=opener) as client:
        with pytest.raises(ProtocolError, match=needle):
            await client.connect()
        assert opener.calls == []


async def test_token_without_auth_token_is_auth_error():
    """token 端点没给 token = 认证面失败，按 401 一类处理；不能带空 token 去握手。"""
    script = ScriptedHTTP(
        {
            ("GET", "/api/session"): [http_json(session_json())],
            ("POST", "/api/hatch/token"): [http_json({"notary_token": None})],
        }
    )
    opener = WSOpener(NoiseWebSocket)
    async with hatched(script, opener=opener) as client:
        with pytest.raises(ProtocolAuthError, match="auth token"):
            await client.connect()
        assert opener.calls == []


async def test_token_network_error_hides_proxy_credentials():
    """代理 URL 带凭据；异常消息只能留类型名，不能把凭据串进日志。"""
    script = ScriptedHTTP(
        {
            ("GET", "/api/session"): [http_json(session_json())],
            ("POST", "/api/hatch/token"): [
                httpx.ConnectError("proxy http://u:secret@p.example 拒绝")
            ],
        }
    )
    opener = WSOpener(NoiseWebSocket)
    async with hatched(
        script,
        opener=opener,
        proxy={"server": "http://p.example:8080", "username": "u", "password": "secret"},
    ) as client:
        with pytest.raises(ProtocolError) as caught:
            await client.connect()
        assert "ConnectError" in str(caught.value)
        assert "secret" not in str(caught.value)
        assert caught.value.__cause__ is None
        assert opener.calls == []


# ---------------------------------------------------------------- CVM 硬停


async def test_delegation_notary_hard_stops_without_browser_fallback():
    """delegation. 前缀是机密 VM 的背书令牌，需要 msg3 回签名 RV challenge。

    本客户端不复刻那一套，且**绝不**在失败后偷偷改用 CDP：静默降级会把
    "协议不支持"误读成"账号坏了"，还把两条失败路径合成一条无法诊断的。
    """
    script = ScriptedHTTP(
        {
            ("GET", "/api/session"): [http_json(session_json())],
            ("POST", "/api/hatch/token"): [
                http_json({"token": "auth-token", "notary_token": "delegation.abc"})
            ],
        }
    )
    opener = WSOpener(NoiseWebSocket)
    async with hatched(script, opener=opener) as client:
        with pytest.raises(ProtocolError) as caught:
            await client.connect()
        text = str(caught.value)
        assert "delegation." in text
        assert "不回退" in text
        assert opener.calls == [], "必须零 WS/浏览器回退尝试"
        assert client._handshake_done is False
        assert client.connected is False


async def test_confidential_vm_type_hard_stops_without_browser_fallback():
    """vm_type 含 confidential = 需要 attestation/recovery 证明；本客户端不支持。"""
    script = ScriptedHTTP(
        {
            ("GET", "/api/session"): [
                http_json(session_json(vm_type="confidential_vm"))
            ],
            ("POST", "/api/hatch/token"): [http_json(TOKEN_BODY)],
        }
    )
    opener = WSOpener(NoiseWebSocket)
    async with hatched(script, opener=opener) as client:
        with pytest.raises(ProtocolError) as caught:
            await client.connect()
        text = str(caught.value)
        assert "confidential" in text
        assert "不回退" in text
        assert opener.calls == []


async def test_cvm_eligibility_state_hard_stops_without_browser_fallback():
    """cvm_eligibility.state 不在 NOT_ELIGIBLE 族（""/NOT_ELIGIBLE/INELIGIBLE/...）
    就意味着账号被划进了机密 VM 池；必须显式拒，不能装作没看见。"""
    script = ScriptedHTTP(
        {
            ("GET", "/api/session"): [
                http_json(session_json(cvm_eligibility={"state": "ELIGIBLE"}))
            ],
            ("POST", "/api/hatch/token"): [http_json(TOKEN_BODY)],
        }
    )
    opener = WSOpener(NoiseWebSocket)
    async with hatched(script, opener=opener) as client:
        with pytest.raises(ProtocolError) as caught:
            await client.connect()
        assert "cvm_eligibility.state='ELIGIBLE'" in str(caught.value)
        assert "不回退" in str(caught.value)
        assert opener.calls == []


# ---------------------------------------------------------------- WS 打开 / 握手失败


async def test_ws_open_failure_becomes_protocol_error_with_type_name():
    """WS 打开失败统一成协议错误并只留异常类型名；不能让原始异常串惊动凭据。"""
    script = ScriptedHTTP(
        {
            ("GET", "/api/session"): [http_json(session_json())],
            ("POST", "/api/hatch/token"): [http_json(TOKEN_BODY)],
        }
    )
    opener = FailingOpener(httpx.ConnectError("wss://h?auth_token=t 失败"))
    async with hatched(script, opener=opener) as client:
        with pytest.raises(ProtocolError, match="Noise WebSocket 连接失败") as caught:
            await client.connect()
        assert "ConnectError" in str(caught.value)
        assert "auth_token" not in str(caught.value)
        assert client.connected is False


async def test_text_frame_during_handshake_tears_down_ws():
    """协议通道只走二进制；握手期收到文本帧说明对端不是 muse，必须报错并拆连接。"""

    class TextReplyWebSocket:
        def __init__(self):
            self.queue = asyncio.Queue()
            self.closed = False

        async def send(self, data):
            await self.queue.put("not-binary")

        async def recv(self):
            return await self.queue.get()

        async def close(self):
            self.closed = True

    ws = TextReplyWebSocket()
    script = ScriptedHTTP(
        {
            ("GET", "/api/session"): [http_json(session_json())],
            ("POST", "/api/hatch/token"): [http_json(TOKEN_BODY)],
        }
    )
    async with hatched(script, opener=WSOpener(lambda: ws)) as client:
        with pytest.raises(ProtocolError, match="文本帧"):
            await client.connect()
        assert ws.closed, "握手失败必须 teardown，不能留一条半开的连接"
        assert client.connected is False


async def test_foreign_msg2_fails_loudly_and_tears_down():
    """对端 msg2 认证失败必须抛 NoiseError 并拆连接，不能返回垃圾字节继续跑。"""
    foreign = NoiseResponder()
    foreign.read_msg1(noise.NoiseXX().msg1(noise.msg1_payload()))
    foreign_msg2 = foreign.write_msg2(b"x")
    ws = NoiseWebSocket()
    ws.next_msg2 = foreign_msg2
    script = ScriptedHTTP(
        {
            ("GET", "/api/session"): [http_json(session_json())],
            ("POST", "/api/hatch/token"): [http_json(TOKEN_BODY)],
        }
    )
    async with hatched(script, opener=WSOpener(lambda: ws)) as client:
        with pytest.raises(noise.NoiseError, match="认证失败"):
            await client.connect()
        assert ws.closed
        assert client.connected is False


async def test_connect_with_zero_heartbeat_interval_skips_heartbeat_task():
    """heartbeat_interval=0 是"关掉心跳"的显式开关；仍要建接收循环。"""
    script = ScriptedHTTP(
        {
            ("GET", "/api/session"): [http_json(session_json())],
            ("POST", "/api/hatch/token"): [http_json(TOKEN_BODY)],
        }
    )
    ws = NoiseWebSocket()
    async with hatched(script, opener=WSOpener(lambda: ws)) as client:
        client.heartbeat_interval = 0
        await client.connect()
        assert client.heartbeat_task is None
        assert client.receiver_task is not None
        assert client.connected


# ---------------------------------------------------------------- 生命周期


async def test_close_is_idempotent_and_drops_state():
    """没连过也能 close；close 之后必须不可再发请求、响应表与等待者全部清空。"""
    client = HatchProtocolClient(COOKIES)
    await client.close()
    await client.close()
    assert client.connected is False
    assert client.responses == {}
    assert client.waiters == {}


async def test_close_cancels_tasks_and_resets_flags():
    client = HatchProtocolClient(COOKIES, site=SITE)
    client.send_cipher = client.recv_cipher = noise.Cipher()
    client._handshake_done = True
    ws = PipeWebSocket()
    client.ws = ws
    client.receiver_task = asyncio.create_task(client._receiver())
    client.responses[1] = ProtocolResponse()
    await client.close()
    assert ws.closed
    assert client.receiver_task is None
    assert client.responses == {}
    assert client._handshake_done is False
    assert client.send_cipher is None


async def test_connected_property_tracks_handshake_and_closing():
    """connected 只在"握手完成 + 有 WS + 未关闭"三条同时成立时为真。"""
    async with pipe() as (client, _ws):
        assert client.connected
        client.closing = True
        assert client.connected is False


async def test_new_task_node_rotates_id_without_dropping_session():
    """事件按 node_id 归属；同一条连接复用 node_id 会把上一轮残留 delta 当成这一轮回答。"""
    async with pipe() as (client, _ws):
        client.session_id = "sess-1"
        before = client.node_id
        after = client.new_task_node()
        assert after != before
        assert client.node_id == after
        assert client.session_id == "sess-1"


async def test_ws_primitives_without_connection_raise_protocol_error():
    """收发原语的前置条件是 connect()；报错要可读，不能是 AttributeError。"""
    client = HatchProtocolClient(COOKIES)
    with pytest.raises(ProtocolError, match="尚未连接"):
        await client._ws_send(b"x")
    with pytest.raises(ProtocolError, match="尚未连接"):
        await client._ws_recv()


async def test_protocol_constants_and_capabilities_are_stable():
    """这些值是线上观测/前端 bundle 抄来的：改掉会让 register-capabilities 被路由错。"""
    assert DEFAULT_CAPABILITIES == (
        "chat_cancel",
        "delta_stream",
        "custom_reactions",
        "custom_reactions_facebook_thumbs_up_v1",
    )
    assert NOISE_WS_HOST == "hatch.metaaivm.com"
    assert NOISE_WS_PATH == "/v1/noise"
    assert PLATFORM == "web"
    assert TIMEZONE == "Asia/Shanghai"
    assert DISPLAY_NAME == "muse-bridge"
    client = HatchProtocolClient(COOKIES)
    assert client.heartbeat_interval == 30.0
    assert client.handshake_timeout == 30.0
    assert client.wake_verify_tries == 1
    assert client.capabilities == list(DEFAULT_CAPABILITIES)


# ---------------------------------------------------------------- HTTP-over-Noise 请求


async def test_request_accumulates_headers_and_body_then_releases_entry():
    """一次非流式请求结束后必须释放响应表，否则一条长连接会攒下所有历史流。"""
    responder = Responder(
        {
            "/x": [
                reply(
                    status=200,
                    headers=[proto.Header(key="a", value="b")],
                    body=b'{"ok":true}',
                )
            ]
        }
    )
    async with pipe(responder) as (client, _ws):
        response = await client.request("POST", "/x", {"a": 1})
        assert response.status == 200
        assert response.headers == [("a", "b")]
        assert response.text == '{"ok":true}'
        assert response.json() == {"ok": True}
        assert response.ended is True
        assert response.reset is None
        assert 1 not in client.responses
        assert client.next_stream == 2
        assert client.responses == {}


async def test_response_json_returns_none_for_empty_body():
    """/fs/write 之类的端点可能回空 body；json() 返回 None 而不是抛错。"""
    async with pipe() as (client, _ws):
        response = ProtocolResponse()
        assert response.json() is None
        response.body.extend(b'{"a":1}')
        assert response.json() == {"a": 1}


async def test_request_raises_on_reset_frame_with_code_and_reason():
    """Reset 帧是协议级拒绝，必须抛 ProtocolError 且 code/reason 进消息。

    只报"请求失败"会让上游 4xx 与真正的流重置混在一起，排查成本极高。
    """
    responder = Responder(
        {
            "/fail": [
                [{"reset": Reset(code=Reset.Code.REFUSED_STREAM, reason="busy")}]
            ]
        }
    )
    async with pipe(responder) as (client, _ws):
        with pytest.raises(ProtocolError, match="code=4") as caught:
            await client.request("GET", "/fail")
        assert "reason=busy" in str(caught.value)
        assert client.responses == {}


async def test_request_times_out_without_response():
    """没有响应就必须按 timeout 结束，不能无限挂住 worker。"""
    async with pipe() as (client, _ws):
        with pytest.raises(ProtocolError, match="等待 stream 1 结束超时"):
            await client.request("GET", "/never", timeout=0.05)


async def test_wait_end_reports_dropped_stream():
    async with pipe() as (client, _ws):
        with pytest.raises(ProtocolError, match="响应已被丢弃"):
            await client._wait_end(5, 0.1)


async def test_wait_headers_times_out():
    async with pipe() as (client, _ws):
        client.responses[9] = ProtocolResponse()
        with pytest.raises(ProtocolError, match="等待 stream 9 响应头超时"):
            await client._wait_headers(9, 0.05)


async def test_request_json_rejects_non_200_and_bad_json():
    """`_request_json` 是 RPC 面的统一闸门：非 200 / 非 JSON 都必须是协议错误。"""
    responder = Responder(
        {
            "/e500": [reply(status=500, body=b"{}")],
            "/html": [reply(status=200, body=b"<html>")],
        }
    )
    async with pipe(responder) as (client, _ws):
        with pytest.raises(ProtocolError, match="HTTP 500"):
            await client._request_json("GET", "/e500", None, 1.0)
        with pytest.raises(ProtocolError, match="不是 JSON"):
            await client._request_json("GET", "/html", None, 1.0)


async def test_request_json_wraps_non_object_payload():
    """JSON 数组/标量不是 dict：统一包成 {"result": value}，调用方不必再判类型。"""
    responder = Responder({"/scalar": [reply(body=b'"text"')]})
    async with pipe(responder) as (client, _ws):
        assert await client._request_json("GET", "/scalar", None, 1.0) == {
            "result": "text"
        }


async def test_request_forwards_stream_ids_and_headers():
    """stream_id 必须单调递增，调用方自定义头必须原样送达（同名覆盖只出现一次）。"""
    responder = Responder(
        {
            "/first": [reply(body=b"{}")],
            "/second": [reply(body=b"{}")],
        }
    )
    async with pipe(responder) as (client, ws):
        await client.request("GET", "/first", headers={"X-Request-ID": "fixed"})
        await client.request("GET", "/second")
        assert [frame.stream_id for frame in ws.frames] == [1, 2]
        first = ws.frames[0].request
        assert first.verb == "GET"
        assert first.path == "/first"
        keys = [item.key for item in first.headers]
        assert sum(1 for key in keys if key.lower() == "x-request-id") == 1
        assert "fixed" in [item.value for item in first.headers]
        assert ws.frames[1].request.headers != first.headers


# ---------------------------------------------------------------- 订阅与事件流


async def test_subscribe_sends_complete_empty_body_and_waits_only_headers():
    """subscribe 是永不结束的流，但**请求体必须完整发送**：end_body=True + `{}`。

    真机 2026-11-11：旧实现的 end_body=False 让服务端永远不回响应头
    （`等待 stream N 响应头超时`）；改成 end_body=True + 空 body 后立刻 200
    （content-type: application/x-ndjson）。这条把那次修复钉死在帧层面。
    """
    responder = Responder(
        {"/chat/subscribe": [reply(status=200, end_body=False)]}
    )
    async with pipe(responder) as (client, ws):
        client.session_id = "sess-x"
        stream_id = await client.subscribe()
        assert stream_id == 1
        assert client._sub_stream_id == 1
        assert client._scan_offset == 0
        frame = ws.frames[0].request
        assert frame.verb == "POST"
        assert frame.path == "/chat/subscribe"
        assert frame.end_body is True, "end_body=False 会让服务端永不回响应头"
        assert frame.body == b"{}", "实测空 body {} 就够；不要塞 capabilities 字符串数组"
        assert client.responses[1].status == 200


async def test_subscribe_forbidden_carries_server_body():
    """403 时异常里要带服务端 body：`matched route missing from endpoint ACL` 是排障唯一线索。

    真机 GET /client/register-capabilities 的 403 就长这样（ACL 只放行 POST）；
    订阅被 ACL 拦下时也要能看到同一句话，而不是只有 `chat.subscribe HTTP 403`。
    """
    forbidden = (
        b'{"error":{"code":"forbidden",'
        b'"message":"matched route missing from endpoint ACL"}}'
    )
    responder = Responder({"/chat/subscribe": [reply(status=403, body=forbidden)]})
    async with pipe(responder) as (client, _ws):
        with pytest.raises(ProtocolError, match="chat.subscribe HTTP 403") as caught:
            await client.subscribe()
        assert "endpoint ACL" in str(caught.value)
        assert client.responses == {}
        assert client._sub_stream_id is None


async def test_subscribe_http_error_is_protocol_error_and_cleans_up():
    """订阅被拒必须报错并释放响应条目；留下半截状态会让下一次 subscribe 复用错流。"""
    responder = Responder({"/chat/subscribe": [reply(status=503, body=b"{}")]})
    async with pipe(responder) as (client, _ws):
        with pytest.raises(ProtocolError, match="chat.subscribe HTTP 503"):
            await client.subscribe()
        assert client.responses == {}
        assert client._sub_stream_id is None


async def test_read_stream_requires_subscribe_first():
    """没有订阅流时 read_stream 必须立刻报错，不能空转等着。"""
    async with pipe() as (client, _ws):
        with pytest.raises(ProtocolError, match="先调用 subscribe"):
            async for _event in client.read_stream():
                pass


async def test_read_stream_scans_half_line_then_single_then_many():
    """订阅流是单条长流，事件以换行追加；一次 recv 可能是半行/一行/多行。

    按 offset 增量扫是唯一正确的读法：不等换行就吐事件会把 JSON 截断，
    每次 recv 只当整行处理则会丢掉被拆开的那条事件。
    """
    responder = Responder({"/chat/subscribe": [reply(end_body=False)]})
    events = []

    async def consume():
        async for event in client.read_stream():
            events.append(event)
            if len(events) >= 5:
                break

    async with pipe(responder) as (client, ws):
        stream_id = await client.subscribe()
        task = asyncio.create_task(consume())
        await ws.inbox.put(
            wire(
                stream_id,
                body_chunk=BodyChunk(
                    data=b'{"event":"first","seq":1}\n{"event":"sec'
                ),
            )
        )
        await until(lambda: len(events) >= 1, what="第一行事件")
        assert len(events) == 1, "半行必须等换行后才能吐出，不能截断 JSON"
        assert events[0]["event"] == "first"
        assert events[0]["name"] == "first"
        assert events[0]["payload"] == {}

        await ws.inbox.put(
            wire(stream_id, body_chunk=BodyChunk(data=b'ond","seq":2}\n'))
        )
        await until(lambda: len(events) >= 2, what="第二行事件")
        assert events[1]["event"] == "second"
        assert events[1]["seq"] == 2

        await ws.inbox.put(
            wire(
                stream_id,
                body_chunk=BodyChunk(
                    data=(
                        b'{"event":"third","seq":3}\n'
                        b'{"event":"fourth","seq":4}\n'
                    )
                ),
            )
        )
        await until(lambda: len(events) >= 4, what="一次 recv 多行")
        assert [item["event"] for item in events] == [
            "first",
            "second",
            "third",
            "fourth",
        ]

        await ws.inbox.put(
            wire(
                stream_id,
                body_chunk=BodyChunk(
                    data=b'{"broken":\n{"event":"fifth","seq":5}\n'
                ),
            )
        )
        await until(lambda: len(events) >= 5, what="坏行后仍继续")
        assert events[4]["event"] == "fifth"
        await task


async def test_read_stream_skips_bad_line_without_killing_stream():
    """坏行只跳过这一行；直接抛错会让一条长连接因为一行垃圾而整条报废。"""
    responder = Responder({"/chat/subscribe": [reply(end_body=False)]})
    async with pipe(responder) as (client, ws):
        stream_id = await client.subscribe()
        await ws.inbox.put(
            wire(
                stream_id,
                body_chunk=BodyChunk(data=b'not-json\n{"event":"alive","seq":2}\n'),
            )
        )
        stream = client.read_stream()
        try:
            event = await asyncio.wait_for(anext(stream), 2.0)
        finally:
            await stream.aclose()
        assert event["event"] == "alive"


async def test_read_stream_raises_on_subscription_reset():
    """订阅流被重置必须抛错并带 code：否则读循环会永远等一条已经死掉的流。"""
    responder = Responder({"/chat/subscribe": [reply(end_body=False)]})
    async with pipe(responder) as (client, ws):
        stream_id = await client.subscribe()
        await ws.inbox.put(
            wire(stream_id, reset=Reset(code=Reset.Code.CANCELLED, reason="cancelled"))
        )
        stream = client.read_stream()
        try:
            with pytest.raises(ProtocolError, match="code=1") as caught:
                await asyncio.wait_for(anext(stream), 2.0)
        finally:
            await stream.aclose()
        assert "cancelled" in str(caught.value)


# ---------------------------------------------------------------- 真机 NDJSON 序列
# 2026-11-11 纯协议首次打通时订阅流吐出的真实事件序列（形状逐字取自实测）：
#   res → model_lock.updated → agent.status(online) → task.status(completed/running)
#   → approvals.snapshot → message.user → agent.status(working)
#   → agent.status(responding) → delta.message_start → delta.text_append
#   → agent.status(回到 online/idle)
# 助手正文只在 `delta.text_append.payload.text`；整段回答 `9+9等于18。` 就是一条 append。

CHAT_SEQUENCE = (
    {
        "id": "http-subscribe-1",
        "payload": {
            "agent_id": "agent-1",
            "channel": "main",
            "conn_id": "conn-1",
            "is_primary": True,
            "is_thread": False,
            "session_id": "s-1",
            "stream_scope": "all",
            "subscribed": True,
        },
        "status": "ok",
        "type": "res",
    },
    {"event": "model_lock.updated", "payload": {"locked": False}, "seq": 1,
     "type": "event"},
    {"event": "agent.status", "payload": {"activity_code": "online",
     "activity_text": "online"}, "seq": 2, "type": "event"},
    # 注意：completed 出现在正文**之前**（实测就长这样），不能拿它当收尾信号。
    {"event": "task.status", "payload": {"status": "completed"}, "seq": 3,
     "type": "event"},
    {"event": "approvals.snapshot", "payload": {"pending": []}, "seq": 4,
     "type": "event"},
    {"event": "message.user", "payload": {"chat_context": {"chat_id": "s-1"},
     "display_text": "9+9"}, "seq": 5, "type": "event"},
    {"event": "agent.status", "payload": {"activity_code": "working",
     "activity_text": "working"}, "seq": 6, "type": "event"},
    {"event": "agent.status", "payload": {"activity_code": "responding",
     "activity_text": "responding"}, "seq": 7, "type": "event"},
    {"event": "delta.message_start", "payload": {"message_id": "assistant-msg-1"},
     "seq": 8, "type": "event"},
    {"event": "delta.text_append", "payload": {
        "is_thread": False,
        "message_id": "assistant-msg-1",
        "message_seq": 37,
        "parent_agent_id": "agent-1",
        "parent_message_id": "assistant-msg-1",
        "text": "9+9等于18。",
    }, "seq": 9, "type": "event"},
    {"event": "agent.status", "payload": {"activity_code": "online",
     "activity_text": "online"}, "seq": 10, "type": "event"},
)


async def test_read_stream_real_sequence_emits_text_delta_then_stops():
    """真机序列喂进去：正文以 `kind=delta` + `text` 产出，收尾后迭代器正常结束。

    这条同时证明"正文之前那条 task.status(completed) 没有把流关掉"：
    11 条事件全部吐出来了，seq 1..10 一条不缺。
    """
    responder = Responder({"/chat/subscribe": [reply(end_body=False)]})
    async with pipe(responder) as (client, ws):
        stream_id = await client.subscribe()
        body = ndjson(*CHAT_SEQUENCE)
        await ws.inbox.put(wire(stream_id, body_chunk=BodyChunk(data=body)))
        events = await drain(client.read_stream())

        assert len(events) == len(CHAT_SEQUENCE)
        assert events[0]["type"] == "res"
        assert events[0]["name"] == "res"
        assert events[0]["kind"] == "status"
        # 原文键一个都没被替换（引擎按 event/name/payload 读，res 行本来就没有 event 键）。
        assert "status" in events[0] and events[0]["payload"]["subscribed"] is True
        assert [event.get("seq") for event in events[1:]] == list(range(1, 11))

        text_events = [event for event in events if event.get("kind") == "delta"]
        assert len(text_events) == 1, "状态事件不得带 text，否则同一段回答会吐两次"
        delta = text_events[0]
        assert delta["name"] == "delta.text_append"
        assert delta["text"] == "9+9等于18。"
        assert delta["payload"]["text"] == "9+9等于18。"
        assert delta["payload"]["message_id"] == "assistant-msg-1"
        assert delta["seq"] == 9

        # message.user / delta.message_start 是状态事件，不带 text（不重复正文）。
        for name in ("message.user", "delta.message_start"):
            found = [event for event in events if event["name"] == name]
            assert found and "text" not in found[0]
            assert found[0]["kind"] == "status"

        # 收尾：最后一条是 agent.status 回到 online，迭代器已结束（drain 返回）。
        assert events[-1]["name"] == "agent.status"
        assert events[-1]["payload"]["activity_code"] == "online"


async def test_read_stream_accumulates_multiple_text_appends():
    """多次 append 按到达顺序各吐一条 delta；合起来才是整段回答。"""
    responder = Responder({"/chat/subscribe": [reply(end_body=False)]})
    async with pipe(responder) as (client, ws):
        stream_id = await client.subscribe()
        first = ndjson(
            {"event": "delta.text_append", "payload": {"text": "9+9"}, "seq": 1,
             "type": "event"}
        )
        second = ndjson(
            {"event": "delta.text_append", "payload": {"text": "等于18。"}, "seq": 2,
             "type": "event"},
            {"event": "agent.status", "payload": {"activity_code": "idle"}, "seq": 3,
             "type": "event"},
        )
        await ws.inbox.put(wire(stream_id, body_chunk=BodyChunk(data=first)))
        await ws.inbox.put(wire(stream_id, body_chunk=BodyChunk(data=second)))
        events = await drain(client.read_stream())
        texts = [event["text"] for event in events if event.get("kind") == "delta"]
        assert texts == ["9+9", "等于18。"]
        assert "".join(texts) == "9+9等于18。"


async def test_read_stream_does_not_end_on_status_before_any_text():
    """正文之前的 online/completed 状态**不能**结束回合（会丢掉整段回答）。

    实测序列里 `agent.status(online)` 与 `task.status(completed)` 都排在正文前面，
    所以收尾条件必须要求"已经收到过正文"。这条用两段 body 钉住：
    先只喂状态（不能结束），再喂正文 + 收尾状态（这时才结束）。
    """
    responder = Responder({"/chat/subscribe": [reply(end_body=False)]})
    pre = ndjson(
        {"event": "task.status", "payload": {"status": "completed"}, "seq": 1,
         "type": "event"},
        {"event": "agent.status", "payload": {"activity_code": "online"}, "seq": 2,
         "type": "event"},
    )
    post = ndjson(
        {"event": "delta.text_append", "payload": {"text": "回答"}, "seq": 3,
         "type": "event"},
        {"event": "agent.status", "payload": {"activity_code": "idle"}, "seq": 4,
         "type": "event"},
    )
    events = []

    async def consume():
        async for event in client.read_stream():
            events.append(event)

    async with pipe(responder) as (client, ws):
        stream_id = await client.subscribe()
        await ws.inbox.put(wire(stream_id, body_chunk=BodyChunk(data=pre)))
        task = asyncio.create_task(consume())
        await until(lambda: len(events) >= 2, what="正文前的两条状态事件")
        assert len(events) == 2, "正文之前的 completed/online 不构成收尾"
        await ws.inbox.put(wire(stream_id, body_chunk=BodyChunk(data=post)))
        await asyncio.wait_for(task, 2.0)
        assert events[2]["text"] == "回答"
        assert events[-1]["payload"]["activity_code"] == "idle"


async def test_read_stream_ends_on_legacy_message_done_after_text():
    """旧模型的 `delta.message_done` 仍算收尾（引擎契约指望它），但同样要求先有正文。"""
    responder = Responder({"/chat/subscribe": [reply(end_body=False)]})
    async with pipe(responder) as (client, ws):
        stream_id = await client.subscribe()
        await ws.inbox.put(
            wire(
                stream_id,
                body_chunk=BodyChunk(
                    data=ndjson(
                        {"event": "delta.message_done", "payload": {"status": "completed"},
                         "seq": 1, "type": "event"},
                        {"event": "delta.text_append", "payload": {"text": "正文"}, "seq": 2,
                         "type": "event"},
                        {"event": "delta.message_done", "payload": {"status": "completed"},
                         "seq": 3, "type": "event"},
                    )
                ),
            )
        )
        events = await drain(client.read_stream())
        # 第一条 message_done 在正文之前，不算收尾；正文之后的那条才收尾。
        assert len(events) == 3
        assert [event["name"] for event in events] == [
            "delta.message_done",
            "delta.text_append",
            "delta.message_done",
        ]


async def test_read_stream_returns_when_subscription_stream_ends():
    """服务端把流关了（response 帧 end_body）就结束迭代，不要对着死流空转。"""
    responder = Responder({"/chat/subscribe": [reply(status=200, end_body=True)]})
    async with pipe(responder) as (client, _ws):
        await client.subscribe()
        assert await drain(client.read_stream(), timeout=1.0) == []


async def test_read_stream_honours_timeout_deadline_without_raising():
    """`timeout` 是本回合读截止：到点返回（不抛异常），避免静默时永远挂住。

    为什么是返回而不是抛：抛会让上层引擎收到一个与自己 timeout 重复的异常类型，
    "没等到"的裁决应该只有一处。
    """
    responder = Responder({"/chat/subscribe": [reply(end_body=False)]})
    async with pipe(responder) as (client, _ws):
        await client.subscribe()
        assert await drain(client.read_stream(timeout=0.05), timeout=1.0) == []


def test_decorate_event_keeps_raw_keys_and_only_delta_carries_text():
    """`kind`/`text` 是**加法**：原始 event/payload/seq 必须原样保留。

    两份引擎（protocol_engine / protocol_generation）按原始键读事件，替换键会静默
    打断它们；同时正文只从 delta.text_append.payload.text 取，状态事件不带 text。
    """
    delta = HatchProtocolClient._parse_event(
        b'{"event":"delta.text_append","payload":{"text":"9+9\xe7\xad\x89\xe4\xba\x8e18\xe3\x80\x82"},"seq":37}'
    )
    assert HatchProtocolClient._decorate_event(delta) == "9+9等于18。"
    assert delta["kind"] == "delta"
    assert delta["text"] == "9+9等于18。"
    assert delta["event"] == "delta.text_append"
    assert delta["seq"] == 37
    assert delta["payload"]["text"] == "9+9等于18。"

    user = HatchProtocolClient._parse_event(
        b'{"event":"message.user","payload":{"display_text":"9+9"}}'
    )
    assert HatchProtocolClient._decorate_event(user) == ""
    assert user["kind"] == "status"
    assert "text" not in user, "状态事件不得带 text，否则正文会被吐两遍"

    other = HatchProtocolClient._parse_event(b'{"event":"weird.thing","payload":{}}')
    assert HatchProtocolClient._decorate_event(other) == ""
    assert other["kind"] == "event"


async def test_read_stream_ignores_previous_turn_tail_on_reused_subscription():
    """复用订阅流时，上一回合没消费完的尾巴必须跳过，不能当成本轮回答。

    订阅是跨回合的长连接；旧实现按全局 offset 继续扫，上一轮 assistant 的 delta
    会被重新吐出来（`new_task_node` 的注释写的就是这个坑）。
    """
    responder = Responder(
        {
            "/client/register-capabilities": [json_reply({"ok": True})],
            "/chat/subscribe": [reply(end_body=False)],
            "/chat/stream": [json_reply({"session_id": "s-1"})],
        }
    )
    stale = ndjson(
        {"event": "delta.text_append", "payload": {"text": "上一轮的尾巴"}, "seq": 99,
         "type": "event"}
    )
    fresh = ndjson(
        {"event": "delta.text_append", "payload": {"text": "这一轮"}, "seq": 1,
         "type": "event"},
        {"event": "agent.status", "payload": {"activity_code": "idle"}, "seq": 2,
         "type": "event"},
    )
    events = []

    async def consume():
        async for event in client.read_stream("hi", node_id=client.node_id):
            events.append(event)

    async with pipe(responder) as (client, ws):
        stream_id = await client.subscribe()  # 先建订阅：模拟"连接已复用"
        await ws.inbox.put(wire(stream_id, body_chunk=BodyChunk(data=stale)))
        await until(
            lambda: "上一轮的尾巴"
            in bytes(client.responses[stream_id].body).decode("utf-8", "replace"),
            what="上一轮尾巴进 body",
        )
        task = asyncio.create_task(consume())
        await until(
            lambda: any(frame.request.path == "/chat/stream" for frame in ws.frames),
            what="/chat/stream 已发出",
        )
        await ws.inbox.put(wire(stream_id, body_chunk=BodyChunk(data=fresh)))
        await asyncio.wait_for(task, 2.0)
        texts = [event["text"] for event in events if event.get("kind") == "delta"]
        assert texts == ["这一轮"], "上一轮的尾巴不得被当成本轮回答"


async def test_read_stream_with_prompt_bootstraps_preamble_then_sends_message():
    """引擎形态 read_stream(prompt,...)：先 register-capabilities → subscribe，再发消息。

    少了任一步，服务端会把事件路由给浏览器那个 client，本连接永远等不到事件；
    client_id 与 node_id 必须是同一个 UUID。
    """
    responder = Responder(
        {
            "/api/nodes/register": [json_reply({})],
            "/client/register-capabilities": [json_reply({})],
            "/chat/subscribe": [reply(end_body=False)],
            "/chat/stream": [json_reply({"session_id": "s-1"})],
        }
    )
    events = []

    async def consume():
        async for event in client.read_stream("hi", node_id="node-x"):
            events.append(event)
            break

    async with pipe(responder) as (client, ws):
        task = asyncio.create_task(consume())
        await until(lambda: client._sub_stream_id is not None, what="订阅建立")
        await ws.inbox.put(
            wire(
                client._sub_stream_id,
                body_chunk=BodyChunk(data=b'{"event":"delta.text_append"}\n'),
            )
        )
        await until(lambda: len(events) == 1, what="首个事件")
        await task
        paths = [frame.request.path for frame in ws.frames]
        assert paths == [
            "/api/nodes/register",
            "/client/register-capabilities",
            "/chat/subscribe",
            "/chat/stream",
        ]
        register = request_json(ws.frames[0].request)
        assert register["node_id"] == "node-x"
        capabilities = request_json(ws.frames[1].request)
        assert capabilities == {"client_id": "node-x"}, "register 只发 client_id（真机 200 形态）"
        assert request_json(ws.frames[2].request) == {}, "subscribe 发空 body（end_body=True）"
        message = request_json(ws.frames[3].request)
        assert message["message"] == "hi"
        assert message["node_id"] == "node-x"
        assert message["timezone"] == TIMEZONE
        assert message["capabilities"] == [], "真机发的是空数组，不是字符串 capability 列表"
        assert client.node_id == "node-x"


async def test_read_stream_prompt_form_runs_prelude_then_consumes_to_turn_end():
    """引擎形态全链：register → subscribe → /chat/stream → NDJSON 增量 → 正常收尾。

    上一条只断言"前奏发对了 + 首个事件"，这条把**整回合**跑完：正文增量收到、
    agent.status 回到 online、迭代器自己结束（不需要调用方 break）。这是
    protocol_engine.py 那种 `async for ... in read_stream(prompt, ...)` 的真实姿势。
    """
    responder = Responder(
        {
            "/client/register-capabilities": [json_reply({"ok": True})],
            "/chat/subscribe": [reply(end_body=False)],
            "/chat/stream": [json_reply({"session_id": "s-1"})],
        }
    )
    events = []

    async def consume():
        async for event in client.read_stream(
            "9+9", node_id=client.node_id, timeout=2.0
        ):
            events.append(event)

    async with pipe(responder) as (client, ws):
        task = asyncio.create_task(consume())
        await until(
            lambda: any(frame.request.path == "/chat/stream" for frame in ws.frames),
            what="/chat/stream 已发出",
        )
        assert client.session_id == "s-1", "ack 里的 session_id 必须被记下"
        stream_id = client._sub_stream_id
        assert stream_id is not None
        await ws.inbox.put(
            wire(
                stream_id,
                body_chunk=BodyChunk(
                    data=ndjson(
                        {"event": "agent.status", "payload": {"activity_code": "working"},
                         "seq": 1, "type": "event"},
                        {"event": "delta.text_append", "payload": {"text": "9+9等于18。"},
                         "seq": 2, "type": "event"},
                        {"event": "agent.status", "payload": {"activity_code": "online"},
                         "seq": 3, "type": "event"},
                    )
                ),
            )
        )
        await asyncio.wait_for(task, 2.0)  # 收尾条件命中，任务自己结束
        assert [frame.request.path for frame in ws.frames] == [
            "/client/register-capabilities",
            "/chat/subscribe",
            "/chat/stream",
        ]
        assert [event["text"] for event in events if event.get("kind") == "delta"] == [
            "9+9等于18。"
        ]
        assert events[-1]["payload"]["activity_code"] == "online"


async def test_register_capabilities_payload_contract():
    """register-capabilities 必须只发 client_id：字符串 capabilities 会 400。

    真机 2026-11-11：旧 body（含 `capabilities: ["chat_cancel", ...]`）→ 400
    `invalid_params` / `invalid type: string "chat_cancel", expected a map`；
    只留 `{"client_id": "<node_id uuid>"}` → 200。所以这里钉住"恰好一个键"，
    并且 `client_id` 与 node_id 同源（事件按它路由）。
    """
    accepted = {
        "accepted_data_sources": 0,
        "accepted_device_commands": 0,
        "accepted_hatch_app_commands": 0,
        "capabilities": ["video_file_ref_ingest", "voice_user_speech_status"],
        "rejected_ids": [],
    }
    responder = Responder({"/client/register-capabilities": [json_reply(accepted)]})
    async with pipe(responder) as (client, ws):
        result = await client.register_capabilities(list(DEFAULT_CAPABILITIES))
        # 200 响应必须被解析出来（服务端返回的 capabilities 是它自己的字符串清单，
        # 与请求侧的 map 要求不是一回事，不能拿来反推请求体）。
        assert result == accepted
        body = request_json(ws.frames[0].request)
        assert body == {"client_id": client.node_id}
        assert "capabilities" not in body, "字符串 capabilities 会重演那个 400"
        for key in ("platform", "display_name", "version"):
            assert key not in body, "实测 200 的那次只发了 client_id，多余字段没有证据"


async def test_register_capabilities_error_carries_server_body():
    """400 时异常里必须带服务端 body 片段 —— 只报 `HTTP 400` 排障等于零信息。

    用的就是真机那次 400 的原文：它才写明"capabilities 的元素必须是 map"。
    """
    rejection = (
        b'{"ok":false,"error":{"code":"invalid_params","message":"parse JSON: '
        b'invalid type: string \\"chat_cancel\\", expected a map at line 1 column 145"}}'
    )
    responder = Responder(
        {"/client/register-capabilities": [reply(status=400, body=rejection)]}
    )
    async with pipe(responder) as (client, _ws):
        with pytest.raises(ProtocolError, match="HTTP 400") as caught:
            await client.register_capabilities(list(DEFAULT_CAPABILITIES))
        text = str(caught.value)
        assert "invalid_params" in text, "服务端错误码必须可见"
        assert "expected a map" in text, "服务端原话必须可见，否则又要抓一次包"
        assert "/client/register-capabilities HTTP 400" in text


async def test_register_capabilities_ok_false_is_protocol_error():
    """200 但 `ok:false` 也算注册失败：静默当成功会让后面永远等不到事件。"""
    responder = Responder(
        {
            "/client/register-capabilities": [
                json_reply({"ok": False, "error": {"code": "bad", "message": "nope"}})
            ]
        }
    )
    async with pipe(responder) as (client, _ws):
        with pytest.raises(ProtocolError, match="未成功") as caught:
            await client.register_capabilities([])
        assert "nope" in str(caught.value)


async def test_register_node_sets_node_id_and_declares_ping_command():
    """没有 /api/nodes/register，侧聊线程的事件不会投递到本连接。"""
    responder = Responder({"/api/nodes/register": [json_reply({})]})
    async with pipe(responder) as (client, ws):
        await client.register_node("node-7")
        assert client.node_id == "node-7"
        body = request_json(ws.frames[0].request)
        assert body["node_id"] == "node-7"
        assert body["platform"] == PLATFORM
        assert body["commands_v2"]["ping"]["description"] == "Connection liveness check"


async def test_send_action_posts_measured_chat_stream_body():
    """/chat/stream 的 body 必须是真机那次 200 的四字段形态。

    实测：`{"message","node_id","capabilities":[],"timezone"}` → 200，ack 带
    `session_id`/`message_id`/`reply_to_message_id`。旧实现发的字符串 capabilities
    与回填的 `session_id`/`metadata` 都没有对照证据，已删除；本条同时钉住"不会
    在第二条消息上偷偷回填 session_id"（那会改变已验证过的 body）。
    """
    ack = {
        "channel": "main",
        "is_thread": False,
        "message_id": "m-1",
        "reply_to_message_id": "m-1",
        "session_id": "s-1",
    }
    responder = Responder({"/chat/stream": [json_reply(ack)]})
    async with pipe(responder) as (client, ws):
        await client.send_action("hello")
        assert client.session_id == "s-1", "session_id 仍要从 ack 记下来"
        body = request_json(ws.frames[0].request)
        assert body == {
            "message": "hello",
            "node_id": client.node_id,
            "capabilities": [],
            "timezone": TIMEZONE,
        }
        assert "items" not in body
        assert "session_id" not in body
        assert "metadata" not in body

        await client.send_action("again")
        # 第二条消息的 body 与第一条**逐键相同**（除 message），node_id 不变；
        # 否则就是又一次未验证的形状漂移。
        second = request_json(ws.frames[1].request)
        assert set(second) == {"message", "node_id", "capabilities", "timezone"}
        assert second["node_id"] == client.node_id
        assert second["message"] == "again"
        assert client.session_id == "s-1"


async def test_stream_body_with_references_uses_items_and_bare_workspace_path():
    """带附件时 `/chat/stream` 走 items 形态，且 file_ref.path 是裸 workspace 路径。

    真机反证（2026-11-11 10:0x）：用 `sandbox://` 前缀做 path 时服务端 500
    `failed to resolve file_ref path sandbox:/workspace/user/files/…`（单斜杠、
    文件不存在）。hatch-web 的两处锚点：
      07hpppij0ssd9.js@67023  `{type:"file_ref", mime_type, path, filename}`，
        path = uploadPath（无前缀）；
      24thn7in_976_.js@97321  `{items:[…], node_id, capabilities, …}`，文本项也在
        items（`{type:"text", text}`），带附件时**不发 message**。
    """
    client = HatchProtocolClient({})
    body = client._stream_body(
        "按参考图重绘",
        [
            {
                "type": "file_ref",
                "mime_type": "image/png",
                "path": "workspace/user/files/ref_0.png",
                "filename": "ref_0.png",
            }
        ],
        "node-1",
    )
    assert "message" not in body, "带附件时不发 message（浏览器两处调用点一致）"
    assert body["items"] == [
        {"type": "text", "text": "按参考图重绘"},
        {
            "type": "file_ref",
            "mime_type": "image/png",
            "path": "workspace/user/files/ref_0.png",
            "filename": "ref_0.png",
        },
    ]
    assert body["node_id"] == "node-1"
    assert body["capabilities"] == []
    assert body["timezone"] == client.timezone


async def test_send_action_keeps_node_id_equal_to_registered_client_id():
    """`node_id` 必须等于 register 用的 client_id：不同就会被路由到别的 client。"""
    responder = Responder({"/chat/stream": [json_reply({"session_id": "s-1"})]})
    async with pipe(responder) as (client, ws):
        registered = client.node_id
        await client.send_action("hi")
        body = request_json(ws.frames[0].request)
        assert body["node_id"] == registered
        assert body["node_id"] == client.node_id


async def test_ping_payload_contract():
    """心跳是"这条连接还活着"最便宜的证据；presence 必须是 active。"""
    responder = Responder({"/api/ping": [json_reply({"ok": True})]})
    async with pipe(responder) as (client, ws):
        await client.ping()
        assert ws.frames[0].request.path == "/api/ping"
        assert request_json(ws.frames[0].request) == {"presence": "active"}


# ---------------------------------------------------------------- fs/read


async def test_fs_read_pages_until_eof():
    """单页最多 1MiB；只取第一页会拿到截断文件（渲染半张图，失败点离原因很远）。"""
    responder = Responder(
        {
            "/fs/read": [
                json_reply(
                    {
                        "result": {
                            "data_base64": base64.b64encode(b"abc").decode(),
                            "len": 3,
                            "eof": False,
                        }
                    }
                ),
                json_reply(
                    {
                        "result": {
                            "data_base64": base64.b64encode(b"def").decode(),
                            "len": 3,
                            "eof": True,
                        }
                    }
                ),
            ]
        }
    )
    async with pipe(responder) as (client, ws):
        assert await client.fs_read("workspace/gen.bin", length=3) == b"abcdef"
        bodies = [request_json(frame.request) for frame in ws.frames]
        assert [body["offset"] for body in bodies] == [0, 3]
        assert [body["path"] for body in bodies] == [
            "workspace/gen.bin",
            "workspace/gen.bin",
        ]
        assert [body["len"] for body in bodies] == [3, 3]


async def test_fs_read_rejects_zero_page_without_eof():
    """0 字节且未置 eof 是异常形态；继续翻页就是死循环。"""
    responder = Responder(
        {
            "/fs/read": [
                json_reply({"result": {"data_base64": "", "len": 0, "eof": False}})
            ]
        }
    )
    async with pipe(responder) as (client, _ws):
        with pytest.raises(ProtocolError, match="0 字节但未置 eof"):
            await client.fs_read("workspace/gen.bin")


async def test_fs_read_requires_generated_workspace_path():
    """读接口只认生成物目录；用户上传区是输入，不是生成物。"""
    async with pipe() as (client, ws):
        with pytest.raises(ProtocolError, match="用户上传区"):
            await client.fs_read("workspace/user/ref.png")
        assert ws.frames == []
        with pytest.raises(ProtocolError, match="workspace/"):
            await client.fs_read("/etc/passwd")


async def test_fs_read_accepts_sandbox_uri_and_strips_prefix():
    """事件里的成品路径是 sandbox:// 形态；请求体必须是 workspace/ 相对路径。"""
    responder = Responder(
        {
            "/fs/read": [
                json_reply(
                    {"result": {"data_base64": "eg==", "len": 1, "eof": True}}
                )
            ]
        }
    )
    async with pipe(responder) as (client, ws):
        assert await client.fs_read("sandbox://workspace/gen.z") == b"z"
        assert request_json(ws.frames[0].request)["path"] == "workspace/gen.z"


# ---------------------------------------------------------------- fs/write


async def test_fs_write_stages_chunks_then_validates_and_renames():
    """分片写临时名 → stat 校验字节数 → rename 到位：三步缺一不可。

    直接写目标名会让"写一半"的文件被下一次同名上传读到；不校验 size 会把
    丢包写进 workspace 而没人发现。
    """
    responder = Responder(
        {
            "/fs/write": [json_reply({"ok": True})],
            "/fs/stat": [json_reply({"result": {"kind": "file", "size": 9}})],
            "/fs/rename": [json_reply({"ok": True})],
        }
    )
    async with pipe(responder) as (client, ws):
        result = await client.fs_write("workspace/out.bin", b"x" * 9, chunk=4)
        writes = [request_json(frame.request) for frame in ws.frames]
        assert [body["data_base64"] for body in writes[:3]] == [
            base64.b64encode(b"xxxx").decode(),
            base64.b64encode(b"xxxx").decode(),
            base64.b64encode(b"x").decode(),
        ]
        assert [body["overwrite"] for body in writes[:3]] == [True, False, False]
        assert [body["append"] for body in writes[:3]] == [False, True, True]
        assert [body["create_parent"] for body in writes[:3]] == [True, False, False]
        staging = writes[0]["path"]
        assert staging.startswith("workspace/.upload-")
        assert staging.endswith(".part")
        assert all(body["path"] == staging for body in writes[:3])
        assert writes[3]["path"] == staging, "stat 校验的是临时文件"
        assert writes[4]["source"] == staging
        assert writes[4]["destination"] == "workspace/out.bin"
        assert writes[4]["overwrite"] is True
        assert writes[4]["create_parent"] is True
        assert result["type"] == "file_ref"
        assert result["path"] == "sandbox://workspace/out.bin"
        assert result["filename"] == "out.bin"
        assert result["size"] == 9
        assert "application" in result["mime_type"]


async def test_fs_write_engine_form_uploads_into_reference_dir():
    """引擎形态 fs_write(data, mime, filename) 必须落到 workspace/user/files/。

    参考素材不在那个目录里，会话根本读不到（等于没上传）。
    """
    responder = Responder(
        {
            "/fs/write": [json_reply({"ok": True})],
            "/fs/stat": [json_reply({"result": {"kind": "file", "size": len(PNG)}})],
            "/fs/rename": [json_reply({"ok": True})],
        }
    )
    async with pipe(responder) as (client, ws):
        result = await client.fs_write(PNG, "image/png", "cat.png")
        assert result["path"] == "sandbox://workspace/user/files/cat.png"
        assert result["mime_type"] == "image/png"
        assert result["filename"] == "cat.png"
        rename = request_json(ws.frames[-1].request)
        assert rename["destination"] == "workspace/user/files/cat.png"


async def test_fs_write_rolls_back_on_stat_failure():
    """写入成功但校验失败时必须 /fs/delete 清掉临时文件再报错（fail closed）。

    留半个文件在 workspace 里，下一次同名上传会读到脏内容，
    这比直接失败更难诊断。
    """
    responder = Responder(
        {
            "/fs/write": [json_reply({"ok": True})],
            "/fs/stat": [reply(status=500, body=b"{}")],
            "/fs/delete": [json_reply({"ok": True})],
        }
    )
    async with pipe(responder) as (client, ws):
        with pytest.raises(ProtocolError, match="fs/stat HTTP 500"):
            await client.fs_write("workspace/out.bin", b"data")
        assert ws.frames[-1].request.path == "/fs/delete"
        assert ws.frames[-2].request.path == "/fs/stat"
        assert "/fs/rename" not in [frame.request.path for frame in ws.frames]


async def test_fs_write_rolls_back_when_stat_kind_is_not_file():
    """stat 没把临时文件认成 file（例如被当成目录）→ 回滚。"""
    responder = Responder(
        {
            "/fs/write": [json_reply({"ok": True})],
            "/fs/stat": [json_reply({"result": {"kind": "dir", "size": 4}})],
            "/fs/delete": [json_reply({"ok": True})],
        }
    )
    async with pipe(responder) as (client, ws):
        with pytest.raises(ProtocolError, match="没把临时文件认成文件"):
            await client.fs_write("workspace/out.bin", b"data")
        assert ws.frames[-1].request.path == "/fs/delete"
        delete = request_json(ws.frames[-1].request)
        assert delete["path"] == request_json(ws.frames[0].request)["path"]
        assert delete["recursive"] is False


async def test_fs_write_rolls_back_on_size_mismatch():
    """声明 4 字节、服务端 stat 说 3：写丢了字节，必须回滚而不是 rename。"""
    responder = Responder(
        {
            "/fs/write": [json_reply({"ok": True})],
            "/fs/stat": [json_reply({"result": {"kind": "file", "size": 3}})],
            "/fs/delete": [json_reply({"ok": True})],
        }
    )
    async with pipe(responder) as (client, ws):
        with pytest.raises(ProtocolError, match="上传大小不符"):
            await client.fs_write("workspace/out.bin", b"data")
        assert ws.frames[-1].request.path == "/fs/delete"
        assert ws.frames[-2].request.path == "/fs/stat"
        assert "/fs/rename" not in [frame.request.path for frame in ws.frames]


async def test_fs_write_rolls_back_when_write_reports_failure():
    """写接口回 ok=false 时即使还没 stat 也要清临时文件（可能已写了半截）。"""
    responder = Responder(
        {
            "/fs/write": [json_reply({"ok": False, "error": "disk full"})],
            "/fs/delete": [json_reply({"ok": True})],
        }
    )
    async with pipe(responder) as (client, ws):
        with pytest.raises(ProtocolError, match="disk full"):
            await client.fs_write("workspace/out.bin", b"data")
        # rollback 必须先于异常抛出；这里 write → delete，没有 stat/rename
        paths = [frame.request.path for frame in ws.frames]
        assert paths == ["/fs/write", "/fs/delete"]


# ---------------------------------------------------------------- media_candidates


async def test_media_candidates_accepts_only_structured_payloads():
    """本客户端不反向列举服务端资源：字符串/None 输入返回空表，绝不假装有列举端点。"""
    async with pipe() as (client, _ws):
        assert client.media_candidates() == []
        assert client.media_candidates("node-1") == []
        found = client.media_candidates(
            {"resources": [{"path": "workspace/gen/a.png", "mime": "image/png"}]}
        )
        assert [item["path"] for item in found] == ["sandbox://workspace/gen/a.png"]
        found = client.media_candidates(
            json.dumps(
                {"resources": [{"path": "workspace/user/up.png"}]}
            ).encode()
        )
        assert found == [], "用户上传区不是生成物"


async def test_media_candidates_rejects_invalid_json_bytes():
    async with pipe() as (client, _ws):
        with pytest.raises(ProtocolError, match="字节输入不是 JSON"):
            client.media_candidates(b"not-json")


# ---------------------------------------------------------------- 心跳


async def test_heartbeat_waits_interval_and_survives_ping_errors(monkeypatch):
    """心跳契约：每次 ping 前先等满 heartbeat_interval（默认 30s），
    且单次 ping 失败不得终止循环（否则连接空闲被回收后没人再证明它还活着）。

    这里把 asyncio.sleep 换成"记录间隔后抛 CancelledError"，不真等 30 秒；
    断言的是"被等待的时长"而不是墙钟时间，所以不会因机器慢而漂移。
    """
    client = HatchProtocolClient(COOKIES)
    sleeps = []
    attempts = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        sleeps.append(delay)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError
        await real_sleep(0)

    async def fake_ping(presence="active"):
        attempts.append(presence)
        if len(attempts) == 1:
            raise RuntimeError("一次网络抖动")  # 必须被 suppress 掉
        return {"ok": True}

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(client, "ping", fake_ping)
    task = asyncio.create_task(client._heartbeat())
    await asyncio.gather(task, return_exceptions=True)
    assert sleeps == [30.0, 30.0, 30.0]
    assert attempts == ["active", "active"]
    assert client.heartbeat_interval == 30.0


# ---------------------------------------------------------------- HTTP 代理口径


async def test_http_client_never_reads_system_proxy():
    """2026-11-11 真机踩过的坑：Windows 系统代理指向 127.0.0.1:18000，而那条
    mihomo 隧道当时节点全挂。httpx 默认 trust_env=True 会把"直连本来可用"的
    /api/session 也塞进死代理，报 `ConnectError: SSL UNEXPECTED_EOF`，看上去
    像"服务器出口网络坏了"——实际是客户端自己偷用了代理。协议层的代理只能
    显式传（proxy=），不得读系统/环境代理，否则同一份代码在不同机器上
    "能不能连"取决于本机代理健康度，这是不可接受的行为漂移。
    """
    client = HatchProtocolClient(COOKIES)
    http = await client._ensure_http()
    try:
        assert http.trust_env is False
        assert client._http is http
    finally:
        await http.aclose()


async def test_http_client_uses_explicit_proxy_only():
    """显式 proxy 仍然生效（只是不像 trust_env 那样偷偷多一个来源）。"""
    client = HatchProtocolClient(
        COOKIES, proxy={"server": "socks5://127.0.0.1:7890"}
    )
    http = await client._ensure_http()
    try:
        assert http.trust_env is False
    finally:
        await http.aclose()
