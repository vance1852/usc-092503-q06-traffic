"""责任认定服务的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('investigator', 'reviewer', 'chief', 'auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS accidents (
    accident_id TEXT PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    location TEXT NOT NULL,
    summary TEXT NOT NULL,
    has_casualty INTEGER NOT NULL DEFAULT 0 CHECK (has_casualty IN (0, 1)),
    evidence_chain_anomaly INTEGER NOT NULL DEFAULT 0 CHECK (evidence_chain_anomaly IN (0, 1)),
    registered_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accident_parties (
    party_id TEXT NOT NULL,
    accident_id TEXT NOT NULL REFERENCES accidents(accident_id),
    name TEXT NOT NULL,
    party_kind TEXT NOT NULL,
    contact TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (accident_id, party_id)
);

CREATE TABLE IF NOT EXISTS liability_drafts (
    draft_id TEXT PRIMARY KEY,
    accident_id TEXT NOT NULL UNIQUE REFERENCES accidents(accident_id),
    current_version_no INTEGER NOT NULL CHECK (current_version_no >= 1),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS liability_versions (
    version_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES liability_drafts(draft_id),
    accident_id TEXT NOT NULL REFERENCES accidents(accident_id),
    version_no INTEGER NOT NULL CHECK (version_no >= 1),
    state TEXT NOT NULL CHECK (state IN ('draft', 'submitted', 'returned', 'signed', 'effective', 'superseded')),
    trigger_reason TEXT NOT NULL,
    change_note TEXT,
    content_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    share_changed INTEGER NOT NULL DEFAULT 0 CHECK (share_changed IN (0, 1)),
    materials_changed INTEGER NOT NULL DEFAULT 0 CHECK (materials_changed IN (0, 1)),
    final_required INTEGER NOT NULL CHECK (final_required IN (0, 1)),
    submitted_by TEXT REFERENCES users(user_id),
    submitted_at TEXT,
    effective_at TEXT,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE (draft_id, version_no)
);

CREATE TABLE IF NOT EXISTS version_signings (
    signing_id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id TEXT NOT NULL REFERENCES liability_versions(version_id),
    level TEXT NOT NULL CHECK (level IN ('submit', 'review', 'final')),
    action TEXT NOT NULL CHECK (action IN ('signed', 'returned')),
    signer_id TEXT NOT NULL REFERENCES users(user_id),
    opinion TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    status TEXT NOT NULL DEFAULT 'valid' CHECK (status IN ('valid', 'invalidated')),
    signed_at TEXT NOT NULL,
    invalidated_at TEXT,
    invalidated_reason TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_valid_signing_per_level
ON version_signings(version_id, level)
WHERE status = 'valid' AND action = 'signed';

CREATE TABLE IF NOT EXISTS service_documents (
    document_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL UNIQUE REFERENCES liability_versions(version_id),
    draft_id TEXT NOT NULL REFERENCES liability_drafts(draft_id),
    accident_id TEXT NOT NULL REFERENCES accidents(accident_id),
    document_no TEXT NOT NULL UNIQUE,
    document_text TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    generated_by TEXT NOT NULL REFERENCES users(user_id),
    generated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

REQUIRED_TABLES = frozenset({
    "schema_meta", "users", "accidents", "accident_parties", "liability_drafts",
    "liability_versions", "version_signings", "service_documents", "audit_events",
})


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接并启用严格的事务与外键设置。

    HTTP 服务以多线程方式共用连接，因此关闭同线程限制；调用方需自行串行化
    （见 api.JsonApplication 的请求锁）。
    """

    connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """显式事务；异常时保证回滚。"""

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize(connection: sqlite3.Connection) -> None:
    """初始化基础资料表，重复执行不改变已有数据。"""

    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    """返回适合机器检查的数据库结构摘要。"""

    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    missing = sorted(REQUIRED_TABLES - set(tables))
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    return {
        "tables": tables,
        "missing_tables": missing,
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(foreign_keys),
    }
