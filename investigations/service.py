#!/usr/bin/env python3
"""Lab anomaly investigation workflow service (rules applied via investigations.rules)."""
from __future__ import annotations

import json
import sqlite3

from common import ApiError, now
from investigations import rules
from investigations.store import InvestigationStore

SYSTEM_ACTOR = "system"


class InvestigationService:
    def __init__(self, conn: sqlite3.Connection, store: InvestigationStore, audit):
        self.conn = conn
        self.store = store
        self.audit = audit  # store.audit(actor, action, entity_type, entity_id, details)

    # ---- helpers -------------------------------------------------------
    @staticmethod
    def _actor(actor: str | None, role: str | None, allowed: set[str]) -> str:
        if not actor:
            raise ApiError(401, "缺少身份")
        if role not in allowed:
            raise ApiError(403, "角色无权执行此操作")
        return actor

    def _advance_batch(self, batch_id: int, expected_revision: int) -> int:
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if batch is None:
            raise ApiError(404, "批次不存在")
        if int(expected_revision) != int(batch["revision"]):
            raise ApiError(409, "批次版本冲突，请刷新后重试")
        stamp = now()
        cur = self.conn.execute(
            "UPDATE batches SET state='investigation',revision=revision+1,updated_at=? WHERE id=? AND revision=?",
            (stamp, batch_id, expected_revision))
        if cur.rowcount != 1:
            raise ApiError(409, "并发更新冲突")
        return int(batch["revision"]) + 1

    # ---- hooks called by BatchService inside its transaction -----------
    def on_test_recorded(self, batch_id: int, test: sqlite3.Row, actor: str) -> None:
        """A test row (any round) was written: create/annotate/reopen the investigation."""
        head = self.store.head_for_test(batch_id, test["test_type"])
        revision = self.conn.execute("SELECT revision FROM batches WHERE id=?", (batch_id,)).fetchone()[0]
        if head is None:
            if not test["passed"]:
                head = self.store.create(batch_id, test["id"], test["test_type"], actor, revision)
                self.audit(actor, "investigation.auto_open", "investigation", head["id"],
                           {"batch_id": batch_id, "test_type": test["test_type"]})
            return
        prior = self.conn.execute(
            "SELECT COUNT(*) AS c FROM tests WHERE batch_id=? AND test_type=? AND id<>?",
            (batch_id, test["test_type"], test["id"])).fetchone()["c"]
        kind = "test_retest" if int(test["round"]) > 1 or prior else "test_record"
        label = "复验回写" if kind == "test_retest" else "检验记录"
        self.store.add_event(
            head["id"], head["version"], kind,
            f"{label}（第 {test['round']} 轮，{'合格' if test['passed'] else '不合格'}）",
            actor, {"test_id": test["id"], "round": test["round"], "passed": bool(test["passed"]),
                    "result": test["result"]})
        # a closed investigation is re-judged whenever the test data changes/new evidence arrives
        if head["status"] == rules.CLOSED and (not test["passed"] or int(test["round"]) > 1 or prior):
            trigger = "检验复验再次不合格" if not test["passed"] else "复验结果更新，原调查按新版本重判"
            self.store.reopen_as_new_version(head, trigger, actor, revision)
            self.audit(actor, "investigation.reopen", "investigation", head["id"],
                       {"batch_id": batch_id, "test_type": test["test_type"], "trigger": trigger})
        elif head["status"] == rules.AWAITING_CONFIRMATION and not test["passed"]:
            self.store.reopen_as_new_version(head, "复验再次不合格", actor, revision)
            self.audit(actor, "investigation.reopen", "investigation", head["id"],
                       {"batch_id": batch_id, "test_type": test["test_type"], "trigger": "复验再次不合格"})

    def on_source_changed(self, batch_id: int, trigger: str, actor: str, detail: dict) -> None:
        """Rework or supplier data changed: every head is judged against the new data version."""
        revision = self.conn.execute("SELECT revision FROM batches WHERE id=?", (batch_id,)).fetchone()[0]
        heads = self.conn.execute(
            "SELECT * FROM investigations WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
        for head in heads:
            if head["status"] == rules.CLOSED:
                reopened = self.store.reopen_as_new_version(head, trigger, actor, revision)
                self.store.add_event(reopened["id"], reopened["version"], "source_changed",
                                     f"{trigger}，新版本重判", actor, detail)
                self.audit(actor, "investigation.reopen", "investigation", head["id"],
                           {"batch_id": batch_id, "test_type": head["test_type"], "trigger": trigger, **detail})
            else:
                self.store.add_event(
                    head["id"], head["version"], "source_changed",
                    f"{trigger}，调查仍在当前版本 v{head['version']} 判定", actor, detail)

    # ---- workflow endpoints -------------------------------------------
    def register(self, actor, role, investigation_id: int, reason: str, retest_plan: str,
                 owner: str, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"lab", "inspector", "qa"})
        inv = self.store.get(investigation_id)
        if inv["status"] != rules.OPEN:
            raise ApiError(409, "调查不在待查状态，不能登记")
        reason, retest_plan, owner = reason.strip(), retest_plan.strip(), owner.strip()
        if not reason or not retest_plan or not owner:
            raise ApiError(400, "必须登记异常原因、复验方案和负责人")
        with self.conn:
            new_revision = self._advance_batch(inv["batch_id"], expected_revision)
            self.store.register(inv, reason, retest_plan, owner, actor)
            self.audit(actor, "investigation.register", "investigation", investigation_id,
                       {"batch_id": inv["batch_id"], "revision": new_revision})
        return self.detail(investigation_id)

    def submit_conclusion(self, actor, role, investigation_id: int, result_conclusion: str,
                          key_deviation: bool, disposition: str, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"lab", "qa"})
        inv = self.store.get(investigation_id)
        if inv["status"] != rules.OPEN:
            raise ApiError(409, "调查不在待查状态，不能提交结论")
        if not result_conclusion.strip():
            raise ApiError(400, "必须填写调查结论")
        tests = self.conn.execute(
            "SELECT * FROM tests WHERE batch_id=? ORDER BY id", (inv["batch_id"],)).fetchall()
        blockers = rules.conclusion_blockers(dict(inv), [dict(t) for t in tests])
        if blockers:
            raise ApiError(409, "；".join(blockers))
        with self.conn:
            new_revision = self._advance_batch(inv["batch_id"], expected_revision)
            self.store.submit_conclusion(inv, result_conclusion.strip(), bool(key_deviation),
                                         disposition.strip(), actor)
            self.audit(actor, "investigation.conclude", "investigation", investigation_id,
                       {"batch_id": inv["batch_id"], "key_deviation": bool(key_deviation),
                        "revision": new_revision})
        return self.detail(investigation_id)

    def confirm(self, actor, role, investigation_id: int, expected_revision: int) -> dict:
        """Close the investigation; the confirmer must be a second QA person."""
        actor = self._actor(actor, role, {"qa"})
        inv = self.store.get(investigation_id)
        if inv["status"] != rules.AWAITING_CONFIRMATION:
            raise ApiError(409, "只有待确认的调查可以确认")
        if inv["concluded_by"] == actor:
            raise ApiError(409, "结论必须由另一名质量人员确认")
        with self.conn:
            new_revision = self._advance_batch(inv["batch_id"], expected_revision)
            confirmed = self.store.confirm(inv, actor)
            self.store.archive_current(confirmed, "调查关闭并经第二人确认，版本归档")
            self.audit(actor, "investigation.confirm", "investigation", investigation_id,
                       {"batch_id": inv["batch_id"], "concluded_by": inv["concluded_by"],
                        "revision": new_revision})
        return self.detail(investigation_id)

    # ---- release gate --------------------------------------------------
    def release_blockers_for_batch(self, batch_id: int) -> list[str]:
        tests = self.conn.execute("SELECT * FROM tests WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
        return rules.release_blockers(
            [dict(r) for r in self.store.open_for_batch(batch_id)], {batch_id: [dict(t) for t in tests]})

    # ---- views ---------------------------------------------------------
    def _serialize(self, row: sqlite3.Row, include_latest: bool = True) -> dict:
        out = {k: row[k] for k in row.keys()}
        if "key_deviation" in out and out["key_deviation"] is not None:
            out["key_deviation"] = bool(out["key_deviation"])
        tests = self.conn.execute(
            "SELECT * FROM tests WHERE batch_id=? ORDER BY id", (row["batch_id"],)).fetchall()
        as_dict = dict(row)
        tests_as_dicts = [dict(t) for t in tests]
        out["block_reasons"] = rules.is_blocked(as_dict, tests_as_dicts)
        out["missing"] = rules.missing_registration(as_dict) if row["status"] == rules.OPEN else []
        if include_latest:
            latest = rules.latest_tests(tests_as_dicts).get(row["test_type"])
            out["latest_test"] = self._test_dict(latest) if latest else None
        return out

    @staticmethod
    def _test_dict(row) -> dict | None:
        if row is None:
            return None
        return {"id": row["id"], "test_type": row["test_type"], "result": row["result"],
                "spec_min": row["spec_min"], "spec_max": row["spec_max"],
                "passed": bool(row["passed"]), "round": row["round"],
                "recorded_by": row["recorded_by"], "created_at": row["created_at"]}

    def list(self, status: str | None = None) -> dict:
        rows = self.store.list_rows(status)
        open_items, closed_items = [], []
        for row in rows:
            item = self._serialize(row)
            item["batch_no"] = row["batch_no"]
            item["product"] = row["product"]
            item["factory_name"] = row["factory_name"]
            (closed_items if row["status"] == rules.CLOSED else open_items).append(item)
        return {"open": open_items, "closed": closed_items}

    def detail(self, investigation_id: int) -> dict:
        row = self.store.get(investigation_id)
        out = self._serialize(row, include_latest=False)
        out["tests"] = [self._test_dict(t) for t in self.store.tests_for(investigation_id, row["test_type"])]
        out["latest_test"] = out["tests"][-1] if out["tests"] else None
        out["events"] = [dict(e) | {"payload": json.loads(e["payload_json"])}
                         for e in self.store.events(investigation_id)]
        out["versions"] = [{"version": v["version"], "status": v["status"],
                            "archived_at": v["archived_at"], "archived_reason": v["archived_reason"],
                            "confirmed_by": v["confirmed_by"]}
                           for v in self.store.versions(investigation_id)]
        batch = self.conn.execute("SELECT batch_no,product,revision FROM batches WHERE id=?",
                                  (row["batch_id"],)).fetchone()
        out["batch_no"] = batch["batch_no"]
        out["product"] = batch["product"]
        out["batch_revision_now"] = batch["revision"]
        return out
