"""账号库：SQLite + Fernet 加密。

muse 的凭据不是 Cookie 串，而是"哪个 Chrome profile + 哪个 CDP 端点"，
以及会话元信息。CDP 端点本身不敏感，但 profile 路径与账号标识要保护。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .state import AccountState

KEY_ENV = "MUSE_BRIDGE_KEY"
DB_ENV = "MUSE_BRIDGE_DB"
DEFAULT_DB = "./data/muse.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account          TEXT PRIMARY KEY,
    label            TEXT NOT NULL DEFAULT '',
    state            TEXT NOT NULL,
    cdp_url          TEXT NOT NULL,
    profile_enc      BLOB,
    vm_ready         INTEGER NOT NULL DEFAULT 0,
    refreshed_at     INTEGER,
    last_probe_ok_at INTEGER,
    last_error       TEXT,
    probe_attempts   INTEGER NOT NULL DEFAULT 0,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    last_failure_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_muse_state ON accounts(state);
CREATE TABLE IF NOT EXISTS tasks (
    task_id      TEXT PRIMARY KEY,
    account      TEXT,
    kind         TEXT NOT NULL,
    state        TEXT NOT NULL,
    prompt       TEXT NOT NULL,
    params_enc   BLOB,
    media_token  TEXT,
    error        TEXT,
    progress     INTEGER NOT NULL DEFAULT 0,
    retries      INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL,
    expires_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_muse_tasks_account ON tasks(account);
CREATE INDEX IF NOT EXISTS idx_muse_tasks_state ON tasks(state);
"""


class ConfigError(RuntimeError):
    pass


def load_cipher() -> Fernet:
    raw = os.environ.get(KEY_ENV, "").strip()
    if not raw:
        raise ConfigError(
            f"缺少 {KEY_ENV}。生成方式：\n"
            '  python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    try:
        return Fernet(raw.encode())
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"{KEY_ENV} 不是合法的 Fernet 密钥") from exc


@dataclass(frozen=True)
class Account:
    account: str
    label: str
    state: AccountState
    cdp_url: str
    vm_ready: bool
    refreshed_at: int | None
    last_probe_ok_at: int | None
    last_error: str | None
    probe_attempts: int
    created_at: int
    updated_at: int
    last_failure_at: int | None = None


class AccountStore:
    def __init__(self, path: str | Path | None = None) -> None:
        resolved = str(path or os.environ.get(DB_ENV, DEFAULT_DB))
        self.path = Path(resolved)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cipher = load_cipher()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._ensure_columns()
        self._ensure_task_columns()
        self._conn.commit()

    def _ensure_columns(self) -> None:
        """增列迁移：PRAGMA 探测，缺了就 ALTER。

        last_failure_at 是冷却时钟（只有 mark_failure 能重置）。老库回填成
        updated_at——那正是旧实现实际使用的时钟，回填保持存量行语义不变。
        """
        columns = {row[1] for row in self._conn.execute('PRAGMA table_info(accounts)')}
        if 'last_failure_at' not in columns:
            self._conn.execute('ALTER TABLE accounts ADD COLUMN last_failure_at INTEGER')
            self._conn.execute(
                'UPDATE accounts SET last_failure_at=updated_at WHERE last_failure_at IS NULL'
            )
            self._conn.commit()

    def _encrypt(self, data: dict[str, object]) -> bytes:
        return self._cipher.encrypt(json.dumps(data, ensure_ascii=False).encode())

    def _decrypt(self, blob: bytes | None) -> dict[str, object]:
        if not blob:
            return {}
        try:
            return json.loads(self._cipher.decrypt(blob).decode())
        except (InvalidToken, ValueError):
            return {}

    def upsert(
        self,
        account: str,
        *,
        cdp_url: str,
        state: AccountState,
        label: str = "",
        profile: dict[str, object] | None = None,
        vm_ready: bool = False,
    ) -> None:
        now = int(time.time())
        payload = self._encrypt(profile or {})
        self._conn.execute(
            """
            INSERT INTO accounts (
                account, label, state, cdp_url, profile_enc, vm_ready,
                refreshed_at, last_probe_ok_at, last_error,
                probe_attempts, created_at, updated_at, last_failure_at
            ) VALUES (?,?,?,?,?,?,?,?,NULL,0,?,?,?)
            ON CONFLICT(account) DO UPDATE SET
                label            = excluded.label,
                state            = excluded.state,
                cdp_url          = excluded.cdp_url,
                profile_enc      = excluded.profile_enc,
                vm_ready         = excluded.vm_ready,
                last_probe_ok_at = excluded.last_probe_ok_at,
                last_error       = NULL,
                updated_at       = excluded.updated_at,
                -- 冷却时钟只有 mark_failure 能重置；这里只保证 retryable 行不为 NULL
                last_failure_at  = COALESCE(accounts.last_failure_at, excluded.updated_at)
            """,
            (
                account,
                label,
                state.value,
                cdp_url,
                payload,
                1 if vm_ready else 0,
                now,
                now,
                now,
                now,
                now,
            ),
        )
        self._conn.commit()

    def mark_ready(self, account: str, *, vm_ready: bool, state: AccountState) -> None:
        now = int(time.time())
        self._conn.execute(
            """
            UPDATE accounts SET state=?, vm_ready=?, last_probe_ok_at=?,
                last_error=NULL, updated_at=? WHERE account=?
            """,
            (state.value, 1 if vm_ready else 0, now, now, account),
        )
        self._conn.commit()

    def mark_failure(self, account: str, *, state: AccountState, error: str) -> None:
        now = int(time.time())
        self._conn.execute(
            """
            UPDATE accounts SET state=?, last_error=?,
                probe_attempts=probe_attempts+1, updated_at=?, last_failure_at=?
            WHERE account=?
            """,
            (state.value, error, now, now, account),
        )
        self._conn.commit()

    def touch_refresh(self, account: str) -> None:
        now = int(time.time())
        self._conn.execute(
            "UPDATE accounts SET refreshed_at=?, updated_at=? WHERE account=?",
            (now, now, account),
        )
        self._conn.commit()

    def get(self, account: str) -> Account | None:
        row = self._conn.execute(
            "SELECT * FROM accounts WHERE account=?", (account,)
        ).fetchone()
        return _row_to_account(row) if row else None

    def profile(self, account: str) -> dict[str, object]:
        row = self._conn.execute(
            "SELECT profile_enc FROM accounts WHERE account=?", (account,)
        ).fetchone()
        return self._decrypt(row["profile_enc"]) if row else {}

    #: profile 里存 cookie 的键。放在加密列里，不新增表列——存量库无需迁移。
    COOKIES_KEY = "cookies"

    def save_cookies(self, account: str, cookies: Mapping[str, str]) -> bool:
        """把外部导入的 cookie 并进加密 profile。账号不存在返回 False。

        为什么要落库：纯协议路径（`protocol_client` / `session_http`）只需要
        cookie 就能跑，不需要 CDP。没有这一格，浏览器一关账号就彻底动不了。
        只存**非空**键值对：空串会在拼 Cookie 头时留下 `hatch_sess=;` 这种尾巴。
        """
        if self.get(account) is None:
            return False
        clean = {
            str(name).strip(): str(value).strip()
            for name, value in dict(cookies or {}).items()
            if str(name).strip() and str(value).strip()
        }
        profile = self.profile(account)
        profile[self.COOKIES_KEY] = clean
        now = int(time.time())
        self._conn.execute(
            "UPDATE accounts SET profile_enc=?, updated_at=? WHERE account=?",
            (self._encrypt(profile), now, account),
        )
        self._conn.commit()
        return True

    def cookies(self, account: str) -> dict[str, str]:
        """读回导入的 cookie。没有就返回空 dict（调用方据此决定走浏览器还是 HTTP）。"""
        raw = self.profile(account).get(self.COOKIES_KEY)
        if not isinstance(raw, dict):
            return {}
        return {str(name): str(value) for name, value in raw.items()}

    def list_all(self) -> list[Account]:
        rows = self._conn.execute("SELECT * FROM accounts ORDER BY account").fetchall()
        return [_row_to_account(row) for row in rows]

    def dispatchable(self) -> list[Account]:
        from .state import is_dispatchable

        now = int(time.time())
        return [
            record
            for record in self.list_all()
            if is_dispatchable(
                record.state,
                now=now,
                last_probe_ok_at=record.last_probe_ok_at,
                last_failure_at=record.last_failure_at,
            )
        ]

    def redacted(self) -> list[dict[str, object]]:
        return [
            {
                "account": record.account,
                "label": record.label,
                "state": record.state.value,
                "cdp_url": record.cdp_url,
                "vm_ready": record.vm_ready,
                "refreshed_at": record.refreshed_at,
                "last_probe_ok_at": record.last_probe_ok_at,
                "last_error": record.last_error,
                "probe_attempts": record.probe_attempts,
                "updated_at": record.updated_at,
                "last_failure_at": record.last_failure_at,
            }
            for record in self.list_all()
        ]

    def delete(self, account: str) -> bool:
        cursor = self._conn.execute("DELETE FROM accounts WHERE account=?", (account,))
        self._conn.commit()
        return cursor.rowcount > 0

    def close(self) -> None:
        self._conn.close()

    # ---- 任务表：SQL 与加解密在这一层，状态机在 jobs.py ----
    #
    # 三条约定：
    #   1. 时间戳一律由本层用 `time.time()` 生成（测试冻结 store_mod.time 即可
    #      整体控制时钟，与 accounts 的冷却时钟同一个手法）。
    #   2. 写路径带 CAS：`WHERE task_id=? AND state=?`，rowcount 为 0 就是
    #      被别人抢先改过，调用方必须知道——静默覆盖会让两个 worker 对同一个
    #      任务给出两个结论，而客户端只看到后落盘的那一个。
    #   3. `close()` 之后不得再调用任何方法（既有约定，靠 sqlite3 自己抛
    #      ProgrammingError 兜住，本层不另加状态位）。

    def _ensure_task_columns(self) -> None:
        """任务表增列迁移：PRAGMA 探测，缺了就 ALTER（照 _ensure_columns 的写法）。

        存量库里的 tasks 表可能来自更早的版本；CREATE TABLE IF NOT EXISTS
        对已存在的表不做任何事，所以新列必须靠这里补，否则读取时 KeyError
        只会出现在生产环境——本地永远是新建库。
        """
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(tasks)")}
        if not columns:
            return  # 表还没建（老库首次升级由 executescript 建好），无需迁移
        for name, declaration in TASK_COLUMN_ADDITIONS:
            if name not in columns:
                self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {declaration}")
        self._conn.commit()

    def insert_task(
        self,
        task_id: str,
        *,
        kind: str,
        prompt: str,
        params: dict[str, object] | None = None,
        account: str = "",
        state: str,
        ttl_seconds: int,
    ) -> None:
        """插入一条任务行。`expires_at = now + ttl_seconds` 就是队列预算的截止时刻。"""
        now = int(time.time())
        self._conn.execute(
            """
            INSERT INTO tasks (
                task_id, account, kind, state, prompt, params_enc,
                media_token, error, progress, retries,
                created_at, updated_at, expires_at
            ) VALUES (?,?,?,?,?,?,NULL,NULL,0,0,?,?,?)
            """,
            (
                task_id,
                account,
                kind,
                state,
                prompt,
                self._encrypt(dict(params or {})),
                now,
                now,
                now + int(ttl_seconds),
            ),
        )
        self._conn.commit()

    def get_task(self, task_id: str) -> dict[str, object] | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        return _task_payload(row, self._decrypt) if row else None

    def list_tasks(
        self,
        *,
        kind: str | None = None,
        state: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        """按创建时间倒序（最新的先给），与上游 list_tasks 的排序一致。

        tiebreaker 用 `rowid` 而不是 `task_id`：id 是随机 hex，同一秒内创建的
        两条任务 created_at 完全相同（测试里时钟被冻结时更是如此），按 id 排
        等于抓阄——`limit=1` 拿到哪条不确定，客户端会看到"最新任务"抖来抖去。
        rowid 是 SQLite 的插入序，天然单调。
        """
        clauses: list[str] = []
        values: list[object] = []
        if kind:
            clauses.append("kind=?")
            values.append(kind)
        if state:
            clauses.append("state=?")
            values.append(state)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM tasks {where} ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (*values, max(0, int(limit))),
        ).fetchall()
        return [_task_payload(row, self._decrypt) for row in rows]

    def count_tasks(self, states: tuple[str, ...] = ()) -> int:
        """排队条数用得上：队满要直接 429，不能无限收任务。"""
        if states:
            marks = ",".join("?" for _ in states)
            cursor = self._conn.execute(
                f"SELECT COUNT(*) FROM tasks WHERE state IN ({marks})", states
            )
        else:
            cursor = self._conn.execute("SELECT COUNT(*) FROM tasks")
        return int(cursor.fetchone()[0])

    def update_task(
        self,
        task_id: str,
        *,
        expected_state: str,
        fields: dict[str, object],
    ) -> bool:
        """CAS 更新。返回 False 表示行不在或状态已被改动（调用方必须处理）。

        只认 `TASK_COLUMNS` 里的列名：拼 SQL 前过滤一次，写错列名就是 KeyError，
        而不是"悄悄没写进去"。
        """
        columns: list[str] = []
        values: list[object] = []
        for name, value in fields.items():
            if name not in TASK_COLUMNS:
                raise KeyError(f"未知的任务列：{name}")
            columns.append(f"{name}=?")
            values.append(value)
        if not columns:
            return self.get_task(task_id) is not None
        columns.append("updated_at=?")
        values.append(int(time.time()))
        values.extend((task_id, expected_state))
        cursor = self._conn.execute(
            f"UPDATE tasks SET {', '.join(columns)} WHERE task_id=? AND state=?",
            values,
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def mark_task_interrupted(self, task_id: str, *, error: str) -> bool:
        """唯一的 CAS 旁路：只把 processing 判成 failed，别的状态一概不动。

        为什么要旁路：jobs.py 的 `update()/finish()` 禁止"终态再改"，
        而对账路径恰恰需要跳过状态机直接收尸。把旁路做成一个具名方法、
        并且 WHERE 里写死 state='processing'，是为了让这条捷径只有一种用法；
        想要更宽的口子就必须显式改这里，改不动就漏不掉。
        """
        now = int(time.time())
        cursor = self._conn.execute(
            "UPDATE tasks SET state='failed', error=?, updated_at=? "
            "WHERE task_id=? AND state='processing'",
            (error, now, task_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def prune_tasks(
        self,
        *,
        now: int | None = None,
        keep: int = 0,
        ttl_seconds: int | None = None,
    ) -> int:
        """删除超龄的**终态**任务行，并保留最新 `keep` 条。

        只清终态：queued/processing 还活着，删掉等于把任务凭空抹掉，
        客户端轮询只会得到 404——那比留一行旧数据糟糕得多。
        """
        current = int(time.time()) if now is None else int(now)
        budget = (
            DEFAULT_TASK_ROW_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
        )
        rows = self._conn.execute(
            "SELECT task_id, state, updated_at FROM tasks "
            "ORDER BY created_at DESC, task_id DESC"
        ).fetchall()
        protected = {row["task_id"] for row in rows[: max(0, int(keep))]}
        doomed = [
            row["task_id"]
            for row in rows
            if row["task_id"] not in protected
            and row["state"] in TASK_TERMINAL_STATES
            and current - int(row["updated_at"] or 0) >= budget
        ]
        for task_id in doomed:
            self._conn.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
        if doomed:
            self._conn.commit()
        return len(doomed)

    def delete_task(self, task_id: str) -> bool:
        cursor = self._conn.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
        self._conn.commit()
        return cursor.rowcount > 0


TASK_COLUMNS = frozenset(
    {
        "account",
        "kind",
        "state",
        "prompt",
        "params_enc",
        "media_token",
        "error",
        "progress",
        "retries",
        "expires_at",
    }
)

# 终态集合与 jobs.TERMINAL_STATES 同值。这里重复一份是有意的：store 不能反向
# 依赖 jobs（jobs 已经依赖 store，循环导入会在 app 启动时炸），
# 而且清理判据宁可写死也不要因为一次重构悄悄变成"什么都能删"。
TASK_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})

# 兜底用的默认保留时长；正常调用方（jobs.TaskStore）会把自己那份配置传进来。
DEFAULT_TASK_ROW_TTL_SECONDS = 24 * 3600

# 存量 tasks 表可能需要补的列：(列名, 声明)。新增列往这里加，不要改 SCHEMA 了事。
TASK_COLUMN_ADDITIONS: tuple[tuple[str, str], ...] = (
    ("retries", "INTEGER NOT NULL DEFAULT 0"),
    ("expires_at", "INTEGER"),
)


def _task_payload(row: sqlite3.Row, decrypt) -> dict[str, object]:
    """行 -> 对外字典。params 在这里解密，调用方拿到的就是纯 dict。

    `decrypt` 作为参数传入（而不是去碰 `AccountStore._decrypt`）：
    这样模块级函数保持无状态，测试里可以塞一个假解密器验证异常路径。
    """
    payload: dict[str, object] = {
        "task_id": row["task_id"],
        "account": row["account"] or "",
        "kind": row["kind"],
        "state": row["state"],
        "prompt": row["prompt"],
        "media_token": row["media_token"] or "",
        "error": row["error"] or "",
        "progress": int(row["progress"] or 0),
        "retries": int(row["retries"] or 0),
        "created_at": int(row["created_at"] or 0),
        "updated_at": int(row["updated_at"] or 0),
        "expires_at": row["expires_at"],
        "params": decrypt(row["params_enc"]),
    }
    return payload


def _row_to_account(row: sqlite3.Row) -> Account:
    return Account(
        account=row["account"],
        label=row["label"] or "",
        state=AccountState(row["state"]),
        cdp_url=row["cdp_url"],
        vm_ready=bool(row["vm_ready"]),
        refreshed_at=row["refreshed_at"],
        last_probe_ok_at=row["last_probe_ok_at"],
        last_error=row["last_error"],
        probe_attempts=row["probe_attempts"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_failure_at=row["last_failure_at"],
    )
