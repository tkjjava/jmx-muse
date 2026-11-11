"""画布 API：`/canvas/api/*`（空间 CRUD + 只读分享 + 前端探测用的 health）。

为什么只读分享的"读"免鉴权、而"写"必须鉴权：分享的产品语义就是把画布只读地
递给**没有 bridge key** 的人（`/canvas/?share=<token>`），token 本身就是不可猜的
能力串（128 位随机）；再把读口加上 Bearer，链接就只能发给同机持有 key 的人，
分享形同虚设。写权限仍然只挂在 `/spaces/{id}` 上——本模块没有任何"凭 token 写"
的入口，所以被分享者拿到的字节只能是只读文档。

为什么 PUT 必须整体覆盖 `doc` 而不是 merge：画布文档是前端唯一真相，节点的
删除、层级调整、成组移动在 merge 语义下根本无法表达（服务端只能靠"某个字段
没出现"猜用户想删掉什么，猜错就静默丢内容）。整体覆盖只有一种解释：落库的
就是请求体里那一份，出问题时把请求体与库里内容一比对即可复现，也不会出现
"一半旧节点 + 一半新节点"那种前端自己都还原不出来的半新半旧状态。

为什么超限（>4 MB / >2000 节点 / >4000 边）也回 `400 invalid_doc` 而不是 413：
前端只需要一条失败分支，`note` 里写明具体哪一项超了（节点数 / 边数 / 字节数）
就够定位。多一个状态码等于多一处前端要记的分支，而画布场景里它没有任何可
操作差异。

为什么 `health` 免鉴权、且读库失败也不回 5xx：前端在没有 bridge 时降级到
localStorage，它必须能在**没有凭据**的情况下判断"后端在不在"。
它只回 `{"ok": true, "spaces": N}`；读库失败回 `{"ok": false, "spaces": 0,
"note": ...}`，仍是 200（进程活着），不带堆栈、不带路径。

为什么不 import `app`：`app.py` 会 `include_router(本模块)`，反向 import 就是
循环依赖。存储实例由本模块的 `get_store()` 提供，测试里用
`app.dependency_overrides[get_store]` 就能换成临时库；鉴权用了一个与
`app.require_key` **语义相同**的本地依赖（同一个 `MUSE_BRIDGE_API_KEYS`、
本地 keyless 放行）。Lead 若再给 router 加挂 `Depends(require_key)` 也不会冲突
——两次同样的判断，结果一致。

所有响应都是 JSON（**唯一例外**是 `GET /canvas/api/muse/export/{slug}`：那是
必须走 `attachment` 的下载字节，理由见该端点的注释）；错误统一
`{"error": ..., "note": ...}`，任何返回都不含异常堆栈或库路径。SQL 全参数化
（见 `canvas_store`）。

muse Spaces / idea-cards 适配（`/muse/*`，只读）为什么不复用 `/v1` 的办法：
这些端点是"用本机保存的账号 cookie 去读该账号的私有 Spaces / Explore 卡片"，
能列出、能导出就等于能看到用户的私有产物，所以与画布写入口用同一把 Bearer
key；`/shares/{token}` 那种免鉴权读口不适用于这里（唯一例外是 idea-card
图标代理，`<img>` 无法携带 Bearer，理由见该端点注释）。导出只走 `attachment`
而不是 inline 的理由写在 `export_muse_space` 的注释里（XSS 硬约束）。
"""

from __future__ import annotations

import logging
import math
import os
import re
import secrets
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from . import media
from .canvas_store import (
    DB_ENV,
    DEFAULT_DB,
    MAX_NAME_LENGTH,
    CanvasDocError,
    CanvasStore,
)
from .muse_spaces import (
    MuseBinary,
    MuseInvalidSlug,
    MuseSpacesClient,
    MuseSpacesError,
    MuseUnavailable,
    validate_identifier,
    validate_slug,
)
from .store import AccountStore

logger = logging.getLogger(__name__)

#: 前端静态资产的根目录。Lead 用它挂 `StaticFiles(directory=STATIC_DIR)`；
#: 本模块不建目录、不托管（`muse_bridge/canvas/` 由前端负责）。
STATIC_DIR = Path(__file__).resolve().parent / "canvas"

AUTH_ENV = "MUSE_BRIDGE_API_KEYS"

router = APIRouter(prefix="/canvas/api", tags=["canvas"])


class SpaceCreate(BaseModel):
    """`POST /spaces` 的请求体。"""

    name: str


class SpaceUpdate(BaseModel):
    """`PUT /spaces/{id}` 的请求体。两个字段都可省略：省略即"这次不改"。"""

    name: str | None = None
    doc: dict | None = None


# ---- 依赖 ---------------------------------------------------------------

_stores: dict[str, CanvasStore] = {}
_stores_lock = threading.Lock()


def get_store() -> CanvasStore:
    """按数据库路径返回缓存的 `CanvasStore`（一个连接 + 一把锁）。

    缓存键是解析后的路径而不是模块级单例：`MUSE_BRIDGE_DB` 指向别的库时
    拿到的是另一个实例，不会继续写老库。测试直接覆盖本依赖即可，不碰真库。
    """
    db_path = os.environ.get(DB_ENV, DEFAULT_DB)
    with _stores_lock:
        store = _stores.get(db_path)
        if store is None:
            store = _stores[db_path] = CanvasStore(db_path)
        return store


def allowed_keys() -> set[str]:
    raw = os.environ.get(AUTH_ENV, "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def require_canvas_key(authorization: str | None = Header(default=None)) -> None:
    """与 `app.require_key` 同语义：配了 `MUSE_BRIDGE_API_KEYS` 才校验。

    本模块不能 import `app`（循环依赖），所以这里重写了一遍同样的判断；
    语义必须保持一致，否则同一台 bridge 上画布与 /v1 会给出两种"有没有带
    key"的答案。`health` 不使用本依赖。
    """
    keys = allowed_keys()
    if not keys:
        return
    token = authorization.removeprefix("Bearer ").strip() if authorization else ""
    if token not in keys:
        raise HTTPException(status_code=401, detail="无效的 API 密钥")


# ---- 错误形状 -----------------------------------------------------------

def _error(status_code: int, error: str, note: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": error, "note": note})


def _not_found(space_id: str) -> JSONResponse:
    return _error(404, "not_found", f"空间不存在：{space_id}")


def _invalid_doc(exc: CanvasDocError) -> JSONResponse:
    return _error(400, "invalid_doc", str(exc))


def _invalid_name(name: str) -> JSONResponse:
    return _error(
        400,
        "invalid_name",
        f"空间名最多 {MAX_NAME_LENGTH} 个字符（收到 {len(name)} 个）",
    )


# 依赖用 `Annotated` 而不是 `= Depends(...)` 默认值：ruff B008 会对"默认值里做
# 函数调用"报错（对自定义类型的注解尤其如此），而 `Annotated` 是 FastAPI 现在
# 推荐写法；`dependency_overrides[get_store]` 的覆盖语义完全不变。
StoreDep = Annotated[CanvasStore, Depends(get_store)]
AuthDep = Annotated[None, Depends(require_canvas_key)]


# ---- 端点 ---------------------------------------------------------------


@router.get(
    "/health",
    summary="画布后端可用性探测",
    description=(
        "供前端判断 bridge 是否在线：不需要 API key，也不会因为库不可读而回 5xx。"
        "`spaces` 是当前空间条数（只做 COUNT，不解析文档）。"
    ),
)
def canvas_health(store: StoreDep):
    """免鉴权、永不 5xx：离线判断依赖它，探测端点自己先挂掉就没意义了。"""
    try:
        spaces = store.count()
    except Exception as exc:  # 探测端点不向外抛任何异常，只报类型名（不带路径）
        return {"ok": False, "spaces": 0, "note": f"画布库不可读：{type(exc).__name__}"}
    return {"ok": True, "spaces": spaces}


@router.get(
    "/spaces",
    summary="列出全部画布空间",
    description=(
        "按 `updated_at` 倒序返回 SpaceMeta 列表（不含文档正文，"
        "`node_count` 由服务端解析文档得出）。"
    ),
)
def list_spaces(
    _auth: AuthDep,
    store: StoreDep,
):
    """只回元信息：列表页不需要每份文档的几 MB 正文。坏档在这里报 400 而不是静默跳过。"""
    try:
        return {"spaces": store.list()}
    except CanvasDocError as exc:
        return _invalid_doc(exc)


@router.post(
    "/spaces",
    summary="新建画布空间",
    description=(
        "服务端生成 `sp_[a-z0-9]{8}` 形式的 id；`name` 去空白后为空则落"
        "「未命名画布」，超过 120 字符回 `400 invalid_name`。新空间默认是空画布。"
    ),
)
def create_space(
    body: SpaceCreate,
    _auth: AuthDep,
    store: StoreDep,
):
    """只接受名字：初始文档由服务端给空画布，客户端不必猜默认 camera/gridSize。"""
    clean = body.name.strip()
    if len(clean) > MAX_NAME_LENGTH:
        return _invalid_name(clean)
    try:
        return {"space": store.create(clean)}
    except CanvasDocError as exc:  # 默认空文档不可能非法；这条只防默认值被改坏
        return _invalid_doc(exc)


@router.get(
    "/spaces/{space_id}",
    summary="读取一个画布空间",
    description="返回 `{\"space\": SpaceMeta, \"doc\": Doc}`；未知 id 回 `404 not_found`。",
)
def get_space(
    space_id: str,
    _auth: AuthDep,
    store: StoreDep,
):
    """整份文档一起回：前端要靠 camera 与 nodes 一次性还原画面，分两次取会撕裂。"""
    try:
        record = store.get(space_id)
    except CanvasDocError as exc:
        return _invalid_doc(exc)
    if record is None:
        return _not_found(space_id)
    return record


@router.put(
    "/spaces/{space_id}",
    summary="保存画布（整体覆盖文档）",
    description=(
        "`doc` 按契约 §4 服务端校验，不合法回 `400 invalid_doc` 且**不落库**；"
        "合法则**整体覆盖**而不是 merge。只传 `name` 就是改名。"
        "未知 id 回 `404 not_found`。"
    ),
)
def update_space(
    space_id: str,
    body: SpaceUpdate,
    _auth: AuthDep,
    store: StoreDep,
):
    """整体覆盖而不是 merge：删除节点在 merge 语义下无法表达，见模块 docstring。"""
    if body.name is not None:
        clean = body.name.strip()
        if len(clean) > MAX_NAME_LENGTH:
            return _invalid_name(clean)
    if body.name is None and body.doc is None:
        # 空 PUT 只回当前元信息，不推进 updated_at：没改东西就不该让别处看起来"被改过"。
        try:
            record = store.get(space_id)
        except CanvasDocError as exc:
            return _invalid_doc(exc)
        if record is None:
            return _not_found(space_id)
        return {"space": record["space"]}
    try:
        space = store.update(space_id, name=body.name, doc=body.doc)
    except CanvasDocError as exc:
        return _invalid_doc(exc)
    if space is None:
        return _not_found(space_id)
    return {"space": space}


@router.delete(
    "/spaces/{space_id}",
    summary="删除画布空间",
    description="成功回 `{\"ok\": true}`；未知 id 回 `404 not_found`（删除不是 id 探测器）。",
)
def delete_space(
    space_id: str,
    _auth: AuthDep,
    store: StoreDep,
):
    """未知 id 回 404 而不是 ok：前端要能区分"删掉了"和"本来就没有"。"""
    if not store.delete(space_id):
        return _not_found(space_id)
    return {"ok": True}


@router.post(
    "/spaces/{space_id}/share",
    summary="创建（或轮换）只读分享链接",
    description=(
        "为空间生成 128 位随机 token，返回 `{\"share\": {token, space_id, created_at, url}}`。"
        "同一空间只保留**一个**活跃 token：再次调用会作废旧链接。"
        "未知 id 回 `404 not_found`；需要 Bearer key（写入口必须鉴权）。"
    ),
)
def create_space_share(
    space_id: str,
    _auth: AuthDep,
    store: StoreDep,
):
    """分享 = 一条 capability URL。轮换语义见 `CanvasStore.share` 的 docstring。"""
    share = store.share(space_id)
    if share is None:
        return _not_found(space_id)
    return {"share": share}


@router.get(
    "/spaces/{space_id}/share",
    summary="读取当前分享链接",
    description="没有分享过返回 `{\"share\": null}`；未知空间 `404 not_found`。GET 无副作用。",
)
def get_space_share(
    space_id: str,
    _auth: AuthDep,
    store: StoreDep,
):
    try:
        record = store.get(space_id)
    except CanvasDocError as exc:
        return _invalid_doc(exc)
    if record is None:
        return _not_found(space_id)
    return {"share": store.share_of(space_id)}


@router.delete(
    "/shares/{token}",
    summary="撤销分享链接",
    description=(
        "撤销后该 token 立即失效（下次 `GET /shares/{token}` 回 404）。"
        "幂等：未知 token 仍回 `200 {\"ok\": false}`，不把撤销做成 token 探测器。"
    ),
)
def revoke_share(
    token: str,
    _auth: AuthDep,
    store: StoreDep,
):
    return {"ok": store.revoke_share(token)}


@router.get(
    "/shares/{token}",
    summary="读取分享画布（只读、免鉴权）",
    description=(
        "分享链接的读取口：`token` 是不可猜的 capability，返回 `{\"space\", \"doc\", "
        "\"share\", \"read_only\": true}`。**故意免鉴权**——分享的本质就是让对方"
        "在不带 key 的情况下只读查看；写权限仍然只挂在 `/spaces/{id}` 上"
        "（需要 Bearer key），本模块没有任何基于 token 的写入口。"
        "无效/已撤销/空间已删统一回 `404 not_found`。"
    ),
)
def read_shared_space(
    token: str,
    store: StoreDep,
):
    try:
        payload = store.resolve_share(token)
    except CanvasDocError as exc:
        return _invalid_doc(exc)
    if payload is None:
        return _error(404, "not_found", "分享链接已失效或不存在")
    return {**payload, "read_only": True}


@router.get(
    "/media/{token}",
    summary="画布同源媒体代理",
    description=(
        "把 `<img src>` 拿不到 Bearer 的问题收在服务端：画布图片卡与 PNG 导出都走本路由，"
        "前端因此不需要给图片请求加请求头，也不会让离屏 canvas 因跨源被 taint。"
    ),
)
def canvas_media(
    token: str,
    range_header: str | None = Header(default=None, alias="range"),
):
    """媒体字节的同源出口。

    为什么**不**复用 `/v1/media/{token}`（那里 `require_key` 只认 `Authorization: Bearer`）：
    `<img>`/`drawImage` 发不了自定义请求头，浏览器里加头只能走 fetch+Blob URL，
    但那样导出 PNG 时又会撞上"图片来自 Blob"的分支与额外内存拷贝。这里直接给字节，
    `token` 本身是服务端生成的不透明能力串（猜不到、拿不到别人的），且只读。
    为什么不做鉴权：本进程的 `MUSE_BRIDGE_API_KEYS` 是给 API 调用方的，不是给画布
    渲染层；画布由浏览器同源加载，能读到 token 就等于已经拿到了这张图。
    """
    ms = media.MediaStore()
    try:
        ref = ms.ref(token)
    except media.MediaError as exc:
        status = exc.status_code
        if status == 404:
            return JSONResponse(
                status_code=404, content={"error": "not_found", "note": "媒体不存在"}
            )
        return JSONResponse(
            status_code=status, content={"error": "media_error", "note": str(exc)}
        )
    headers = media.serve_headers(size=ref.size, filename=ref.filename, mime=ref.mime)
    # `/v1/media` 故意恒发 `application/octet-stream + attachment`（不给浏览器就地
    # 渲染的机会）。画布要的正好相反：图片卡与导出都要浏览器把它当图读，所以这里
    # 用 MediaRef 已经嗅出来的真实 mime 覆盖，并把 disposition 改成 inline。
    headers["Content-Type"] = ref.mime or "application/octet-stream"
    headers["Content-Disposition"] = "inline"
    headers["X-Muse-Media-Mime"] = ref.mime or "application/octet-stream"
    span = media.parse_range(range_header, ref.size) if range_header else None
    if span is None:
        return Response(content=ms.read(ref.token), headers=headers)
    try:
        data, first, last, total = ms.read_range(ref.token, span[0], span[1])
    except media.MediaRangeError as exc:
        return JSONResponse(
            status_code=416, content={"error": "range_not_satisfiable", "note": str(exc)}
        )
    headers["Content-Range"] = f"bytes {first}-{last}/{total}"
    headers["Content-Length"] = str(len(data))
    return Response(content=data, status_code=206, headers=headers)


# ---- muse 只读适配：账号选择、客户端与节点契约 ---------------------------

#: 单次 muse 协议请求的超时。列表是"点一下等结果"的交互，导出更重，所以给
#: 45s（与 `protocol_driver` 的 30s 量级同档，但导出多留一点渲染时间）。
MUSE_TIMEOUT = 45.0

#: 有多个账号时优先用这个；没有它再按 `list_all` 顺序找第一个有 cookie 的。
PREFERRED_MUSE_ACCOUNT = "muse1"

_account_stores: dict[str, AccountStore] = {}
_account_stores_lock = threading.Lock()


def get_account_store() -> AccountStore | None:
    """账号库依赖；拿不到（缺 `MUSE_BRIDGE_KEY` / 库打不开）时返回 None。

    为什么返回 None 而不是抛：调用方要把"这台 bridge 还没有可用的 muse 账号"
    统一映射成 503 `muse_unavailable`。抛异常会让 FastAPI 回 500，响应里带上与
    本请求无关的配置错误（哪把 key 没配），前端也分不清是"没账号"还是"库坏了"。

    为什么可以缓存连接：本模块对 `accounts` 表**只读**（解密 cookie），写路径仍
    由 `app` 自己的 `AccountStore` 负责；缓存键是库路径（与 `get_store` 同手法），
    测试用 `app.dependency_overrides[get_account_store]` 换成假库即可，不碰真库。
    """
    db_path = os.environ.get(DB_ENV, DEFAULT_DB)
    try:
        with _account_stores_lock:
            store = _account_stores.get(db_path)
            if store is None:
                store = _account_stores[db_path] = AccountStore(db_path)
            return store
    except Exception as exc:  # 配置/权限/磁盘问题一律按"没有账号"处理，不泄漏细节
        # 对外仍然回 503/`ok:false`（不带细节），但**日志要留一行**：否则库路径写错、
        # 目录权限不对这类问题会表现成"这台 bridge 没有 muse 账号"，排查方向全错。
        # 只记类型与消息到 debug，运维调日志级别即可看到，不会进响应。
        logger.debug("画布账号库不可用（按无账号处理）：%s", exc)
        return None


def pick_muse_account(store: AccountStore) -> tuple[str, dict[str, str]] | None:
    """挑一个有 cookie 的账号（优先 `muse1`），返回 `(account, cookies)` 或 None。

    为什么必须"有 cookie"：本适配层只走纯协议（不碰 CDP / 浏览器）；没有
    `hatch_sess` 的账号连 `/api/session` 都续不上，建客户端只会白烧一次 connect。
    `pool._build_driver` 对聊天/生成用的是同一条判据。
    """
    records = list(store.list_all())
    preferred = store.get(PREFERRED_MUSE_ACCOUNT)
    if preferred is not None:
        rest = [record for record in records if record.account != PREFERRED_MUSE_ACCOUNT]
        records = [preferred, *rest]
    for record in records:
        cookies = store.cookies(record.account)
        if cookies:
            return record.account, dict(cookies)
    return None


def build_muse_client(store: AccountStore) -> MuseSpacesClient | None:
    """按当前账号与代理配置建一个只读协议客户端；无可用账号返回 None。

    `MUSE_PROTOCOL_PROXY` 与 pool 的协议路径共用同一个环境变量：真机结论是 WS
    必须经本机代理到 hatch.metaaivm.com（直连 TLS 不通）。代理串只在构造时交给
    客户端，不进日志、不进异常、不进响应。

    测试直接 monkeypatch 本函数即可让所有 `/muse/*` 端点零网络、零账号库。
    """
    picked = pick_muse_account(store)
    if picked is None:
        return None
    account, cookies = picked
    proxy = os.environ.get("MUSE_PROTOCOL_PROXY", "").strip() or None
    return MuseSpacesClient(cookies, account=account, proxy=proxy, timeout=MUSE_TIMEOUT)


def _require_muse_client(accounts: AccountStore | None) -> MuseSpacesClient:
    """依赖 -> 客户端，或者抛 `MuseUnavailable`（503 的唯一来源）。"""
    if accounts is None:
        raise MuseUnavailable(
            "账号库不可用：检查 MUSE_BRIDGE_KEY 是否配置、MUSE_BRIDGE_DB 是否可读"
        )
    client = build_muse_client(accounts)
    if client is None:
        raise MuseUnavailable("没有可用账号：先在 /console attach，或给账号导入 cookie")
    return client


AccountStoreDep = Annotated[AccountStore | None, Depends(get_account_store)]


def _muse_error(exc: MuseSpacesError) -> JSONResponse:
    return _error(exc.status_code, exc.code, exc.note)


async def _run_muse(
    accounts: AccountStore | None,
    call: Callable[[MuseSpacesClient], Awaitable[object]],
) -> object | JSONResponse:
    """一次 `/muse/*` 调用的完整生命周期：建客户端 → 连接 → `call` → 关连接。

    错误分三类，且**都不含堆栈**：
      * `MuseSpacesError`（含 `MuseUnavailable`）：按异常自带的 status/code 映射；
      * 其余异常：502 `muse_error`，只报异常类型名（防止代理凭据/本地路径进响应）；
      * 正常：把 `call` 的返回值原样交给路由层。
    """
    try:
        client = _require_muse_client(accounts)
    except MuseSpacesError as exc:
        return _muse_error(exc)
    try:
        async with client:
            return await call(client)
    except MuseSpacesError as exc:
        return _muse_error(exc)
    except Exception as exc:
        return _error(502, "muse_error", f"muse 协议调用失败（{type(exc).__name__}）")


def _pixels(value: object, fallback: int) -> int:
    """卡片像素尺寸的归一化：有限正数才用，否则回退；下限 80（§4 的最小节点尺寸）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    if not math.isfinite(value) or value <= 0:
        return fallback
    return max(80, int(round(value)))


def muse_space_card_node(space: dict[str, object]) -> dict[str, object]:
    """muse Space -> 画布节点 JSON（**纯函数**，不写 muse、不写本地库）。

    契约（前端拿到后自行决定位置并 PUT 进 `doc.nodes`）::

        {
          "node": {
            "id": "n_[a-z0-9]{10}",          # 与 canvas_store.NODE_ID_RE 同形
            "type": "image" | "text",
            "x": 0, "y": 0, "w": <int>, "h": <int>, "z": 0,
            "data": {
              "text": "<space name>",
              "url": "<preview_card_url | thumbnail_url | null>",
              "thumbUrl": "<thumbnail_url | image_url | null>",
              "width": <int>, "height": <int>,
              "status": "done",
              "muse": {
                "spaceSlug": ..., "viewUrl": ..., "shortcode": ...,
                "updatedAtMs": ..., "constructionStatus": ...
              }
            }
          }
        }

    依据（为什么长这样）：
      * `docs/CANVAS_DESIGN.md` §4：Node 必填键是 `id/type/x/y/w/h/z/data`；
        NodeData 允许 `url`/`thumbUrl`/`width`/`height`/`status`/`text`/`muse`，
        且"未知字段保留并透传"——所以 `muse` 子对象与 `text` 都能安全放进 data；
      * §10 P3 修订：导入只"预览图 / 名称 / 链接"，**不**承诺文档同步。因此卡片
        指向 muse 的预览图 URL（`preview_image.preview_card_url`，退化到
        `thumbnail_url`），而不是把 HTML 塞进节点；
      * 没有预览图时退化成 `text` 卡片（名称 + `view_url` 收在 `muse` 里），
        这样"列表里存在但还在构建"的 Space 也能放上画布；
      * id 由 `secrets.token_hex(5)` 生成：`n_` + 10 位小写 hex，满足 §4 的
        `^n_[a-z0-9]{10}$`（同形；前端入库前仍应做一次唯一性去重）。
    """
    preview = space.get("preview_image")
    preview = preview if isinstance(preview, dict) else {}
    preview_url = preview.get("preview_card_url") or preview.get("thumbnail_url")
    thumb_url = preview.get("thumbnail_url") or space.get("image_url")
    if preview_url:
        width = _pixels(
            preview.get("preview_card_width_px") or preview.get("thumbnail_width_px"),
            640,
        )
        height = _pixels(
            preview.get("preview_card_height_px") or preview.get("thumbnail_height_px"),
            400,
        )
        node_type = "image"
    else:
        # 文本卡：固定一个"标题卡"尺寸，避免把 640x400 的图片框空着放在画布上。
        width, height = 360, 160
        node_type = "text"
    data: dict[str, object] = {
        "text": space.get("name"),
        "url": preview_url,
        "thumbUrl": thumb_url,
        "width": width,
        "height": height,
        "status": "done",
        "muse": {
            "spaceSlug": space.get("slug"),
            "viewUrl": space.get("view_url"),
            "shortcode": space.get("shortcode"),
            "updatedAtMs": space.get("updated_at_ms"),
            "constructionStatus": space.get("construction_status"),
        },
    }
    return {
        "id": f"n_{secrets.token_hex(5)}",
        "type": node_type,
        "x": 0,
        "y": 0,
        "w": width,
        "h": height,
        "z": 0,
        "data": data,
    }


# ---- muse 只读端点 -------------------------------------------------------


@router.get(
    "/muse/spaces",
    summary="列出当前 muse 账号的 Spaces（只读）",
    description=(
        "字段按 `docs/CANVAS_PROTOCOL.md` §2.1 归一化（缺失字段为 null；slug 缺失时"
        "回退 space_id；任一条不可解析则整个响应失败）。无可用账号/无 cookie → 503 "
        "`muse_unavailable`；`include_constructing=true` 透传上游同名参数。"
    ),
)
async def list_muse_spaces(
    _auth: AuthDep,
    accounts: AccountStoreDep,
    include_constructing: bool = False,
):
    """`{"spaces": [SpaceListItem...]}`；上游顺序原样返回（排序在客户端，§3.3）。"""
    result = await _run_muse(
        accounts,
        lambda client: client.list_spaces(include_constructing=include_constructing),
    )
    if isinstance(result, JSONResponse):
        return result
    return {"spaces": result}


class MuseImportRequest(BaseModel):
    """`POST /muse/import` 请求体。只收 slug：卡片摆在画布哪里由前端决定。"""

    slug: str


@router.post(
    "/muse/import",
    summary="把 muse Space 转成画布卡片节点（只读预览，不写 muse）",
    description=(
        "返回 `{\"node\": <画布节点>, \"space\": <归一化 Space>}`：节点形状与依据写在"
        " `muse_space_card_node` 的 docstring（§4 Node 形状 + §10 P3 修订的"
        "\"只导入预览图/名称/链接\"）。本端点**不**写 muse，也**不**写本地画布库；"
        "前端拿到节点后自行 PUT。找不到该 slug → 404 `not_found`。"
    ),
)
async def import_muse_space(
    body: MuseImportRequest,
    _auth: AuthDep,
    accounts: AccountStoreDep,
):
    """把 Space 变成一张卡片节点；不落库、不同步文档（P3 修订的边界）。"""
    try:
        slug = validate_slug(body.slug)
    except MuseSpacesError as exc:
        return _muse_error(exc)
    result = await _run_muse(
        accounts,
        # `include_constructing=True`：正在构建中的 Space 也要能导入成卡片；
        # 列表端点本身默认 false，与前端的默认调用一致。
        lambda client: client.list_spaces(include_constructing=True),
    )
    if isinstance(result, JSONResponse):
        return result
    spaces = result if isinstance(result, list) else []
    space = next(
        (
            item
            for item in spaces
            if isinstance(item, dict) and item.get("slug") == slug
        ),
        None,
    )
    if space is None:
        return _error(404, "not_found", f"muse 空间不存在：{slug}")
    return {"node": muse_space_card_node(space), "space": space}


_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _export_headers(slug: str, size: int, mime: str) -> dict[str, str]:
    """导出响应的头：**没有任何一条分支会变成内联渲染**。

    安全硬约束（改动前先读完）：导出的 HTML 是 muse 服务端渲染、内容来自用户/
    AI 的整包文档。它与画布前端**同源**（同一 host 下的 `/canvas` 与
    `/canvas/api`），一旦以 `text/html` + `inline` 渲染，页面里的脚本就能：
      1) 读 `localStorage` 里的 bridge key（`/console` 把 key 存在那一格）；
      2) 以同源身份调用 `/admin/*` 与 `/v1/*`（key 被读走，鉴权形同虚设）；
      3) 用 `fetch` 读 `/canvas/api/shares/<token>` 等其它画布内容。
    因此这里四条一起发：`Content-Type` 恒 `application/octet-stream`（即便某层
    忽略 disposition，也不会被解析成 HTML）、`Content-Disposition` 恒
    `attachment`（默认下载）、`X-Content-Type-Options: nosniff`、
    `Content-Security-Policy: sandbox`（纵深防御；对附件无害）。
    为什么不落 media store 再发 token：`/canvas/api/media/{token}` 对图片是
    inline（画布卡片必须能 `<img>`），HTML 走那条路径等于把同一个 XSS 换个 token
    重新引入；"attachment 直发"是唯一不新增渲染面的做法。
    文件名只由校验过的 slug 派生（正则再清洗一次），不含引号/换行/分隔符，
    没有响应头注入面。
    """
    safe = _SAFE_FILENAME_RE.sub("_", slug)[:120].strip("._") or "space"
    return {
        "Content-Type": "application/octet-stream",
        "Content-Length": str(size),
        "Content-Disposition": f'attachment; filename="{safe}.html"',
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox",
        "Cache-Control": "private, no-store",
        "X-Muse-Export-Mime": mime or "text/html",
    }


@router.get(
    "/muse/export/{slug}",
    summary="下载 muse Space 的 export.html（只能作为附件下载）",
    description=(
        "透传 `GET /spaces/{slug}/export.html`（§10.1/§12.1#10，二进制）。"
        "响应恒为 `application/octet-stream` + `Content-Disposition: attachment`："
        "导出 HTML 与本服务同源，inline 渲染等于把 XSS 交给 muse 侧可控内容，"
        "详细理由见响应头构造函数的注释。"
        "`slug` 非法 → 400；muse 侧不存在 → 404 `not_found`。"
    ),
)
async def export_muse_space(
    slug: str,
    _auth: AuthDep,
    accounts: AccountStoreDep,
):
    """导出 muse 成品 HTML 字节，只以下载附件形式发出（安全硬约束见注释）。"""
    try:
        clean = validate_slug(slug)
    except MuseSpacesError as exc:
        return _muse_error(exc)
    result = await _run_muse(accounts, lambda client: client.export_html(clean))
    if isinstance(result, JSONResponse):
        return result
    if not isinstance(result, MuseBinary):
        return _error(502, "muse_error", "muse 导出没有返回字节")
    return Response(
        content=result.data,
        headers=_export_headers(clean, len(result.data), result.mime),
    )


#: 可以 inline 的 mime **白名单**（不是 `mime.startswith("image/")`）。
#: 理由：`image/svg+xml` 也是 image，但 SVG 能执行脚本；与本服务同源 inline
#: 一个 SVG/HTML 等于把 XSS 交还给 muse 侧可控内容。只放浏览器按纯像素解码的
#: 栅格格式，其余（SVG/HTML/未知）一律走附件。
INLINE_IMAGE_MIMES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/heic",
        "image/heif",
        "image/avif",
    }
)

#: 上游图标 URL 的相对前缀（真机样本：`/api/idea-cards/media/icon-57e8…?v=…`）。
_IDEA_MEDIA_PATH = "/api/idea-cards/media/"
#: 本进程同源代理前缀：卡片图标必须能直接 `<img src>`，所以要把上游相对地址改写
#: 过来；否则前端拼 `/api/...` 会打到本进程没挂载的路径（404）。
_PROXIED_IDEA_MEDIA_PATH = "/canvas/api/muse/idea-cards/media/"


def _proxy_icon_url(value: object) -> object:
    """把上游相对媒体地址改写成同源代理地址；其它值原样返回。

    只改写确实指向 `idea-cards` 媒体的相对路径（绝对 URL、以及别的相对路径都不
    碰——服务端没有替别人拼 host 的职责）。`?v=` 查询串保留：上游用它做内容版本
    缓存键，丢掉会让浏览器复用到旧图标。
    """
    if not isinstance(value, str) or not value.startswith(_IDEA_MEDIA_PATH):
        return value
    path, _, query = value.partition("?")
    handle = path[len(_IDEA_MEDIA_PATH) :]
    if not handle:
        return value
    suffix = f"?{query}" if query else ""
    return f"{_PROXIED_IDEA_MEDIA_PATH}{handle}{suffix}"


def _proxied_idea_card(card: dict[str, object]) -> dict[str, object]:
    """卡片浅拷贝 + 把图标 URL 换成同源代理（不改动原始对象，便于测试断言纯度）。"""
    rewritten = dict(card)
    rewritten["iconUrl"] = _proxy_icon_url(card.get("iconUrl"))
    variants = card.get("iconVariants")
    if isinstance(variants, list):
        rewritten["iconVariants"] = [
            {**variant, "url": _proxy_icon_url(variant.get("url"))}
            if isinstance(variant, dict)
            else variant
            for variant in variants
        ]
    return rewritten


def _proxied_idea_cards(payload: dict[str, object]) -> dict[str, object]:
    """列表结构浅拷贝 + 逐卡改写图标 URL。"""
    result = dict(payload)
    sections = payload.get("sections")
    if isinstance(sections, list):
        result["sections"] = [
            {
                **section,
                "cards": [
                    _proxied_idea_card(card)
                    for card in section.get("cards") or []
                    if isinstance(card, dict)
                ],
            }
            if isinstance(section, dict)
            else section
            for section in sections
        ]
    return result



def _muse_media_headers(filename: str, mime: str, size: int) -> dict[str, str]:
    """idea-card 媒体的响应头：栅格图 inline（给 `<img>`），其余强制附件下载。"""
    safe = _SAFE_FILENAME_RE.sub("_", filename)[:120].strip("._") or "media"
    common = {
        "Content-Length": str(size),
        "X-Content-Type-Options": "nosniff",
        "X-Muse-Media-Mime": mime,
        # 图标按内容哈希命名（真机 handle 形如 `icon-57e8…aa6`），URL 即版本，
        # 短缓存能省掉重复取图；`private` 保证不被共享代理缓存。
        "Cache-Control": "private, max-age=600",
    }
    if mime in INLINE_IMAGE_MIMES:
        return {
            **common,
            "Content-Type": mime,
            "Content-Disposition": f'inline; filename="{safe}"',
        }
    return {
        **common,
        "Content-Type": "application/octet-stream",
        "Content-Disposition": f'attachment; filename="{safe}.bin"',
        "Content-Security-Policy": "sandbox",
    }


@router.get(
    "/muse/idea-cards",
    summary="列出 muse Explore 灵感卡（只读）",
    description=(
        "归一化 `GET /api/idea-cards`（§12.3，真机 7 个 section / 67,759 B）："
        "返回 `{\"sections\": [{\"id\",\"title\",\"layout\",\"cards\":[{...}]}],"
        " \"viewerState\", \"pagination\"}`；卡片只保留 id/标题/摘要/构建状态/"
        "图标 URL 等字段，未知字段收在 `extra`。卡片上的 `iconUrl`/`iconVariants` "
        "已改写为同源 `/canvas/api/muse/idea-cards/media/...`（可直接 `<img>`）。"
    ),
)
async def list_muse_idea_cards(
    _auth: AuthDep,
    accounts: AccountStoreDep,
):
    """只读灵感卡列表；不触发 `idea-cards.execute`（那是异步建应用，另属 P3）。"""
    result = await _run_muse(accounts, lambda client: client.list_idea_cards())
    if isinstance(result, JSONResponse):
        return result
    if not isinstance(result, dict):
        return result
    return _proxied_idea_cards(result)


@router.get(
    "/muse/idea-cards/media/{handle}",
    summary="muse 灵感卡图标代理（同源、按文件头给 mime）",
    description=(
        "把 muse 的 `/api/idea-cards/media/{handle}` 字节同源转发，供 `<img>` 使用。"
        "mime 以**文件头嗅探**为准；只有栅格图片 inline，其余一律 attachment。"
        "本端点不校验 Bearer（理由见函数注释）。"
    ),
)
async def muse_idea_card_media(
    handle: str,
    accounts: AccountStoreDep,
):
    """图标/媒体代理。

    为什么**不**要求 Bearer：`<img>` 发不了自定义请求头（与 `/canvas/api/media/{token}`
    完全同一个问题与先例）。这里没有像 `media` 那样用服务端随机 token，而是直接
    透传 muse 的 handle，理由是：handle 形如内容哈希（真机 `icon-57e8…aa6`），
    只由 Bearer 保护的列表/详情下发，猜不到；泄出的也只是 Explore 灵感卡的公开
    图标（非用户私有产物）。安全边界靠三件事兜住：严格 handle 校验（不合法直接
    404，不区分"非法/不存在"）+ 文件头嗅探 mime + 栅格白名单才 inline。
    """
    try:
        clean = validate_identifier(handle, what="idea card media handle")
    except MuseInvalidSlug:
        # 非法 handle 与不存在同样回 404：不给"这个 handle 格式合法但没资源"的探测差异。
        return _error(404, "not_found", "idea card 媒体不存在")
    result = await _run_muse(accounts, lambda client: client.idea_card_media(clean))
    if isinstance(result, JSONResponse):
        return result
    if not isinstance(result, MuseBinary):
        return _error(502, "muse_error", "idea card 媒体没有返回字节")
    return Response(
        content=result.data,
        headers=_muse_media_headers(clean, result.mime, len(result.data)),
    )


@router.get(
    "/muse/idea-cards/{card_id}",
    summary="读取 muse Explore 灵感卡详情（只读）",
    description=(
        "归一化 `GET /api/idea-cards/{ideaCardId}`（§12.3，真机 200/1,877 B）："
        "返回 `{\"card\": {...含 items}}`；图标 URL 已改写为同源代理地址。"
        "卡片 id 非法 → 400，muse 侧不存在 → 404 `not_found`。"
    ),
)
async def get_muse_idea_card(
    card_id: str,
    _auth: AuthDep,
    accounts: AccountStoreDep,
):
    """单卡详情（不触发 execute）。"""
    try:
        clean = validate_identifier(card_id, what="idea card id")
    except MuseSpacesError as exc:
        return _muse_error(exc)
    result = await _run_muse(accounts, lambda client: client.get_idea_card(clean))
    if isinstance(result, JSONResponse):
        return result
    card = _proxied_idea_card(result) if isinstance(result, dict) else result
    return {"card": card}

