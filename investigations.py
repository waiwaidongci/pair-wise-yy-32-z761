"""实验室异常调查存储:首检不合格建待查记录,复验回写,版本重判与旧记录留档。

与放行规则(rules.py)、页面(static/index.html)分开维护;本模块只管调查自身的
表结构、生命周期和归档,事务由调用方(BatchService)统一管理。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import rules

SCHEMA = """
CREATE TABLE IF NOT EXISTS investigations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL REFERENCES batches(id),
  test_type TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','concluded','closed')) DEFAULT 'pending',
  cause TEXT, retest_plan TEXT, owner TEXT,
  verdict TEXT NOT NULL DEFAULT 'fail' CHECK(verdict IN ('none','pass','fail')),
  version INTEGER NOT NULL DEFAULT 1,
  conclusion TEXT, concluded_by TEXT, concluded_at TEXT,
  confirmed_by TEXT, confirmed_at TEXT,
  created_by TEXT NOT NULL, created_at TEXT NOT NULL, closed_at TEXT
);
CREATE TABLE IF NOT EXISTS investigation_archive (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  investigation_id INTEGER NOT NULL REFERENCES investigations(id),
  version INTEGER NOT NULL,
  event TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_investigations_batch ON investigations(batch_id);
"""


class InvestigationError(Exception):
    """调查生命周期不合法,由服务层转换为对应 HTTP 状态。"""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.message, self.status = message, status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _j(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class InvestigationService:
    def __init__(self, store):
        self.store, self.conn = store, store.conn

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def _row(self, investigation_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM investigations WHERE id=?", (investigation_id,)).fetchone()
        if not row:
            raise InvestigationError("调查记录不存在", 404)
        return row

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> dict:
        return {key: row[key] for key in row.keys()}

    def _log(self, inv: sqlite3.Row, event: str, actor: str, detail: dict) -> None:
        """把当前版本的完整记录留档,再附加事件信息。"""
        self.conn.execute("""INSERT INTO investigation_archive(investigation_id,version,event,snapshot_json,detail_json,actor,created_at)
                             VALUES(?,?,?,?,?,?,?)""",
                          (inv["id"], inv["version"], event, _j(self._snapshot(inv)), _j(detail), actor, _now()))

    def open_for_failure(self, batch_id: int, test_type: str, actor: str) -> sqlite3.Row | None:
        """首检不合格时建立待查记录;同项目已有未关闭调查则不重复建。"""
        existing = self.conn.execute("SELECT id FROM investigations WHERE batch_id=? AND test_type=? AND status!='closed'",
                                     (batch_id, test_type)).fetchone()
        if existing:
            return None
        cur = self.conn.execute("INSERT INTO investigations(batch_id,test_type,status,verdict,created_by,created_at) VALUES(?,?,'pending','fail',?,?)",
                                (batch_id, test_type, actor, _now()))
        row = self._row(cur.lastrowid)
        self._log(row, "created", actor, {"batch_id": batch_id, "test_type": test_type})
        self.store.audit(actor, "investigation.open", "investigation", row["id"], {"batch_id": batch_id, "test_type": test_type})
        return row

    def register(self, investigation_id: int, cause: str, retest_plan: str, owner: str, actor: str) -> dict:
        """登记异常原因、复验方案和负责人。"""
        inv = self._row(investigation_id)
        if inv["status"] != "pending":
            raise InvestigationError("调查已提交结论，不能修改登记信息")
        if not cause.strip() or not retest_plan.strip() or not owner.strip():
            raise InvestigationError("异常原因、复验方案和负责人不能为空", 400)
        self.conn.execute("UPDATE investigations SET cause=?,retest_plan=?,owner=? WHERE id=?", (cause, retest_plan, owner, investigation_id))
        row = self._row(investigation_id)
        self._log(row, "registered", actor, {"cause": cause, "retest_plan": retest_plan, "owner": owner})
        self.store.audit(actor, "investigation.register", "investigation", investigation_id, {"batch_id": inv["batch_id"], "owner": owner})
        return self._enrich(row)

    def conclude(self, investigation_id: int, conclusion: str, actor: str) -> dict:
        """提交调查结论:原因/方案/负责人已登记且最新复验合格。"""
        inv = self._row(investigation_id)
        if inv["status"] != "pending":
            raise InvestigationError("调查不在待查状态，不能提交结论")
        if not (inv["cause"] and inv["retest_plan"] and inv["owner"]):
            raise InvestigationError("请先登记异常原因、复验方案和负责人")
        if inv["verdict"] != "pass":
            raise InvestigationError("最新复验仍不合格，不能提交结论")
        if not conclusion.strip():
            raise InvestigationError("必须填写调查结论", 400)
        self.conn.execute("UPDATE investigations SET status='concluded',conclusion=?,concluded_by=?,concluded_at=? WHERE id=? AND status='pending'",
                          (conclusion, actor, _now(), investigation_id))
        row = self._row(investigation_id)
        self._log(row, "concluded", actor, {"conclusion": conclusion})
        self.store.audit(actor, "investigation.conclude", "investigation", investigation_id, {"batch_id": inv["batch_id"]})
        return self._enrich(row)

    def confirm(self, investigation_id: int, actor: str) -> dict:
        """结论由另一名质量人员确认后关闭。"""
        inv = self._row(investigation_id)
        if inv["status"] != "concluded":
            raise InvestigationError("调查尚未提交结论，不能确认")
        if inv["concluded_by"] == actor:
            raise InvestigationError("结论必须由另一名质量人员确认")
        stamp = _now()
        self.conn.execute("UPDATE investigations SET status='closed',confirmed_by=?,confirmed_at=?,closed_at=? WHERE id=? AND status='concluded'",
                          (actor, stamp, stamp, investigation_id))
        row = self._row(investigation_id)
        self._log(row, "confirmed", actor, {})
        self.store.audit(actor, "investigation.confirm", "investigation", investigation_id, {"batch_id": inv["batch_id"]})
        return self._enrich(row)

    def on_batch_data_changed(self, batch_id: int, actor: str, trigger: str, detail: dict) -> None:
        """检验、返工或供应商资料变化后,未关闭调查按新版本重判,旧记录留档。

        重判后最新结果再次不合格时,已提交但未确认的结论失效,调查回到待查。
        """
        open_invs = self.conn.execute("SELECT * FROM investigations WHERE batch_id=? AND status!='closed' ORDER BY id", (batch_id,)).fetchall()
        for inv in open_invs:
            new_verdict = rules.verdict_for(self._latest_test(batch_id, inv["test_type"]))
            event = "retest" if trigger == "test" and detail.get("test_type") == inv["test_type"] else "rejudged"
            self._log(inv, event, actor, {"trigger": trigger, "old_verdict": inv["verdict"], "new_verdict": new_verdict, **detail})
            if inv["status"] == "concluded" and new_verdict == "fail":
                self.conn.execute("""UPDATE investigations SET version=version+1,verdict=?,status='pending',
                                     conclusion=NULL,concluded_by=NULL,concluded_at=NULL WHERE id=?""", (new_verdict, inv["id"]))
            else:
                self.conn.execute("UPDATE investigations SET version=version+1,verdict=? WHERE id=?", (new_verdict, inv["id"]))

    def _latest_test(self, batch_id: int, test_type: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM tests WHERE batch_id=? AND test_type=? ORDER BY id DESC LIMIT 1", (batch_id, test_type)).fetchone()

    def _enrich(self, inv: sqlite3.Row, with_archive: bool = False) -> dict:
        latest = self._latest_test(inv["batch_id"], inv["test_type"])
        batch = self.conn.execute("SELECT batch_no,product FROM batches WHERE id=?", (inv["batch_id"],)).fetchone()
        data = {
            "id": inv["id"], "batch_id": inv["batch_id"],
            "batch_no": batch["batch_no"] if batch else "", "product": batch["product"] if batch else "",
            "test_type": inv["test_type"], "status": inv["status"], "version": inv["version"],
            "cause": inv["cause"], "retest_plan": inv["retest_plan"], "owner": inv["owner"], "verdict": inv["verdict"],
            "conclusion": inv["conclusion"], "concluded_by": inv["concluded_by"], "concluded_at": inv["concluded_at"],
            "confirmed_by": inv["confirmed_by"], "confirmed_at": inv["confirmed_at"],
            "created_by": inv["created_by"], "created_at": inv["created_at"], "closed_at": inv["closed_at"],
            "latest_test": ({"id": latest["id"], "round": latest["round"], "result": latest["result"],
                             "spec_min": latest["spec_min"], "spec_max": latest["spec_max"],
                             "passed": bool(latest["passed"]), "recorded_by": latest["recorded_by"]} if latest else None),
            "blockers": rules.investigation_blockers(inv, rules.verdict_for(latest)),
        }
        if with_archive:
            data["archive"] = [
                {"version": r["version"], "event": r["event"], "detail": json.loads(r["detail_json"]),
                 "snapshot": json.loads(r["snapshot_json"]), "actor": r["actor"], "created_at": r["created_at"]}
                for r in self.conn.execute("SELECT * FROM investigation_archive WHERE investigation_id=? ORDER BY id", (inv["id"],))]
        return data

    def for_batch(self, batch_id: int, with_archive: bool = True) -> list[dict]:
        return [self._enrich(row, with_archive)
                for row in self.conn.execute("SELECT * FROM investigations WHERE batch_id=? ORDER BY id", (batch_id,))]

    def list_all(self) -> list[dict]:
        return [self._enrich(row) for row in self.conn.execute("SELECT * FROM investigations ORDER BY id DESC")]
