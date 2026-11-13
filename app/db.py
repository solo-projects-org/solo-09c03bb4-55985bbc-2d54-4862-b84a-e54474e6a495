"""SQLite 持久化：书目记录、采集进度、采集运行历史。

所有写操作在单连接单事务内完成，保证“整批采完才提交”。
"""
from __future__ import annotations

import os
import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

DB_PATH = os.environ.get("APP_DB_PATH", "/data/harvester.db")

logger = logging.getLogger("harvester.db")

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS bib_records (
    identifier      TEXT PRIMARY KEY,            -- OAI 记录唯一标识
    datestamp       TEXT NOT NULL,               -- OAI header datestamp
    status          TEXT NOT NULL,               -- active / withdrawn
    title           TEXT,
    creator         TEXT,
    publisher       TEXT,
    subject         TEXT,
    description     TEXT,
    date            TEXT,
    type            TEXT,
    format          TEXT,
    identifier_dc   TEXT,
    source          TEXT,
    language        TEXT,
    relation        TEXT,
    coverage        TEXT,
    rights          TEXT,
    raw_dc          TEXT NOT NULL,               -- 原始 Dublin Core JSON
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS harvest_progress (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    last_until      TEXT NOT NULL,               -- 上次成功采集的截止日（含）
    last_harvest_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS harvest_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    from_date       TEXT NOT NULL,
    until_date      TEXT NOT NULL,
    status          TEXT NOT NULL,               -- success / failed
    pages           INTEGER NOT NULL DEFAULT 0,
    fetched         INTEGER NOT NULL DEFAULT 0,
    inserted        INTEGER NOT NULL DEFAULT 0,
    updated         INTEGER NOT NULL DEFAULT 0,
    withdrawn       INTEGER NOT NULL DEFAULT 0,
    retries         INTEGER NOT NULL DEFAULT 0,
    error           TEXT
);

-- 分卷归属：每条书目一行，随采集事务增量重算。
-- 升级旧库时由 backfill_attributions 用现有目录补建，不触碰续采进度。
CREATE TABLE IF NOT EXISTS attributions (
    identifier        TEXT PRIMARY KEY,          -- 对应 bib_records.identifier
    parent_identifier TEXT,                      -- 直接上级 OAI 标识；总目为 NULL
    root_identifier   TEXT,                      -- 在架总目标识；未决为 NULL
    status            TEXT NOT NULL,             -- root/resolved/parent_missing/
                                                 -- parent_withdrawn/cycle
    reason            TEXT,                      -- 未决/成环原因
    chain             TEXT NOT NULL DEFAULT '[]', -- 上溯链 JSON（含起点）
    updated_at        TEXT NOT NULL,
    FOREIGN KEY (identifier) REFERENCES bib_records(identifier)
);

-- 按直接上级反查受影响下级（补采后只重算相关卷册，不重扫全库）
CREATE INDEX IF NOT EXISTS idx_attributions_parent
    ON attributions(parent_identifier);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(db_path: str | None = None) -> None:
    conn = get_connection(db_path)
    try:
        # 建表脚本含 IF NOT EXISTS，对旧库无副作用。
        # 注意：sqlite3 的 executescript 会先隐式提交，不能包进显式事务。
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        # 旧库升级：用现有目录补建缺失归属，独立事务，失败只回滚补建，
        # harvest_progress / harvest_runs 与续采进度均不改动。
        with transaction(conn):
            backfilled = backfill_attributions(conn)
            conn.commit()
        if backfilled:
            logger.info("旧目录归属补建完成：%d 条", backfilled)
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务上下文：异常回滚，正常时由调用方负责 commit。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
    except Exception:
        conn.rollback()
        raise


def get_last_until(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT last_until FROM harvest_progress WHERE id = 1"
    ).fetchone()
    return row["last_until"] if row else None


def advance_progress(conn: sqlite3.Connection, until_date: str) -> None:
    """整批成功后推进截止日（单行 upsert）。"""
    conn.execute(
        """
        INSERT INTO harvest_progress (id, last_until, last_harvest_at)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            last_until = excluded.last_until,
            last_harvest_at = excluded.last_harvest_at
        """,
        (until_date, utc_now_iso()),
    )


def start_run(conn: sqlite3.Connection, from_date: str, until_date: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO harvest_runs (started_at, from_date, until_date, status)
        VALUES (?, ?, ?, 'running')
        """,
        (utc_now_iso(), from_date, until_date),
    )
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, *, status: str,
               pages: int, fetched: int, inserted: int, updated: int,
               withdrawn: int, retries: int, error: str | None) -> None:
    conn.execute(
        """
        UPDATE harvest_runs SET
            finished_at = ?, status = ?, pages = ?, fetched = ?,
            inserted = ?, updated = ?, withdrawn = ?, retries = ?, error = ?
        WHERE id = ?
        """,
        (utc_now_iso(), status, pages, fetched, inserted, updated,
         withdrawn, retries, error, run_id),
    )


def upsert_bib_record(conn: sqlite3.Connection, *, identifier: str,
                      datestamp: str, fields: dict, raw_dc: str) -> str:
    """按标识 upsert 书目；返回 'inserted' 或 'updated'。"""
    existing = conn.execute(
        "SELECT identifier FROM bib_records WHERE identifier = ?",
        (identifier,),
    ).fetchone()
    now = utc_now_iso()
    params = (
        identifier, datestamp, "active",
        fields.get("title"), fields.get("creator"), fields.get("publisher"),
        fields.get("subject"), fields.get("description"), fields.get("date"),
        fields.get("type"), fields.get("format"), fields.get("identifier"),
        fields.get("source"), fields.get("language"), fields.get("relation"),
        fields.get("coverage"), fields.get("rights"), raw_dc, now,
    )
    if existing is None:
        conn.execute(
            """
            INSERT INTO bib_records (
                identifier, datestamp, status, title, creator, publisher,
                subject, description, date, type, format, identifier_dc,
                source, language, relation, coverage, rights, raw_dc, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params,
        )
        return "inserted"
    conn.execute(
        """
        UPDATE bib_records SET
            datestamp = ?, status = ?, title = ?, creator = ?, publisher = ?,
            subject = ?, description = ?, date = ?, type = ?, format = ?,
            identifier_dc = ?, source = ?, language = ?, relation = ?,
            coverage = ?, rights = ?, raw_dc = ?, updated_at = ?
        WHERE identifier = ?
        """,
        params[1:] + (identifier,),
    )
    return "updated"


def withdraw_bib_record(conn: sqlite3.Connection, *, identifier: str,
                        datestamp: str) -> bool:
    """遇删除标记则撤下（标记 withdrawn）。不存在返回 False。"""
    cur = conn.execute(
        """
        UPDATE bib_records
        SET status = 'withdrawn', datestamp = ?, updated_at = ?
        WHERE identifier = ?
        """,
        (datestamp, utc_now_iso(), identifier),
    )
    return cur.rowcount > 0


def insert_withdrawn_tombstone(conn: sqlite3.Connection, *, identifier: str,
                               datestamp: str) -> None:
    """删除标记记录此前不在库中时，写入 withdrawn 墓碑，保留撤下痕迹。"""
    conn.execute(
        """
        INSERT INTO bib_records (
            identifier, datestamp, status, raw_dc, updated_at
        ) VALUES (?, ?, 'withdrawn', ?, ?)
        ON CONFLICT(identifier) DO NOTHING
        """,
        (identifier, datestamp, "{}", utc_now_iso()),
    )


# ---------------------------------------------------------------------------
# 分卷归属
# ---------------------------------------------------------------------------

def upsert_attribution(conn: sqlite3.Connection, result) -> None:
    """按归属求解结果 upsert 一行 attributions。"""
    row = result.as_row()
    conn.execute(
        """
        INSERT INTO attributions (
            identifier, parent_identifier, root_identifier, status,
            reason, chain, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(identifier) DO UPDATE SET
            parent_identifier = excluded.parent_identifier,
            root_identifier   = excluded.root_identifier,
            status            = excluded.status,
            reason            = excluded.reason,
            chain             = excluded.chain,
            updated_at        = excluded.updated_at
        """,
        (
            row["identifier"], row["parent_identifier"],
            row["root_identifier"], row["status"], row["reason"],
            row["chain"], utc_now_iso(),
        ),
    )


def get_attribution(conn: sqlite3.Connection, identifier: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM attributions WHERE identifier = ?", (identifier,)
    ).fetchone()


def list_unresolved_attributions(conn: sqlite3.Connection, *, limit: int,
                                 offset: int) -> list[sqlite3.Row]:
    """未决归属（缺失/撤下/成环），供编目员筛选待补采、待更正清单。"""
    return conn.execute(
        """
        SELECT * FROM attributions
        WHERE status IN ('parent_missing', 'parent_withdrawn', 'cycle')
        ORDER BY identifier LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()


def find_descendant_identifiers(conn: sqlite3.Connection,
                                identifiers: list[str]) -> set[str]:
    """反查直接或间接以 identifiers 中任一记录为上级的卷册。

    只沿已存储的 parent_identifier 关系向下闭包，不重扫全库。
    """
    affected: set[str] = set()
    frontier = list(identifiers)
    while frontier:
        placeholders = ",".join("?" for _ in frontier)
        rows = conn.execute(
            f"""
            SELECT identifier FROM attributions
            WHERE parent_identifier IN ({placeholders})
            """,
            frontier,
        ).fetchall()
        frontier = [
            row["identifier"] for row in rows
            if row["identifier"] not in affected
        ]
        affected.update(frontier)
    return affected


def delete_attribution(conn: sqlite3.Connection, identifier: str) -> None:
    """书目被清理时同步删除归属（当前采集只撤下不删除，保留以备扩展）。"""
    conn.execute("DELETE FROM attributions WHERE identifier = ?", (identifier,))


def backfill_attributions(conn: sqlite3.Connection) -> int:
    """升级旧库：为缺少归属行的旧目录补建归属。

    - 只 INSERT 缺失行，已计算过的归属不覆盖；
    - 不修改 harvest_progress / harvest_runs，续采进度不变；
    - 返回补建条数。调用方负责在同一事务内提交。
    """
    # 延迟导入避免 db <-> attribution 循环依赖
    from .attribution import resolve_attribution

    rows = conn.execute(
        """
        SELECT b.identifier FROM bib_records b
        LEFT JOIN attributions a ON a.identifier = b.identifier
        WHERE a.identifier IS NULL
        ORDER BY b.identifier
        """
    ).fetchall()
    for row in rows:
        result = resolve_attribution(conn, row["identifier"])
        upsert_attribution(conn, result)
    return len(rows)
