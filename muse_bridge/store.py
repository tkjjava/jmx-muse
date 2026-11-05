"""账号库：SQLite + Fernet 加密。

muse 的凭据不是 Cookie 串，而是"哪个 Chrome profile + 哪个 CDP 端点"，
以及会话元信息。CDP 端点本身不敏感，但 profile 路径与账号标识要保护。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
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
    updated_at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_muse_state ON accounts(state);
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


class AccountStore:
    def __init__(self, path: str | Path | None = None) -> None:
        resolved = str(path or os.environ.get(DB_ENV, DEFAULT_DB))
        self.path = Path(resolved)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cipher = load_cipher()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
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
                probe_attempts, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,NULL,0,?,?)
            ON CONFLICT(account) DO UPDATE SET
                label            = excluded.label,
                state            = excluded.state,
                cdp_url          = excluded.cdp_url,
                profile_enc      = excluded.profile_enc,
                vm_ready         = excluded.vm_ready,
                last_probe_ok_at = excluded.last_probe_ok_at,
                last_error       = NULL,
                updated_at       = excluded.updated_at
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
            ),
        )
        self._conn.commit()

    def mark_ready(
        self, account: str, *, vm_ready: bool, state: AccountState
    ) -> None:
        now = int(time.time())
        self._conn.execute(
            """
            UPDATE accounts SET state=?, vm_ready=?, last_probe_ok_at=?,
                last_error=NULL, updated_at=? WHERE account=?
            """,
            (state.value, 1 if vm_ready else 0, now, now, account),
        )
        self._conn.commit()

    def mark_failure(
        self, account: str, *, state: AccountState, error: str
    ) -> None:
        now = int(time.time())
        self._conn.execute(
            """
            UPDATE accounts SET state=?, last_error=?,
                probe_attempts=probe_attempts+1, updated_at=?
            WHERE account=?
            """,
            (state.value, error, now, account),
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

    def list_all(self) -> list[Account]:
        rows = self._conn.execute(
            "SELECT * FROM accounts ORDER BY account"
        ).fetchall()
        return [_row_to_account(row) for row in rows]

    def dispatchable(self) -> list[Account]:
        from .state import is_dispatchable

        now = int(time.time())
        return [
            record
            for record in self.list_all()
            if is_dispatchable(
                record.state, now=now, last_probe_ok_at=record.last_probe_ok_at
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
            }
            for record in self.list_all()
        ]

    def delete(self, account: str) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM accounts WHERE account=?", (account,)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def close(self) -> None:
        self._conn.close()


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
    )
