"""画布存储的离线单测：CRUD、校验失败不落库、updated_at 单调、上限、并发。

全部用 `tmp_path` 下的文件库或 `:memory:`，**不碰** `data/muse.db`：
真实账号库里有加密凭据与任务队列，测试写进去既污染现场，也会让"校验失败
不落库"这类断言失去意义（别人写过的行会让计数永远不为零）。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections import deque
from copy import deepcopy
from itertools import pairwise

import pytest

from muse_bridge.canvas_store import (
    MAX_DOC_BYTES,
    NODE_ID_RE,
    SPACE_ID_RE,
    CanvasDocError,
    CanvasStore,
    empty_doc,
    normalize_name,
    validate_doc,
)

DEFAULT_NAME = "未命名画布"


@pytest.fixture
def store(tmp_path):
    instance = CanvasStore(tmp_path / "canvas.db")
    yield instance
    instance.close()


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


# ---- 空文档 / 名字 ------------------------------------------------------


def test_empty_doc_matches_contract_exactly():
    assert empty_doc() == {
        "version": 1,
        "camera": {"x": -200, "y": -120, "zoom": 1},
        "nodes": [],
        "edges": [],
        "meta": {"theme": "dark", "background": "dots", "gridSize": 28},
    }


def test_empty_doc_is_a_fresh_object_each_call():
    first = empty_doc()
    first["nodes"].append(_node())
    assert empty_doc()["nodes"] == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("  海边  ", "海边"), ("", DEFAULT_NAME), ("   ", DEFAULT_NAME), (None, DEFAULT_NAME)],
)
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


def test_normalize_name_rejects_overlong():
    with pytest.raises(ValueError, match="最多 120"):
        normalize_name("x" * 121)
    assert normalize_name("x" * 120) == "x" * 120


# ---- CRUD --------------------------------------------------------------


def test_create_defaults_to_unnamed_empty_canvas(store):
    space = store.create("   ")
    assert SPACE_ID_RE.fullmatch(space["id"]), space
    assert space["name"] == DEFAULT_NAME
    assert space["node_count"] == 0
    assert space["thumbnail"] is None
    assert space["source"] == "local"
    assert space["created_at"] == space["updated_at"]

    record = store.get(space["id"])
    assert record is not None
    assert record["space"] == space
    assert record["doc"] == empty_doc()


def test_create_and_get_roundtrip_preserves_unknown_fields(store):
    future_node = _node(extra_in_node={"later": [1, 2]})
    doc = _doc(
        future_node,
        edges=({"id": "e_1", "from": future_node["id"], "to": future_node["id"]},),
        future_top_level={"x": True},
    )
    space = store.create("带未知字段", doc)
    record = store.get(space["id"])
    assert record is not None
    assert record["doc"] == doc  # 未知字段原样透传，一个字节都不改
    assert record["space"]["node_count"] == 1


def test_get_missing_returns_none(store):
    assert store.get("sp_deadbeef") is None


def test_list_is_empty_initially(store):
    assert store.list() == []


def test_list_orders_by_updated_at_desc(store):
    first = store.create("第一个")
    second = store.create("第二个")
    third = store.create("第三个")
    assert [item["id"] for item in store.list()][:2] == [third["id"], second["id"]]

    store.update(first["id"], name="第一个（改过）")
    assert store.list()[0]["id"] == first["id"]
    assert {item["id"] for item in store.list()} == {
        first["id"],
        second["id"],
        third["id"],
    }


def test_update_outranks_same_second_create(store, monkeypatch):
    """同一秒内先建 A 再改 B：改过的 B 必须排第一。

    旧实现 `updated_at = MAX(now, old+1)`：B 拿到的 updated_at 恰好等于刚建的
    A 的 created_at（两者都是 now），列表 `ORDER BY updated_at DESC, rowid DESC`
    于是把 A 排在 B 前面——2026-11-12 全量套件里的偶发失败就是这个。
    现在 update 取 `MAX(now, old+1, 全表最大+1)`，与"刚改过的居顶"语义对齐。
    """
    frozen = 1_800_000_000  # 固定时钟：确保 create 与 update 落在同一秒

    class _FrozenTime:
        @staticmethod
        def time() -> int:
            return frozen

    monkeypatch.setattr("muse_bridge.canvas_store.time", _FrozenTime())
    first = store.create("第一个")
    second = store.create("第二个")
    store.update(first["id"], name="第一个（改过）")
    items = store.list()
    assert items[0]["id"] == first["id"]
    assert items[0]["updated_at"] > items[1]["updated_at"]
    assert items[1]["id"] == second["id"]


def test_list_meta_has_exactly_the_contract_fields(store):
    store.create("甲")
    item = store.list()[0]
    assert set(item) == {
        "id",
        "name",
        "created_at",
        "updated_at",
        "node_count",
        "thumbnail",
        "source",
    }


def test_update_name_keeps_doc(store):
    doc = _doc(_node())
    space = store.create("原名", doc)
    updated = store.update(space["id"], name="  新名  ")
    assert updated is not None
    assert updated["name"] == "新名"
    assert updated["node_count"] == 1
    assert store.get(space["id"])["doc"] == doc


def test_update_doc_keeps_name(store):
    space = store.create("名字")
    doc = _doc(_node(), _node("n_bbbbbbbbbb", x=400))
    updated = store.update(space["id"], doc=doc)
    assert updated is not None
    assert updated["name"] == "名字"
    assert updated["node_count"] == 2
    assert store.get(space["id"])["doc"] == doc


def test_update_bumps_updated_at_strictly_even_within_one_second(store):
    space = store.create("计时")
    timestamps = [space["updated_at"]]
    for _ in range(6):
        after = store.update(space["id"], doc=_doc(_node()))
        assert after is not None
        timestamps.append(after["updated_at"])
    assert timestamps == sorted(timestamps)
    assert all(later > earlier for earlier, later in pairwise(timestamps))
    # created_at 不参与单调约定
    assert store.get(space["id"])["space"]["created_at"] == space["created_at"]


def test_update_does_not_change_created_at(store):
    space = store.create("甲")
    updated = store.update(space["id"], name="乙")
    assert updated["created_at"] == space["created_at"]


def test_update_missing_returns_none(store):
    assert store.update("sp_deadbeef", name="x") is None
    assert store.update("sp_deadbeef", doc=empty_doc()) is None


def test_delete_then_get_is_gone(store):
    space = store.create("待删")
    assert store.delete(space["id"]) is True
    assert store.get(space["id"]) is None
    assert store.delete(space["id"]) is False
    assert store.list() == []
    assert store.count() == 0


def test_count_tracks_rows(store):
    assert store.count() == 0
    store.create("a")
    store.create("b")
    assert store.count() == 2


def test_memory_database_works():
    memory = CanvasStore(":memory:")
    try:
        space = memory.create("内存库")
        assert memory.get(space["id"])["doc"] == empty_doc()
    finally:
        memory.close()


# ---- 校验 ----

VALIDATORS = [
    ("doc-not-object", [], "doc 必须是 JSON 对象"),
    ("version-missing", {"nodes": [], "edges": []}, "version 必须为 1"),
    ("version-2", {**empty_doc(), "version": 2}, "version 必须为 1"),
    ("version-bool", {**empty_doc(), "version": True}, "version 必须为 1"),
    ("nodes-not-list", {**empty_doc(), "nodes": {}}, "nodes 必须是数组"),
    ("node-not-object", {**empty_doc(), "nodes": [1]}, "node[0] 必须是对象"),
    (
        "node-id-missing",
        {**empty_doc(), "nodes": [{"type": "image"}]},
        "node[0]: 缺少 id",
    ),
    (
        "node-id-shape",
        _doc(_node("node-short")),
        "必须匹配 ^n_[a-z0-9]{10}$",
    ),
    ("node-w-zero", _doc(_node(w=0)), "node n_aaaaaaaaaa: w 必须 > 0"),
    ("node-h-negative", _doc(_node(h=-4)), "node n_aaaaaaaaaa: h 必须 > 0"),
    ("node-x-inf", _doc(_node(x=float("inf"))), "node n_aaaaaaaaaa: x 必须是有限数"),
    (
        "node-z-nan",
        _doc({**_node(), "z": float("nan")}),
        "node n_aaaaaaaaaa: z 必须是有限数",
    ),
    ("node-type", _doc(_node(type="audio")), "type 'audio' 必须是 image/video/prompt/text 之一"),
    ("node-data-shape", _doc(_node(data=[1])), "node n_aaaaaaaaaa: data 必须是对象"),
    ("node-dup-id", _doc(_node(), _node()), "重复的节点 id"),
    (
        "edge-not-object",
        {**empty_doc(), "edges": [1]},
        "edge[0] 必须是对象",
    ),
    (
        "edge-id-missing",
        _doc(_node(), edges=({"from": "n_aaaaaaaaaa", "to": "n_aaaaaaaaaa"},)),
        "edge[0]: 缺少 id",
    ),
    (
        "edge-from-dangling",
        _doc(_node(), edges=({"id": "e1", "from": "n_cccccccccc", "to": "n_aaaaaaaaaa"},)),
        "edges 里 from 指向不存在的节点 n_cccccccccc",
    ),
    (
        "edge-to-dangling",
        _doc(_node(), edges=({"id": "e1", "from": "n_aaaaaaaaaa", "to": "n_dddddddddd"},)),
        "edges 里 to 指向不存在的节点 n_dddddddddd",
    ),
    (
        "edge-end-missing",
        _doc(_node(), edges=({"id": "e1", "from": "n_aaaaaaaaaa"},)),
        "edge[0]: 缺少 to",
    ),
    (
        "edges-not-list",
        {**empty_doc(), "edges": {}},
        "edges 必须是数组",
    ),
    (
        "camera-not-object",
        {**empty_doc(), "camera": [1, 2]},
        "camera 必须是对象",
    ),
    (
        "camera-x-str",
        {**empty_doc(), "camera": {"x": "0", "y": 0, "zoom": 1}},
        "camera.x 必须是有限数",
    ),
    (
        "camera-zoom-low",
        {**empty_doc(), "camera": {"x": 0, "y": 0, "zoom": 0.01}},
        "camera.zoom 必须在",
    ),
    (
        "camera-zoom-high",
        {**empty_doc(), "camera": {"x": 0, "y": 0, "zoom": 8.5}},
        "camera.zoom 必须在",
    ),
    (
        "meta-not-object",
        {**empty_doc(), "meta": "dark"},
        "meta 必须是对象",
    ),
]


@pytest.mark.parametrize(
    ("doc", "fragment"),
    [case[1:] for case in VALIDATORS],
    ids=[case[0] for case in VALIDATORS],
)
def test_validate_doc_rejects_with_specific_message(doc, fragment):
    with pytest.raises(CanvasDocError) as excinfo:
        validate_doc(doc)
    assert fragment in str(excinfo.value)


@pytest.mark.parametrize(
    "doc",
    [
        empty_doc(),
        _doc(_node(data={})),
        _doc(_node(), edges=({"id": "e1", "from": "n_aaaaaaaaaa", "to": "n_aaaaaaaaaa"},)),
        {**empty_doc(), "camera": {"x": -1e9, "y": 1e9, "zoom": 0.05}},
        {**empty_doc(), "camera": {"x": 0, "y": 0, "zoom": 8}},
        {**empty_doc(), "camera": None, "meta": None},
    ],
    ids=["empty", "data-empty", "self-edge", "zoom-min", "zoom-max", "nulls"],
)
def test_validate_doc_accepts_contract_shapes(doc):
    validate_doc(doc)


def test_validate_doc_is_read_only():
    doc = _doc(_node())
    del doc["nodes"][0]["data"]
    snapshot = deepcopy(doc)
    validate_doc(doc)
    assert doc == snapshot
    assert "data" not in doc["nodes"][0]


def test_validate_doc_allows_unknown_fields():
    doc = _doc(_node(future={"a": 1}), edges=(), future_toplevel=[1, 2, 3])
    validate_doc(doc)  # 不抛即通过；内容未被改动
    assert doc["future_toplevel"] == [1, 2, 3]


def test_node_id_pattern_constant_matches_contract():
    assert NODE_ID_RE.fullmatch("n_0123456789")
    assert NODE_ID_RE.fullmatch("n_012345678") is None
    assert re.fullmatch(r"sp_[a-z0-9]{8}", "sp_ab12cd34")


def test_validate_doc_rejects_too_many_nodes():
    nodes = [_node(f"n_{index:010d}") for index in range(2000)]
    validate_doc(_doc(*nodes))  # 边界值本身合法
    nodes.append(_node("n_9999999999"))
    with pytest.raises(CanvasDocError, match="nodes 最多 2000 个（收到 2001"):
        validate_doc(_doc(*nodes))


def test_validate_doc_rejects_too_many_edges():
    ends = {"from": "n_aaaaaaaaaa", "to": "n_aaaaaaaaaa"}
    edges = tuple({"id": f"e{index}", **ends} for index in range(4000))
    validate_doc(_doc(_node(), edges=edges))
    too_many = (*edges, {"id": "e4000", **ends})
    with pytest.raises(CanvasDocError, match="edges 最多 4000 条"):
        validate_doc(_doc(_node(), edges=too_many))


def test_validate_doc_rejects_oversized_payload():
    big = _doc(_node(data={"text": "x" * (MAX_DOC_BYTES + 16)}))
    with pytest.raises(CanvasDocError, match="MB 上限"):
        validate_doc(big)


def test_validate_doc_accepts_large_but_legal_payload():
    doc = _doc(_node(data={"text": "x" * (1024 * 1024)}))
    validate_doc(doc)
    assert len(json.dumps(doc, ensure_ascii=False).encode()) > 1024 * 1024


# ---- 校验失败不落库 ----

def test_create_with_invalid_doc_writes_nothing(store):
    with pytest.raises(CanvasDocError, match="w 必须 > 0"):
        store.create("坏的", _doc(_node(w=0)))
    assert store.count() == 0
    assert store.list() == []


def test_update_with_invalid_doc_keeps_old_state(store):
    good = _doc(_node())
    space = store.create("好文档", good)
    before = store.get(space["id"])

    with pytest.raises(CanvasDocError, match="w 必须 > 0"):
        store.update(space["id"], doc=_doc(_node(w=0)))
    with pytest.raises(CanvasDocError, match="重复的节点 id"):
        store.update(space["id"], doc=_doc(_node(), _node()))
    with pytest.raises(CanvasDocError, match="指向不存在的节点"):
        store.update(
            space["id"],
            doc=_doc(_node(), edges=({"id": "e", "from": "n_aaaaaaaaaa", "to": "n_zzzzzzzzzz"},)),
        )

    after = store.get(space["id"])
    assert after == before
    assert after["space"]["updated_at"] == before["space"]["updated_at"]


def test_overlong_name_rejected_before_write(store):
    with pytest.raises(ValueError, match="最多 120"):
        store.create("x" * 121)
    assert store.count() == 0


# ---- 坏档 ----

def test_corrupt_json_raises_on_get(tmp_path):
    path = tmp_path / "corrupt.db"
    store = CanvasStore(path)
    space = store.create("会坏掉")
    with sqlite3.connect(str(path)) as conn:
        conn.execute(
            "UPDATE canvas_spaces SET doc_json = ? WHERE id = ?",
            ("{not json", space["id"]),
        )
    with pytest.raises(CanvasDocError, match="不是合法 JSON"):
        store.get(space["id"])
    with pytest.raises(CanvasDocError, match="不是合法 JSON"):
        store.list()
    # count 不解析文档：坏档不影响健康探测
    assert store.count() == 1
    store.close()


def test_non_object_json_raises_on_get(tmp_path):
    path = tmp_path / "list.db"
    store = CanvasStore(path)
    space = store.create("存成数组")
    with sqlite3.connect(str(path)) as conn:
        conn.execute(
            "UPDATE canvas_spaces SET doc_json = ? WHERE id = ?", ("[]", space["id"])
        )
    with pytest.raises(CanvasDocError, match="不是 JSON 对象"):
        store.get(space["id"])
    store.close()


# ---- 并发 ----

def test_concurrent_create_yields_twenty_distinct_ids(store):
    ids: deque[str] = deque()
    lock = threading.Lock()
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            space = store.create(f"并发-{index}")
            with lock:
                ids.append(space["id"])
        except BaseException as exc:  # 线程里的异常必须带回主线程，否则测试假绿
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(ids) == 20
    assert len(set(ids)) == 20
    assert all(SPACE_ID_RE.fullmatch(space_id) for space_id in ids)
    assert store.count() == 20
    assert {item["id"] for item in store.list()} == set(ids)


def test_concurrent_updates_keep_last_write_and_monotonic_clock(store):
    space = store.create("并发写")
    results: list[int] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        updated = store.update(space["id"], doc=_doc(_node(x=index)))
        with lock:
            results.append(updated["updated_at"])

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    final = store.get(space["id"])["space"]
    assert final["updated_at"] >= max(results)
    assert len(set(results)) == len(results)  # 10 次写入拿到 10 个不同的时间戳
    assert store.get(space["id"])["doc"]["nodes"][0]["x"] in range(10)


# ---- 只读分享 ------------------------------------------------------------


def test_share_creates_capability_url_and_reads_back(store):
    """分享 token 是不可猜的能力串；解析回来必须与存进去的文档一致。"""
    space = store.create("分享画布", doc=_doc(_node()))
    assert store.share_of(space["id"]) is None, "没分享过时 GET 不该凭空造 token"

    share = store.share(space["id"])
    assert share is not None
    assert share["space_id"] == space["id"]
    assert re.fullmatch(r"sh_[0-9a-f]{32}", share["token"]), share["token"]
    assert share["url"] == f"/canvas/?share={share['token']}"

    resolved = store.resolve_share(share["token"])
    assert resolved is not None
    assert resolved["space"]["id"] == space["id"]
    assert resolved["doc"] == _doc(_node())
    assert resolved["share"]["token"] == share["token"]
    assert resolved["share"]["space_id"] == space["id"]


def test_reshare_rotates_token_and_kills_the_old_one(store):
    """同一空间只保留一个活跃 token：再分享一次，旧链接立即失效。"""
    space = store.create("轮换")
    first = store.share(space["id"])
    second = store.share(space["id"])

    assert first["token"] != second["token"]
    assert store.resolve_share(first["token"]) is None, "旧 token 必须失效"
    assert store.resolve_share(second["token"]) is not None
    assert store.share_of(space["id"])["token"] == second["token"]


def test_share_token_survives_store_reopen(tmp_path):
    """token 是落库的，不是进程内内存态：重启 bridge 后分享链接要继续能读。"""
    first = CanvasStore(tmp_path / "share.db")
    space = first.create("重启后仍可读", doc=_doc(_node()))
    token = first.share(space["id"])["token"]
    first.close()

    second = CanvasStore(tmp_path / "share.db")
    try:
        resolved = second.resolve_share(token)
        assert resolved is not None
        assert resolved["space"]["name"] == "重启后仍可读"
        assert resolved["doc"]["nodes"][0]["id"] == "n_aaaaaaaaaa"
    finally:
        second.close()


def test_revoke_share_is_idempotent(store):
    space = store.create("撤销")
    token = store.share(space["id"])["token"]
    assert store.revoke_share(token) is True
    assert store.resolve_share(token) is None
    assert store.revoke_share(token) is False, "重复撤销返回 False，但不抛错"
    assert store.revoke_share("sh_" + "0" * 32) is False


def test_share_unknown_space_returns_none(store):
    assert store.share("sp_00000000") is None


def test_delete_space_also_deletes_its_share(store):
    """删空间必须连带删 token：留着 token 行等于给"已删除的画布"留一把钥匙。"""
    space = store.create("删前分享")
    token = store.share(space["id"])["token"]
    assert store.delete(space["id"]) is True
    assert store.resolve_share(token) is None
    assert store.revoke_share(token) is False, "删除时 token 行应已被清掉"
