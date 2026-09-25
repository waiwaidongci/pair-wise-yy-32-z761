#!/usr/bin/env python3
"""Laboratory anomaly investigation storage (versioned, append-only archive)."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from common import j, now


class InvestigationStore:
    """Owns the investigation tables; never decides rules itself."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS investigations (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          batch_id INTEGER NOT NULL REFERENCES batches(id),
          test_id INTEGER,
          test_type TEXT NOT NULL,
          version INTEGER NOT NULL DEFAULT 1,
          status TEXT NOT NULL CHECK(status IN ('open','awaiting_confirmation','closed')),
          reason TEXT, retest_plan TEXT, owner TEXT,
          result_conclusion TEXT, key_deviation INTEGER, disposition TEXT,
          concluded_by TEXT, concluded_at TEXT,
          confirmed_by TEXT, confirmed_at TEXT,
          reopened_from_version INTEGER,
          batch_revision INTEGER NOT NULL,
          created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_investigations_active
          ON investigations(batch_id, test_type, version);
        CREATE TABLE IF NOT EXISTS investigation_versions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          investigation_id INTEGER NOT NULL REFERENCES investigations(id),
          version INTEGER NOT NULL,
          status TEXT NOT NULL,
          reason TEXT, retest_plan TEXT, owner TEXT,
          result_conclusion TEXT, key_deviation INTEGER, disposition TEXT,
          concluded_by TEXT, concluded_at TEXT,
          confirmed_by TEXT, confirmed_at TEXT,
          archived_at TEXT NOT NULL, archived_reason TEXT NOT NULL,
          snapshot_json TEXT NOT NULL,
          UNIQUE(investigation_id, version)
        );
        CREATE TABLE IF NOT EXISTS investigation_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          investigation_id INTEGER NOT NULL REFERENCES investigations(id),
          version INTEGER NOT NULL,
          kind TEXT NOT NULL,
          detail TEXT NOT NULL,
          actor TEXT NOT NULL, at TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}'
        );
        """)
        self.conn.commit()

    # ---- queries -------------------------------------------------------
    def head_for_test(self, batch_id: int, test_type: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM investigations WHERE batch_id=? AND test_type=? ORDER BY version DESC LIMIT 1",
            (batch_id, test_type)).fetchone()

    def open_for_batch(self, batch_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM investigations WHERE batch_id=? AND status!='closed' ORDER BY id",
            (batch_id,)).fetchall()

    def get(self, investigation_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM investigations WHERE id=?", (investigation_id,)).fetchone()
        if not row:
            from common import ApiError
            raise ApiError(404, "调查记录不存在")
        return row

    def list_rows(self, status: str | None = None) -> list[sqlite3.Row]:
        sql = """SELECT i.*, b.batch_no, b.product, b.factory_id, f.name AS factory_name
                 FROM investigations i JOIN batches b ON b.id=i.batch_id
                 JOIN factories f ON f.id=b.factory_id"""
        if status == "open":
            sql += " WHERE i.status!='closed'"
        elif status == "closed":
            sql += " WHERE i.status='closed'"
        sql += " ORDER BY i.status='closed', i.id DESC"
        return self.conn.execute(sql).fetchall()

    def events(self, investigation_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM investigation_events WHERE investigation_id=? ORDER BY id",
            (investigation_id,)).fetchall()

    def versions(self, investigation_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM investigation_versions WHERE investigation_id=? ORDER BY version",
            (investigation_id,)).fetchall()

    def latest_tests(self, batch_id: int) -> dict[str, sqlite3.Row]:
        latest: dict[str, sqlite3.Row] = {}
        for row in self.conn.execute("SELECT * FROM tests WHERE batch_id=? ORDER BY id", (batch_id,)):
            latest[row["test_type"]] = row
        return latest

    def tests_for(self, investigation_id: int, test_type: str) -> list[sqlite3.Row]:
        row = self.get(investigation_id)
        return self.conn.execute(
            "SELECT * FROM tests WHERE batch_id=? AND test_type=? ORDER BY round, id",
            (row["batch_id"], test_type)).fetchall()

    # ---- mutations -----------------------------------------------------
    def create(self, batch_id: int, test_id: int | None, test_type: str, actor: str, batch_revision: int) -> sqlite3.Row:
        stamp = now()
        cur = self.conn.execute(
            """INSERT INTO investigations(batch_id,test_id,test_type,version,status,batch_revision,created_by,created_at,updated_at)
               VALUES(?,?,?,1,'open',?,?,?,?)""",
            (batch_id, test_id, test_type, batch_revision, actor, stamp, stamp))
        inv_id = int(cur.lastrowid)
        self.add_event(inv_id, 1, "created", "首检不合格，系统建立待查记录", actor,
                       {"test_id": test_id, "test_type": test_type})
        return self.get(inv_id)

    def register(self, row: sqlite3.Row, reason: str, retest_plan: str, owner: str, actor: str) -> sqlite3.Row:
        self.conn.execute(
            "UPDATE investigations SET reason=?,retest_plan=?,owner=?,updated_at=? WHERE id=?",
            (reason, retest_plan, owner, now(), row["id"]))
        self.add_event(row["id"], row["version"], "registered",
                       f"登记异常原因、复验方案与负责人 {owner}", actor,
                       {"reason": reason, "retest_plan": retest_plan, "owner": owner})
        return self.get(row["id"])

    def add_event(self, investigation_id: int, version: int, kind: str, detail: str,
                  actor: str, payload: dict[str, Any] | None = None) -> None:
        self.conn.execute(
            """INSERT INTO investigation_events(investigation_id,version,kind,detail,actor,at,payload_json)
               VALUES(?,?,?,?,?,?,?)""",
            (investigation_id, version, kind, detail, actor, now(), j(payload or {})))

    def submit_conclusion(self, row: sqlite3.Row, result_conclusion: str, key_deviation: bool,
                          disposition: str, actor: str) -> sqlite3.Row:
        stamp = now()
        self.conn.execute(
            """UPDATE investigations SET result_conclusion=?,key_deviation=?,disposition=?,
                   concluded_by=?,concluded_at=?,status='awaiting_confirmation',updated_at=? WHERE id=?""",
            (result_conclusion, int(key_deviation), disposition, actor, stamp, stamp, row["id"]))
        self.add_event(row["id"], row["version"], "concluded",
                       "调查结论提交，等待另一名质量人员确认", actor,
                       {"result_conclusion": result_conclusion, "key_deviation": key_deviation,
                        "disposition": disposition})
        return self.get(row["id"])

    def confirm(self, row: sqlite3.Row, actor: str) -> sqlite3.Row:
        stamp = now()
        self.conn.execute(
            "UPDATE investigations SET status='closed',confirmed_by=?,confirmed_at=?,updated_at=? WHERE id=?",
            (actor, stamp, stamp, row["id"]))
        self.add_event(row["id"], row["version"], "confirmed", f"结论由 {actor} 确认，调查关闭", actor)
        return self.get(row["id"])

    def archive_current(self, row: sqlite3.Row, reason: str) -> None:
        """Snapshot the current head into the append-only version archive (idempotent per version)."""
        self.conn.execute(
            """INSERT OR IGNORE INTO investigation_versions(investigation_id,version,status,reason,retest_plan,owner,
                  result_conclusion,key_deviation,disposition,concluded_by,concluded_at,confirmed_by,confirmed_at,
                  archived_at,archived_reason,snapshot_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["id"], row["version"], row["status"], row["reason"], row["retest_plan"], row["owner"],
             row["result_conclusion"], row["key_deviation"], row["disposition"], row["concluded_by"],
             row["concluded_at"], row["confirmed_by"], row["confirmed_at"], now(), reason,
             j(dict(row))))

    def reopen_as_new_version(self, old: sqlite3.Row, trigger: str, actor: str, batch_revision: int) -> sqlite3.Row:
        """Re-judge after source/test data changed: archive old head, advance to a new open version."""
        self.archive_current(old, trigger)
        new_version = int(old["version"]) + 1
        stamp = now()
        self.conn.execute(
            """UPDATE investigations SET version=?,status='open',reason=?,retest_plan=?,owner=?,
                  result_conclusion=NULL,key_deviation=NULL,disposition=NULL,
                  concluded_by=NULL,concluded_at=NULL,confirmed_by=NULL,confirmed_at=NULL,
                  reopened_from_version=?,batch_revision=?,updated_at=? WHERE id=?""",
            (new_version, old["reason"], old["retest_plan"], old["owner"],
             old["version"], batch_revision, stamp, old["id"]))
        inv = self.get(old["id"])
        self.add_event(inv["id"], new_version, "reopened",
                       f"{trigger}：旧版本 v{old['version']} 归档，按新版本 v{new_version} 重新判定",
                       actor, {"reopened_from_version": old["version"], "trigger": trigger})
        return inv
