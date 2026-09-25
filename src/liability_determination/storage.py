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

-- 事故案件
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    case_number TEXT NOT NULL,
    title TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    location TEXT NOT NULL,
    -- 人员伤亡标记：出现人员伤亡时责任认定需要负责人终审
    casualties INTEGER NOT NULL DEFAULT 0 CHECK (casualties IN (0, 1)),
    -- 证据链异常标记：证据相互矛盾、缺失关键环节时需要负责人终审
    evidence_anomaly INTEGER NOT NULL DEFAULT 0 CHECK (evidence_anomaly IN (0, 1)),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

-- 事故当事方
CREATE TABLE IF NOT EXISTS parties (
    party_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('driver', 'pedestrian', 'cyclist', 'vehicle_owner', 'other')),
    contact TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (case_id, party_id)
);

-- 汇总进认定的证据材料：当事人陈述、车辆轨迹、现场证据、法规依据
CREATE TABLE IF NOT EXISTS evidence_materials (
    material_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    material_type TEXT NOT NULL
        CHECK (material_type IN ('statement', 'trajectory', 'scene', 'regulation')),
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    source_ref TEXT,
    recorded_by TEXT NOT NULL REFERENCES users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE (case_id, material_id)
);

-- 责任认定草案（一个案件只有一份草案，下含不可变版本）
CREATE TABLE IF NOT EXISTS determination_drafts (
    draft_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL UNIQUE REFERENCES cases(case_id),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

-- 不可变版本。current=1 的版本是当前版本；修订后旧版本 current 置 0，
-- 其签署保留但签署状态置为 superseded（意见本身不删除）。
CREATE TABLE IF NOT EXISTS determination_versions (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id TEXT NOT NULL REFERENCES determination_drafts(draft_id),
    version_no INTEGER NOT NULL CHECK (version_no > 0),
    -- draft：主办编辑中；submitted：已提交待签署；
    -- rejected：签署被驳回；effective：所需层级签署完成并生效；
    -- superseded：曾生效但已被更新的当前版本取代
    status TEXT NOT NULL CHECK (status IN ('draft', 'submitted', 'rejected', 'effective', 'superseded')),
    basis_note TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    is_current INTEGER NOT NULL CHECK (is_current IN (0, 1)),
    change_summary TEXT,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    submitted_at TEXT,
    UNIQUE (draft_id, version_no)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_current_version_per_draft
ON determination_versions(draft_id) WHERE is_current = 1;

-- 每个版本对每一方的责任认定
CREATE TABLE IF NOT EXISTS version_parties (
    version_id INTEGER NOT NULL REFERENCES determination_versions(version_id),
    party_id TEXT NOT NULL,
    responsibility_ratio INTEGER NOT NULL CHECK (responsibility_ratio BETWEEN 0 AND 100),
    finding TEXT NOT NULL CHECK (finding IN ('full', 'primary', 'equal', 'secondary', 'minor', 'none')),
    reasoning TEXT NOT NULL,
    PRIMARY KEY (version_id, party_id)
);

-- 每个版本引用的证据材料（认定结论的引用依据）
CREATE TABLE IF NOT EXISTS version_materials (
    version_id INTEGER NOT NULL REFERENCES determination_versions(version_id),
    material_id TEXT NOT NULL,
    material_type TEXT NOT NULL,
    cited_note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (version_id, material_id)
);

-- 多级签署：review（复核人签署）/ final（负责人终审）
CREATE TABLE IF NOT EXISTS version_signatures (
    signature_id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES determination_versions(version_id),
    level TEXT NOT NULL CHECK (level IN ('review', 'final')),
    -- active：对当前版本有效；superseded：因修订失效，意见仍保留
    status TEXT NOT NULL CHECK (status IN ('active', 'superseded')),
    opinion TEXT NOT NULL CHECK (opinion IN ('agree', 'reject')),
    comment TEXT NOT NULL DEFAULT '',
    signer_id TEXT NOT NULL REFERENCES users(user_id),
    signed_at TEXT NOT NULL,
    superseded_at TEXT,
    UNIQUE (version_id, level)
);

-- 生效决定与送达文本：一个版本至多一份，重复签署不会生成第二份
CREATE TABLE IF NOT EXISTS determination_effects (
    effect_id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL UNIQUE REFERENCES determination_versions(version_id),
    draft_id TEXT NOT NULL REFERENCES determination_drafts(draft_id),
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    document_no TEXT NOT NULL UNIQUE,
    document_text TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    -- 被更新版本取代时填写；原送达文本仍保留可查
    superseded_at TEXT
);

-- 状态变化与签署留痕（查询时用于解释状态变化）
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
    "schema_meta", "users", "cases", "parties", "evidence_materials",
    "determination_drafts", "determination_versions", "version_parties",
    "version_materials", "version_signatures", "determination_effects",
    "audit_events",
})


def connect(path: str | Path = ":memory:", *, check_same_thread: bool = True) -> sqlite3.Connection:
    """打开连接并启用外键与显式事务模式。

    HTTP 服务按工作线程分发请求时传入 check_same_thread=False，
    并保证每个线程使用自己的连接（见 api 中的线程局部连接）。
    """

    connection = sqlite3.connect(
        str(path), isolation_level=None, check_same_thread=check_same_thread
    )
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
    """初始化数据表，重复执行不改变已有数据。"""

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
