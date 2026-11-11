"""画布空间存储：一张 SQLite 表，只存 JSON 文档，不引 ORM。

为什么不用 ORM：画布文档是**前端定义的版本化 JSON**（`docs/CANVAS_DESIGN.md`
§4），服务端对它唯一的硬需求是"原样存、原样取"。ORM 会把 `nodes` / `edges` /
`data` 拆成关系表，于是前端每加一个未知字段（契约明确要求"未知字段保留并
透传"）都要先改一次表结构与迁移脚本，而服务端根本不需要理解这些字段。这里
把整份文档当一个不透明 blob 存进 `doc_json`，只对**上线即冻结**的少数约束
做校验，其余一律透传。

为什么不碰其它表：`canvas_spaces` 建在 bridge 现有的同一个数据库文件里
（`MUSE_BRIDGE_DB`，默认 `./data/muse.db`），备份/迁移只要搬一个文件。但账号
与任务属于另一条业务线（凭据加密、状态机、CAS 更新），画布既不该读也不该写
它们——本模块只有 `canvas_spaces` / `canvas_shares` 两张表的 DDL（分享 token
是画布自己的产品能力，与账号凭据无关）。

为什么 `updated_at` 严格递增（即使同一秒内连续保存）：前端用
`(id, updated_at)` 判断"手里这份是不是最新文档"。两个标签页/两台设备对同一
空间先后保存时，只有 `updated_at` 变了，前端才能看出"文档被别处改过"。
而 `time.time()` 是秒级的，同一秒内的两次保存会给出相同值——前端就分不清
新旧，会把旧文档当新文档覆盖回去，而且事后无法审计谁改了什么。所以写入时取
`updated_at = max(now, old + 1)`：对外仍是秒级时间戳，但保证每次成功写入都
**严格大于**上一次。`created_at` 不参与这个约定。

连接与并发：一个实例 = 一个连接（`check_same_thread=False`）+ 一把模块内的
`threading.Lock`，所有 SQL 都在锁内完成，换连接换事务的写法在这里没有收益。
SQLite 打开 `journal_mode=WAL`，读不挡写。SQL 全参数化（`?` 占位），本模块
不存在把外部字符串拼进语句的路径。
"""

from __future__ import annotations

import json
import math
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .store import DB_ENV, DEFAULT_DB  # noqa: F401  (DB_ENV/DEFAULT_DB 供 API 层复用)

DEFAULT_SPACE_NAME = "未命名画布"
MAX_NAME_LENGTH = 120
MAX_NODES = 2000
MAX_EDGES = 4000
MAX_DOC_BYTES = 4 * 1024 * 1024
NODE_TYPES = frozenset({"image", "video", "prompt", "text"})
ZOOM_MIN = 0.05
ZOOM_MAX = 8.0

SPACE_ID_RE = re.compile(r"sp_[a-z0-9]{8}")
NODE_ID_RE = re.compile(r"n_[a-z0-9]{10}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS canvas_spaces(
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    doc_json   TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    source     TEXT NOT NULL DEFAULT 'local'
);
CREATE TABLE IF NOT EXISTS canvas_shares(
    token      TEXT PRIMARY KEY,
    space_id   TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS canvas_shares_space ON canvas_shares(space_id);
"""


class CanvasDocError(ValueError):
    """文档不合法，或库里的 JSON 读不出来。

    消息本身就是回给前端的那句 `note`：故意写得具体（哪个节点、哪个字段、
    收到什么），因为画布前端没有服务端上下文，一句"invalid document"没法排障。
    """


def empty_doc() -> dict[str, Any]:
    """契约 §4/§6 的空画布初始态。每次返回新对象，调用方随便改。"""
    return {
        "version": 1,
        "camera": {"x": -200, "y": -120, "zoom": 1},
        "nodes": [],
        "edges": [],
        "meta": {"theme": "dark", "background": "dots", "gridSize": 28},
    }


def normalize_name(name: str | None) -> str:
    """去首尾空白；空名落到默认名。名称过长是**调用方的错**，直接报错而不是截断。"""
    cleaned = (name or "").strip()
    if len(cleaned) > MAX_NAME_LENGTH:
        raise ValueError(
            f"空间名最多 {MAX_NAME_LENGTH} 个字符（收到 {len(cleaned)} 个）"
        )
    return cleaned or DEFAULT_SPACE_NAME


def validate_doc(doc: Any) -> None:
    """按契约 §4 校验画布文档；**只读**，不补字段、不改调用方数据。

    不合法抛 `CanvasDocError`，调用方据此拒绝落库。未知字段一律放行——契约
    要的是"未来能扩展"，不是"服务端认识每个字段"。

    `data` 缺失是允许的（NodeData 全可选）；本函数不替调用方补 `{}`，因为
    "校验函数顺手改数据"会让"校验失败不落库"这条保证变得不可信。
    """
    if not isinstance(doc, dict):
        raise CanvasDocError(f"doc 必须是 JSON 对象（收到 {type(doc).__name__}）")

    version = doc.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise CanvasDocError(f"version 必须为 1（收到 {version!r}）")

    nodes = doc.get("nodes")
    if not isinstance(nodes, list):
        raise CanvasDocError(f"nodes 必须是数组（收到 {type(nodes).__name__}）")
    if len(nodes) > MAX_NODES:
        raise CanvasDocError(f"nodes 最多 {MAX_NODES} 个（收到 {len(nodes)} 个）")

    seen: set[str] = set()
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise CanvasDocError(f"node[{index}] 必须是对象")
        raw_id = node.get("id")
        if not isinstance(raw_id, str) or not raw_id:
            raise CanvasDocError(f"node[{index}]: 缺少 id")
        if NODE_ID_RE.fullmatch(raw_id) is None:
            raise CanvasDocError(
                f"node[{index}]: id {raw_id!r} 必须匹配 ^n_[a-z0-9]{{10}}$"
            )
        label = f"node {raw_id}"
        if raw_id in seen:
            raise CanvasDocError(f"{label}: 重复的节点 id")
        seen.add(raw_id)

        node_type = node.get("type")
        if not isinstance(node_type, str) or node_type not in NODE_TYPES:
            raise CanvasDocError(
                f"{label}: type {node_type!r} 必须是 image/video/prompt/text 之一"
            )

        for field in ("x", "y", "w", "h", "z"):
            value = _finite(node.get(field))
            if value is None:
                raise CanvasDocError(
                    f"{label}: {field} 必须是有限数（收到 {node.get(field)!r}）"
                )
            if field in ("w", "h") and value <= 0:
                raise CanvasDocError(f"{label}: {field} 必须 > 0（收到 {value}）")

        data = node.get("data")
        if data is not None and not isinstance(data, dict):
            raise CanvasDocError(f"{label}: data 必须是对象")

    edges = doc.get("edges")
    if not isinstance(edges, list):
        raise CanvasDocError(f"edges 必须是数组（收到 {type(edges).__name__}）")
    if len(edges) > MAX_EDGES:
        raise CanvasDocError(f"edges 最多 {MAX_EDGES} 条（收到 {len(edges)} 条）")

    for index, edge in enumerate(edges):
        if not isinstance(edge, dict):
            raise CanvasDocError(f"edge[{index}] 必须是对象")
        edge_id = edge.get("id")
        if not isinstance(edge_id, str) or not edge_id:
            raise CanvasDocError(f"edge[{index}]: 缺少 id")
        for end in ("from", "to"):
            ref = edge.get(end)
            if not isinstance(ref, str) or not ref:
                raise CanvasDocError(f"edge[{index}]: 缺少 {end}")
            if ref not in seen:
                raise CanvasDocError(f"edges 里 {end} 指向不存在的节点 {ref}")

    camera = doc.get("camera")
    if camera is not None:
        if not isinstance(camera, dict):
            raise CanvasDocError("camera 必须是对象")
        for axis in ("x", "y"):
            if _finite(camera.get(axis)) is None:
                raise CanvasDocError(
                    f"camera.{axis} 必须是有限数（收到 {camera.get(axis)!r}）"
                )
        zoom = _finite(camera.get("zoom"))
        if zoom is None or not ZOOM_MIN <= zoom <= ZOOM_MAX:
            raise CanvasDocError(
                f"camera.zoom 必须在 [{ZOOM_MIN}, {ZOOM_MAX}] 之间"
                f"（收到 {camera.get('zoom')!r}）"
            )

    meta = doc.get("meta")
    if meta is not None and not isinstance(meta, dict):
        raise CanvasDocError("meta 必须是对象")

    try:
        encoded = json.dumps(doc, ensure_ascii=False).encode()
    except (TypeError, ValueError) as exc:
        raise CanvasDocError(f"doc 无法序列化成 JSON：{exc}") from exc
    if len(encoded) > MAX_DOC_BYTES:
        raise CanvasDocError(
            f"doc 序列化后 {len(encoded)} 字节，超过 "
            f"{MAX_DOC_BYTES // (1024 * 1024)} MB 上限"
        )


class CanvasStore:
    """画布空间读写。所有方法线程安全（内部一把锁串行化）。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL：读写不互挡。`:memory:` 上 SQLite 会自动降级成 memory 日志，不报错。
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA journal_mode = WAL").fetchone()
        self._conn.executescript(SCHEMA)

    # ---- 写 -----------------------------------------------------------

    def create(self, name: str, doc: dict | None = None) -> dict[str, Any]:
        """新建空间并返回 SpaceMeta。`doc=None` 即空画布；给了就先校验再落库。"""
        payload = empty_doc() if doc is None else doc
        validate_doc(payload)
        doc_json = _dump(payload)
        clean_name = normalize_name(name)
        now = int(time.time())
        with self._lock:
            space_id = self._allocate_id()
            self._conn.execute(
                "INSERT INTO canvas_spaces"
                " (id, name, doc_json, created_at, updated_at, source)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (space_id, clean_name, doc_json, now, now, "local"),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM canvas_spaces WHERE id = ?", (space_id,)
            ).fetchone()
        if row is None:  # pragma: no cover - 刚提交的行读不到只可能是库被换掉
            raise CanvasDocError(f"空间 {space_id} 写入后读回失败")
        return _meta(row, _load_doc(row))

    def update(
        self,
        space_id: str,
        *,
        name: str | None = None,
        doc: dict | None = None,
    ) -> dict[str, Any] | None:
        """整体覆盖 name/doc（传 None 的字段保持原值）。空间不存在返回 None。

        `updated_at` 由 SQL 里的 `MAX(now, old + 1)` 计算，不是先读再写：
        先读后写会在两个请求之间留一个窗口，窗口里的并发写会把时间戳推回去。
        """
        doc_json: str | None = None
        if doc is not None:
            validate_doc(doc)
            doc_json = _dump(doc)
        clean_name = normalize_name(name) if name is not None else None
        now = int(time.time())
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE canvas_spaces"
                " SET name = COALESCE(?, name),"
                "     doc_json = COALESCE(?, doc_json),"
                "     updated_at = MAX(?, updated_at + 1)"
                " WHERE id = ?",
                (clean_name, doc_json, now, space_id),
            )
            self._conn.commit()
            if cursor.rowcount == 0:
                return None
            row = self._conn.execute(
                "SELECT * FROM canvas_spaces WHERE id = ?", (space_id,)
            ).fetchone()
        if row is None:  # pragma: no cover - rowcount > 0 却读不到
            return None
        return _meta(row, _load_doc(row))

    def delete(self, space_id: str) -> bool:
        with self._lock:
            # 先删分享：否则空间删掉后，旧分享链接还能顺着 join 找到一条不存在的
            # 空间（读到 None）——用户会以为"撤销了分享"，但 token 行还留在库里，
            # 将来若 id 被复用（当前不会，将来可能改）就会复活一个旧链接。
            self._conn.execute(
                "DELETE FROM canvas_shares WHERE space_id = ?", (space_id,)
            )
            cursor = self._conn.execute(
                "DELETE FROM canvas_spaces WHERE id = ?", (space_id,)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    # ---- 只读分享 -------------------------------------------------------

    def share(self, space_id: str) -> dict[str, Any] | None:
        """为空间创建（或轮换）只读分享 token；空间不存在返回 None。

        每个空间**只保留一个活跃 token**：再次分享会先删旧 token。为什么轮换而不
        并存：并存的旧链接无法被"撤销分享"一次性收回——用户点了撤销，旧链接却
        还能读，等于分享收不回来。
        """
        token = f"sh_{secrets.token_hex(16)}"
        now = int(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM canvas_spaces WHERE id = ?", (space_id,)
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "DELETE FROM canvas_shares WHERE space_id = ?", (space_id,)
            )
            self._conn.execute(
                "INSERT INTO canvas_shares (token, space_id, created_at)"
                " VALUES (?, ?, ?)",
                (token, space_id, now),
            )
            self._conn.commit()
        return {
            "token": token,
            "space_id": space_id,
            "created_at": now,
            "url": f"/canvas/?share={token}",
        }

    def share_of(self, space_id: str) -> dict[str, Any] | None:
        """当前活跃分享；没有就返回 None（不自动创建——GET 不应有副作用）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT token, space_id, created_at FROM canvas_shares"
                " WHERE space_id = ? LIMIT 1",
                (space_id,),
            ).fetchone()
        if row is None:
            return None
        return dict(row) | {"url": f"/canvas/?share={row['token']}"}

    def resolve_share(self, token: str) -> dict[str, Any] | None:
        """token → `{"space": SpaceMeta, "doc": Doc, "share": {...}}`。

        无效/已撤销/空间已删一律返回 None（调用方统一回 404，不给"token 存在但
        空间没了"这类可探测的中间态）。只读：本模块没有任何基于 token 的写入口，
        写权限只属于 `/canvas/api/spaces/{id}`（要求 Bearer key）。
        """
        if not isinstance(token, str) or not token:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT s.*, sh.token AS share_token, sh.created_at AS share_created_at"
                " FROM canvas_shares sh JOIN canvas_spaces s ON s.id = sh.space_id"
                " WHERE sh.token = ?",
                (token,),
            ).fetchone()
        if row is None:
            return None
        doc = _load_doc(row)
        share = {
            "token": row["share_token"],
            "space_id": row["id"],
            "created_at": row["share_created_at"],
        }
        return {"space": _meta(row, doc), "doc": doc, "share": share}

    def revoke_share(self, token: str) -> bool:
        """撤销一个分享 token；幂等：不存在返回 False（不抛错）。"""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM canvas_shares WHERE token = ?", (token,)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    # ---- 读 -----------------------------------------------------------

    def list(self) -> list[dict[str, Any]]:
        """按 `updated_at DESC` 列出 SpaceMeta（不含 doc 正文）。

        tiebreaker 用 `rowid DESC`：同一秒内建的两个空间 `updated_at` 相同，只按
        时间排等于抓阄，列表顺序会抖。rowid 是插入序，天然稳定。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM canvas_spaces ORDER BY updated_at DESC, rowid DESC"
            ).fetchall()
        return [_meta(row, _load_doc(row)) for row in rows]

    def get(self, space_id: str) -> dict[str, Any] | None:
        """返回 `{"space": SpaceMeta, "doc": Doc}`；不存在返回 None。

        库里的 `doc_json` 坏掉时抛 `CanvasDocError`（而不是当成空画布）：
        把坏档静默读成空文档，前端下一次保存就会把用户内容彻底抹掉。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM canvas_spaces WHERE id = ?", (space_id,)
            ).fetchone()
        if row is None:
            return None
        doc = _load_doc(row)
        return {"space": _meta(row, doc), "doc": doc}

    def count(self) -> int:
        """空间条数。`/canvas/api/health` 用它，**不解析 doc**，坏档也不影响探测。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM canvas_spaces"
            ).fetchone()
        return int(row["n"])

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- 内部 ---------------------------------------------------------

    def _allocate_id(self) -> str:
        """在锁内挑一个未被占用的 id；撞号就换一个（哈希空间够大，几乎不会发生）。"""
        for _ in range(8):
            candidate = f"sp_{secrets.token_hex(4)}"
            row = self._conn.execute(
                "SELECT 1 FROM canvas_spaces WHERE id = ?", (candidate,)
            ).fetchone()
            if row is None:
                return candidate
        raise RuntimeError("无法分配未占用的画布空间 id")


def _finite(value: Any) -> float | None:
    """有限数才算数：bool、NaN、±Inf、超范围整数（float() 溢出）全部返回 None。

    返回 `None` 表示不合法而不是返回 0.0：`0.0` 是合法坐标，不能用它当哨兵。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _dump(doc: dict[str, Any]) -> str:
    return json.dumps(doc, ensure_ascii=False)


def _load_doc(row: sqlite3.Row) -> dict[str, Any]:
    raw = row["doc_json"]
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise CanvasDocError(
            f"空间 {row['id']} 的 doc_json 不是合法 JSON：{exc}"
        ) from exc
    if not isinstance(doc, dict):
        raise CanvasDocError(f"空间 {row['id']} 的 doc_json 不是 JSON 对象")
    return doc


def _meta(row: sqlite3.Row, doc: dict[str, Any]) -> dict[str, Any]:
    """行 + 已解析文档 -> 契约 §3.1 的 SpaceMeta。`thumbnail` 先行留空。"""
    return {
        "id": row["id"],
        "name": row["name"],
        "created_at": int(row["created_at"]),
        "updated_at": int(row["updated_at"]),
        "node_count": len(doc.get("nodes") or []),
        "thumbnail": None,
        "source": row["source"] or "local",
    }
