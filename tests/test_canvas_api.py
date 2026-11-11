"""画布 API 的离线契约测试：CRUD 全链路 + 404/400 + health 免鉴权。

刻意**不 import `muse_bridge.app`**：这里自己装配一个 `FastAPI()` 并
`include_router(router)`，再用 `dependency_overrides[get_store]` 换成
`tmp_path` 里的临时库。理由有两条：一是 API 契约不该靠整个 bridge 的启动
（账号库需要 Fernet key、pool 需要浏览器配置）才能验证；二是真实 `app` 一
旦被 import 就会读 `MUSE_BRIDGE_DB`，测试有可能摸到 `data/muse.db`。
"""

from __future__ import annotations

import ast
import re
from copy import deepcopy
from itertools import pairwise
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from muse_bridge import canvas_api as canvas_api_module
from muse_bridge.canvas_api import STATIC_DIR, get_store, router
from muse_bridge.canvas_store import CanvasStore, empty_doc

SPACE_ID = re.compile(r"sp_[a-z0-9]{8}")
SPACE_META_KEYS = {
    "id",
    "name",
    "created_at",
    "updated_at",
    "node_count",
    "thumbnail",
    "source",
}


@pytest.fixture
def store(tmp_path):
    instance = CanvasStore(tmp_path / "canvas-api.db")
    yield instance
    instance.close()


@pytest.fixture
async def api(store, monkeypatch):
    monkeypatch.delenv("MUSE_BRIDGE_API_KEYS", raising=False)
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_store] = lambda: store
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://canvas") as client:
        yield client


def _node(node_id: str = "n_aaaaaaaaaa", **overrides) -> dict:
    node = {
        "id": node_id,
        "type": "image",
        "x": 12,
        "y": 24,
        "w": 320,
        "h": 240,
        "z": 1,
        "data": {"prompt": "一只猫", "status": "done"},
    }
    node.update(overrides)
    return node


def _doc(*nodes: dict, edges: tuple[dict, ...] = (), **extra) -> dict:
    doc = empty_doc()
    doc["nodes"] = list(nodes)
    doc["edges"] = list(edges)
    doc.update(extra)
    return doc


async def _create(api, name: str = "测试画布") -> dict:
    response = await api.post("/canvas/api/spaces", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["space"]


# ---- 静态目录 -----------------------------------------------------------

def test_static_dir_points_at_the_canvas_folder():
    assert STATIC_DIR.is_absolute()
    assert STATIC_DIR.name == "canvas"
    assert STATIC_DIR.parent.name == "muse_bridge"


# ---- 列表 / 新建 ---------------------------------------------------------

@pytest.mark.asyncio
async def test_list_is_empty_initially(api):
    response = await api.get("/canvas/api/spaces")
    assert response.status_code == 200, response.text
    assert response.json() == {"spaces": []}


@pytest.mark.asyncio
async def test_create_returns_space_meta_and_default_empty_doc(api):
    response = await api.post("/canvas/api/spaces", json={"name": "  新画布  "})
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"space"}
    space = body["space"]
    assert set(space) == SPACE_META_KEYS
    assert SPACE_ID.fullmatch(space["id"])
    assert space["name"] == "新画布"
    assert space["node_count"] == 0
    assert space["thumbnail"] is None
    assert space["source"] == "local"
    assert space["created_at"] == space["updated_at"]

    record = (await api.get(f"/canvas/api/spaces/{space['id']}")).json()
    assert record["space"] == space
    assert record["doc"] == empty_doc()


@pytest.mark.asyncio
async def test_blank_name_falls_back_to_default(api):
    space = await _create(api, "   ")
    assert space["name"] == "未命名画布"


@pytest.mark.asyncio
async def test_overlong_name_is_400_invalid_name(api):
    response = await api.post("/canvas/api/spaces", json={"name": "x" * 121})
    assert response.status_code == 400, response.text
    assert response.json()["error"] == "invalid_name"
    assert "120" in response.json()["note"]


@pytest.mark.asyncio
async def test_list_reflects_node_count_and_newest_first(api):
    first = await _create(api, "甲")
    second = await _create(api, "乙")
    response = await api.get("/canvas/api/spaces")
    assert [item["id"] for item in response.json()["spaces"]] == [
        second["id"],
        first["id"],
    ]

    put = await api.put(
        f"/canvas/api/spaces/{first['id']}", json={"doc": _doc(_node())}
    )
    assert put.status_code == 200, put.text
    listing = (await api.get("/canvas/api/spaces")).json()["spaces"]
    assert listing[0]["id"] == first["id"]
    assert listing[0]["node_count"] == 1


# ---- 读取 / 保存 / 删除 -------------------------------------------------

@pytest.mark.asyncio
async def test_crud_roundtrip_keeps_unknown_fields(api):
    space = await _create(api, "全链路")
    node = _node(future={"later": [1, 2]})
    doc = _doc(
        node,
        _node("n_bbbbbbbbbb", type="text", data={"text": "便签"}, x=500),
        edges=({"id": "e_1", "from": node["id"], "to": "n_bbbbbbbbbb"},),
        future_toplevel={"ok": True},
    )

    put = await api.put(f"/canvas/api/spaces/{space['id']}", json={"doc": doc})
    assert put.status_code == 200, put.text
    put_body = put.json()
    assert set(put_body) == {"space"}
    assert put_body["space"]["node_count"] == 2

    record = (await api.get(f"/canvas/api/spaces/{space['id']}")).json()
    assert record["doc"] == doc  # 未知字段一个都不丢

    delete = await api.delete(f"/canvas/api/spaces/{space['id']}")
    assert delete.status_code == 200, delete.text
    assert delete.json() == {"ok": True}

    after = await api.get(f"/canvas/api/spaces/{space['id']}")
    assert after.status_code == 404, after.text
    assert after.json()["error"] == "not_found"


@pytest.mark.asyncio
async def test_put_name_only_renames_without_touching_doc(api):
    space = await _create(api, "原名")
    doc = _doc(_node())
    await api.put(f"/canvas/api/spaces/{space['id']}", json={"doc": doc})

    rename = await api.put(
        f"/canvas/api/spaces/{space['id']}", json={"name": "  改名了  "}
    )
    assert rename.status_code == 200, rename.text
    assert rename.json()["space"]["name"] == "改名了"
    record = (await api.get(f"/canvas/api/spaces/{space['id']}")).json()
    assert record["doc"] == doc


@pytest.mark.asyncio
async def test_put_empty_body_is_a_noop(api):
    space = await _create(api, "没变化")
    response = await api.put(f"/canvas/api/spaces/{space['id']}", json={})
    assert response.status_code == 200, response.text
    assert response.json()["space"] == space  # updated_at 都没动


@pytest.mark.asyncio
async def test_updated_at_strictly_increases_on_put(api):
    space = await _create(api, "计时")
    timestamps = [space["updated_at"]]
    for index in range(3):
        response = await api.put(
            f"/canvas/api/spaces/{space['id']}", json={"doc": _doc(_node(x=index))}
        )
        timestamps.append(response.json()["space"]["updated_at"])
    assert all(later > earlier for earlier, later in pairwise(timestamps))


@pytest.mark.asyncio
async def test_unknown_id_is_404_with_contract_envelope(api):
    unknown = "sp_deadbeef"
    responses = [
        await api.get(f"/canvas/api/spaces/{unknown}"),
        await api.put(f"/canvas/api/spaces/{unknown}", json={"doc": empty_doc()}),
        await api.delete(f"/canvas/api/spaces/{unknown}"),
    ]
    for response in responses:
        assert response.status_code == 404, response.text
        assert response.json() == {
            "error": "not_found",
            "note": f"空间不存在：{unknown}",
        }


@pytest.mark.asyncio
async def test_put_valid_doc_then_delete_then_list_is_empty(api):
    space = await _create(api, "删除后")
    await api.delete(f"/canvas/api/spaces/{space['id']}")
    assert (await api.get("/canvas/api/spaces")).json() == {"spaces": []}


# ---- 400 invalid_doc ----------------------------------------------------

INVALID_DOCS = [
    ("version", {"version": 2, "nodes": [], "edges": []}, "version 必须为 1"),
    ("w-zero", _doc(_node(w=0)), "w 必须 > 0"),
    ("dup-id", _doc(_node(), _node()), "重复的节点 id"),
    (
        "edge-dangling",
        _doc(
            _node(),
            edges=({"id": "e", "from": "n_aaaaaaaaaa", "to": "n_zzzzzzzzzz"},),
        ),
        "edges 里 to 指向不存在的节点 n_zzzzzzzzzz",
    ),
    ("type", _doc(_node(type="audio")), "image/video/prompt/text"),
    (
        "zoom",
        {**empty_doc(), "camera": {"x": 0, "y": 0, "zoom": 0.01}},
        "camera.zoom 必须在",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("doc", "fragment"),
    [case[1:] for case in INVALID_DOCS],
    ids=[case[0] for case in INVALID_DOCS],
)
async def test_invalid_doc_is_400_and_not_persisted(api, doc, fragment):
    space = await _create(api, "校验")
    good = _doc(_node())
    await api.put(f"/canvas/api/spaces/{space['id']}", json={"doc": good})
    before = (await api.get(f"/canvas/api/spaces/{space['id']}")).json()

    response = await api.put(f"/canvas/api/spaces/{space['id']}", json={"doc": doc})
    assert response.status_code == 400, response.text
    body = response.json()
    assert set(body) == {"error", "note"}
    assert body["error"] == "invalid_doc"
    assert fragment in body["note"]
    assert "Traceback" not in response.text
    assert response.headers["content-type"].startswith("application/json")

    after = (await api.get(f"/canvas/api/spaces/{space['id']}")).json()
    assert after == before  # 校验失败不落库


@pytest.mark.asyncio
async def test_too_many_nodes_is_400_invalid_doc(api):
    space = await _create(api, "超限")
    nodes = [_node(f"n_{index:010d}", type="text", data={"text": "x"}) for index in range(2001)]
    response = await api.put(
        f"/canvas/api/spaces/{space['id']}", json={"doc": _doc(*nodes)}
    )
    assert response.status_code == 400, response.text
    assert "nodes 最多 2000 个" in response.json()["note"]

    untouched = (await api.get(f"/canvas/api/spaces/{space['id']}")).json()
    assert untouched["space"]["node_count"] == 0


@pytest.mark.asyncio
async def test_non_object_doc_is_rejected_by_request_shape(api):
    """`doc: dict` 在 FastAPI 层就是 422（框架形状错误），不是我们能改的 400。

    只有在 doc 是对象、但内容不合法时才走 `400 invalid_doc`——两者都由测试钉住，
    免得以后有人把模型改成 `Any` 而悄悄改变契约。
    """
    space = await _create(api, "非对象")
    response = await api.put(
        f"/canvas/api/spaces/{space['id']}", json={"doc": ["不是对象"]}
    )
    assert response.status_code == 422, response.text


# ---- health：免鉴权 ------------------------------------------------------

@pytest.mark.asyncio
async def test_health_needs_no_key(api):
    await _create(api, "一条")
    response = await api.get("/canvas/api/health")  # 不带头
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "spaces": 1}


@pytest.mark.asyncio
async def test_health_stays_open_while_crud_requires_key(api, monkeypatch):
    """配了 key 之后：health 仍可探测，CRUD 必须带 key——前端靠这个区分降级。"""
    monkeypatch.setenv("MUSE_BRIDGE_API_KEYS", "s3cret")

    health = await api.get("/canvas/api/health")
    assert health.status_code == 200, health.text
    assert health.json()["ok"] is True

    denied = await api.get("/canvas/api/spaces")
    assert denied.status_code == 401, denied.text

    allowed = await api.get(
        "/canvas/api/spaces", headers={"Authorization": "Bearer s3cret"}
    )
    assert allowed.status_code == 200, allowed.text


@pytest.mark.asyncio
async def test_health_does_not_leak_stack_on_broken_store():
    class _Broken:
        def count(self) -> int:
            raise RuntimeError("data/muse.db 打不开")

    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_store] = lambda: _Broken()
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://canvas") as client:
        response = await client.get("/canvas/api/health")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["spaces"] == 0
    assert "data/muse.db" not in response.text
    assert "Traceback" not in response.text


# ---- 响应形状 -----------------------------------------------------------

@pytest.mark.asyncio
async def test_get_returns_meta_plus_doc_only(api):
    space = await _create(api, "形状")
    response = await api.get(f"/canvas/api/spaces/{space['id']}")
    assert set(response.json()) == {"space", "doc"}
    assert response.headers["content-type"].startswith("application/json")


@pytest.mark.asyncio
async def test_put_does_not_echo_doc_back(api):
    """PUT 只回 SpaceMeta：把几 MB 的文档再回一遍没有信息量，还会放大上传开销。"""
    space = await _create(api, "不回文档")
    response = await api.put(
        f"/canvas/api/spaces/{space['id']}", json={"doc": _doc(_node())}
    )
    assert set(response.json()) == {"space"}
    assert "doc" not in response.json()


@pytest.mark.asyncio
async def test_store_is_not_written_by_invalid_create_body(api, store):
    """请求体形状错误（缺 name）时连空间都不该建出来。"""
    response = await api.post("/canvas/api/spaces", json={})
    assert response.status_code == 422, response.text
    assert store.count() == 0


def test_canvas_api_module_does_not_import_app():
    """本模块不得 import muse_bridge.app：Lead 要在 app 里 include_router，反向就是循环。

    用 AST 看导入语句而不是查 `sys.modules`：全量跑测时别的测试文件早就
    import 过 app，按 sys.modules 判断会得到假绿/假红。
    """
    tree = ast.parse(Path(canvas_api_module.__file__).read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module in ("app", "muse_bridge.app"):
                offenders.append(f"from {node.module} import ...")
            elif node.module is None:
                offenders.extend(
                    f"from . import {alias.name}"
                    for alias in node.names
                    if alias.name == "app"
                )
        elif isinstance(node, ast.Import):
            offenders.extend(
                f"import {alias.name}"
                for alias in node.names
                if alias.name in ("app", "muse_bridge.app")
            )
    assert offenders == []


@pytest.mark.asyncio
async def test_doc_roundtrip_is_byte_stable(api):
    """存进去再取出来必须与请求体完全一致（比较 dict，不比较字段顺序）。"""
    space = await _create(api, "稳定")
    doc = _doc(_node(), meta={"theme": "dark", "background": "dots", "gridSize": 28})
    snapshot = deepcopy(doc)
    await api.put(f"/canvas/api/spaces/{space['id']}", json={"doc": doc})
    record = (await api.get(f"/canvas/api/spaces/{space['id']}")).json()
    assert record["doc"] == snapshot == doc


# ---- 只读分享：写入口要 key，读分享口免密 ---------------------------------


@pytest.mark.asyncio
async def test_share_write_requires_key_but_public_read_does_not(api, monkeypatch):
    """分享的读写权限必须分离：创建/撤销要 Bearer，读分享链接**故意**免鉴权。"""
    space = await _create(api, "分享画布")
    doc = _doc(_node())
    await api.put(f"/canvas/api/spaces/{space['id']}", json={"doc": doc})
    monkeypatch.setenv("MUSE_BRIDGE_API_KEYS", "s3cret")

    denied = await api.post(f"/canvas/api/spaces/{space['id']}/share")
    assert denied.status_code == 401, denied.text

    allowed = await api.post(
        f"/canvas/api/spaces/{space['id']}/share",
        headers={"Authorization": "Bearer s3cret"},
    )
    assert allowed.status_code == 200, allowed.text
    share = allowed.json()["share"]
    assert share["url"] == f"/canvas/?share={share['token']}"

    # 免鉴权读取：这正是分享链接的意义（对方没有 bridge key）。
    public = await api.get(f"/canvas/api/shares/{share['token']}")
    assert public.status_code == 200, public.text
    body = public.json()
    assert body["read_only"] is True
    assert body["space"]["id"] == space["id"]
    assert body["doc"] == doc

    # 公开读口不能变成写口：同一个空间不带头 PUT 仍是 401。
    put = await api.put(f"/canvas/api/spaces/{space['id']}", json={"doc": doc})
    assert put.status_code == 401, put.text

    current = await api.get(
        f"/canvas/api/spaces/{space['id']}/share",
        headers={"Authorization": "Bearer s3cret"},
    )
    assert current.status_code == 200
    assert current.json()["share"]["token"] == share["token"]


@pytest.mark.asyncio
async def test_share_read_is_404_after_revoke(api):
    space = await _create(api, "撤销后的分享")
    token = (await api.post(f"/canvas/api/spaces/{space['id']}/share")).json()["share"][
        "token"
    ]

    revoked = await api.delete(f"/canvas/api/shares/{token}")
    assert revoked.status_code == 200, revoked.text
    assert revoked.json() == {"ok": True}

    gone = await api.get(f"/canvas/api/shares/{token}")
    assert gone.status_code == 404, gone.text
    assert gone.json()["error"] == "not_found"

    again = await api.delete(f"/canvas/api/shares/{token}")
    assert again.status_code == 200
    assert again.json() == {"ok": False}, "撤销必须幂等，且不把删除做成 token 探测器"


@pytest.mark.asyncio
async def test_share_unknown_targets_are_404(api):
    unknown = "sp_00000000"
    assert (await api.post(f"/canvas/api/spaces/{unknown}/share")).status_code == 404
    assert (await api.get(f"/canvas/api/spaces/{unknown}/share")).status_code == 404
    assert (await api.get("/canvas/api/shares/sh_" + "0" * 32)).status_code == 404


@pytest.mark.asyncio
async def test_share_get_reports_null_before_first_share(api):
    """GET 没有副作用：没分享过回 null，而不是顺手创建一个 token。"""
    space = await _create(api, "还没分享")
    response = await api.get(f"/canvas/api/spaces/{space['id']}/share")
    assert response.status_code == 200
    assert response.json() == {"share": None}
