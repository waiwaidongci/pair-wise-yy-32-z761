import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, BatchService, Store
from investigations import rules


def rev(svc, batch_id):
    return svc.batch_detail(batch_id)["batch"]["revision"]


class InvestigationFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = BatchService(Store(Path(self.tmp.name) / "i.db"))
        self.f1 = self.s.register_factory("qa", "qa", "F1", "一厂", "CN")["id"]
        self.batch = self.s.create_batch("operator", "operator", self.f1, "B-I1", "注射液", "2026-03-01", "2028-03-01")
        self.bid = self.batch["id"]

    def tearDown(self):
        self.s.store.close(); self.tmp.cleanup()

    def _fail_then_investigate(self, owner="lab1", conclude_by="lab1", confirm_by="qa2"):
        r = self.s.record_test("lab1", "lab", self.f1, self.bid, "含量", 88, 95, 105, rev(self.s, self.bid))
        self.assertFalse(r["passed"])
        detail = self.s.batch_detail(self.bid)
        self.assertEqual(1, len(detail["investigations"]))
        inv = self.s.investigations.list()["open"][0]
        self.assertEqual(rules.OPEN, inv["status"])
        # first failed test alone blocks release
        blockers = self.s.investigations.release_blockers_for_batch(self.bid)
        self.assertTrue(any("异常原因" in b for b in blockers))
        with self.assertRaises(ApiError) as blocked:
            self.s.decide("qa1", "qa", self.bid, "release", "尝试放行", rev(self.s, self.bid))
        self.assertTrue("不合格" in blocked.exception.message or "调查未闭环" in blocked.exception.message)

        self.s.investigations.register("lab1", "lab", inv["id"], "配液投料计算错误", "重新配液并复测含量", owner, rev(self.s, self.bid))
        # once registration exists but no passing retest yet, the investigation itself blocks release
        self.assertIn("尚未复验合格", "；".join(self.s.investigations.release_blockers_for_batch(self.bid)))
        # retest passes and is written back
        self.s.record_test("lab1", "lab", self.f1, self.bid, "含量", 100, 95, 105, rev(self.s, self.bid))
        # cannot conclude without a retest (separate assertion in rule test); here retest exists
        self.s.investigations.submit_conclusion(conclude_by, "lab", inv["id"], "复测合格，原因为投料差错", False,
                                                "返工后复检合格，建议放行", rev(self.s, self.bid))
        detail = self.s.investigations.detail(inv["id"])
        self.assertEqual(rules.AWAITING_CONFIRMATION, detail["status"])
        # release still blocked while awaiting confirmation
        with self.assertRaises(ApiError):
            self.s.decide("qa1", "qa", self.bid, "release", "尝试放行", rev(self.s, self.bid))
        # same person cannot confirm
        if conclude_by == confirm_by:
            with self.assertRaises(ApiError):
                self.s.investigations.confirm(confirm_by, "qa", inv["id"], rev(self.s, self.bid))
            return inv
        self.s.investigations.confirm(confirm_by, "qa", inv["id"], rev(self.s, self.bid))
        self.assertEqual(rules.CLOSED, self.s.investigations.detail(inv["id"])["status"])
        return inv

    def test_full_investigation_blocks_until_second_qa_confirms(self):
        inv = self._fail_then_investigate(conclude_by="lab1", confirm_by="qa2")
        out = self.s.decide("qa1", "qa", self.bid, "release", "调查关闭，复测合格", rev(self.s, self.bid))
        self.assertEqual("released", out["batch"]["state"])
        detail = self.s.investigations.detail(inv["id"])
        self.assertEqual("qa2", detail["confirmed_by"])
        self.assertGreaterEqual(len(detail["events"]), 4)

    def test_conclusion_requires_passing_retest(self):
        self.s.record_test("lab1", "lab", self.f1, self.bid, "含量", 88, 95, 105, rev(self.s, self.bid))
        inv_id = self.s.investigations.list()["open"][0]["id"]
        self.s.investigations.register("lab1", "lab", inv_id, "原因", "方案", "lab1", rev(self.s, self.bid))
        with self.assertRaises(ApiError) as e1:
            self.s.investigations.submit_conclusion("lab1", "lab", inv_id, "结论", False, "", rev(self.s, self.bid))
        self.assertIn("复验", e1.exception.message)
        # failing retest still cannot conclude
        self.s.record_test("lab1", "lab", self.f1, self.bid, "含量", 89, 95, 105, rev(self.s, self.bid))
        with self.assertRaises(ApiError) as e2:
            self.s.investigations.submit_conclusion("lab1", "lab", inv_id, "结论", False, "", rev(self.s, self.bid))
        self.assertIn("仍不合格", e2.exception.message)

    def test_same_qa_cannot_self_confirm(self):
        self._fail_then_investigate(conclude_by="qa1", confirm_by="qa1")
        inv_id = self.s.investigations.list()["open"][0]["id"]
        self.assertEqual(rules.AWAITING_CONFIRMATION, self.s.investigations.detail(inv_id)["status"])

    def test_key_deviation_conclusion_keeps_release_blocked(self):
        # key deviation as separate deviation record blocks; also investigation must be closed first
        self.s.record_test("lab1", "lab", self.f1, self.bid, "内毒素", 5, 0, 2, rev(self.s, self.bid))
        inv_id = self.s.investigations.list()["open"][0]["id"]
        self.s.investigations.register("lab1", "lab", inv_id, "水源污染", "停产调查", "lab1", rev(self.s, self.bid))
        self.s.record_test("lab1", "lab", self.f1, self.bid, "内毒素", 1, 0, 2, retest_rev := rev(self.s, self.bid))
        self.s.investigations.submit_conclusion("lab1", "lab", inv_id, "环境监测超标", True, "报废", rev(self.s, self.bid))
        self.s.investigations.confirm("qa2", "qa", inv_id, rev(self.s, self.bid))
        # investigation closed, but a critical deviation recorded alongside blocks release anyway
        self.s.add_deviation("inspector", "inspector", self.f1, self.bid, "critical", "无菌数据异常", None, rev(self.s, self.bid))
        with self.assertRaises(ApiError) as e:
            self.s.decide("qa1", "qa", self.bid, "release", "放行", rev(self.s, self.bid))
        self.assertIn("关键偏差", e.exception.message)

    def test_new_test_round_after_closure_reopens_archived_version(self):
        inv = self._fail_then_investigate()
        inv_id = inv["id"]
        self.assertEqual(1, self.s.investigations.detail(inv_id)["version"])
        # supplier change after closure -> re-adjudicate as new version, old archived
        self.s.record_supplier_change("operator", "operator", self.f1, self.bid, "新原料药厂", "供应商变更",
                                      "起始物料供应商更换", rev(self.s, self.bid))
        detail = self.s.investigations.detail(inv_id)
        self.assertEqual(rules.OPEN, detail["status"])
        self.assertEqual(2, detail["version"])
        archived = detail["versions"]
        self.assertTrue(any(v["version"] == 1 for v in archived))
        # rework after reopening only annotates current version
        self.s.plan_rework("operator", "operator", self.f1, self.bid, "返工处理", rev(self.s, self.bid))
        self.assertEqual(2, self.s.investigations.detail(inv_id)["version"])
        # close the v2 investigation with a passing retest and second-person confirm
        self.s.record_test("lab1", "lab", self.f1, self.bid, "含量", 101, 95, 105, rev(self.s, self.bid))
        self.s.investigations.submit_conclusion("lab1", "lab", inv_id, "供应商变更评估无影响，复测合格", False,
                                                "放行", rev(self.s, self.bid))
        self.s.investigations.confirm("qa2", "qa", inv_id, rev(self.s, self.bid))
        # a new failing test round on a closed investigation forces a fresh version
        self.s.record_test("lab1", "lab", self.f1, self.bid, "含量", 90, 95, 105, rev(self.s, self.bid))
        self.assertEqual(3, self.s.investigations.detail(inv_id)["version"])
        # release blocked again
        with self.assertRaises(ApiError):
            self.s.decide("qa1", "qa", self.bid, "release", "放行", rev(self.s, self.bid))
        # archive retains both prior versions
        versions = {v["version"]: v for v in self.s.investigations.detail(inv_id)["versions"]}
        self.assertEqual({1, 2}, {*versions})
        self.assertEqual(versions[1]["status"], "closed")
        self.assertEqual(versions[2]["status"], "closed")

    def test_only_lab_inspector_qa_can_register(self):
        self.s.record_test("lab1", "lab", self.f1, self.bid, "含量", 88, 95, 105, rev(self.s, self.bid))
        inv_id = self.s.investigations.list()["open"][0]["id"]
        with self.assertRaises(ApiError) as e:
            self.s.investigations.register("op", "operator", inv_id, "a", "b", "c", rev(self.s, self.bid))
        self.assertEqual(403, e.exception.status)

    def test_list_groups_open_and_closed_with_block_reasons(self):
        self._fail_then_investigate()
        data = self.s.investigations.list()
        self.assertEqual(0, len(data["open"]))
        self.assertEqual(1, len(data["closed"]))
        self.assertEqual([], data["closed"][0]["block_reasons"])
        self.assertIsNotNone(data["closed"][0]["latest_test"])


class InvestigationRuleTest(unittest.TestCase):
    def _inv(self, **kw):
        base = {"id": 1, "batch_id": 9, "test_type": "含量", "version": 1, "status": rules.OPEN,
                "reason": "", "retest_plan": "", "owner": "", "result_conclusion": None,
                "key_deviation": None, "disposition": None, "concluded_by": None, "confirmed_by": None}
        base.update(kw); return base

    def test_open_without_registration_blocks(self):
        reasons = rules.is_blocked(self._inv(), [])
        self.assertEqual(4, len(reasons))  # reason / plan / owner / no passing retest

    def test_awaiting_only_needs_confirmation(self):
        inv = self._inv(status=rules.AWAITING_CONFIRMATION, reason="r", retest_plan="p", owner="o")
        self.assertEqual(["调查 v1 结论待另一名质量人员确认"], rules.is_blocked(inv, []))

    def test_closed_never_blocks(self):
        inv = self._inv(status=rules.CLOSED, reason="r", retest_plan="p", owner="o")
        self.assertEqual([], rules.is_blocked(inv, []))

    def test_passing_retest_required_for_conclusion(self):
        inv = self._inv(reason="r", retest_plan="p", owner="o")
        tests = [{"test_type": "含量", "passed": 0}, {"test_type": "含量", "passed": 0}]
        self.assertTrue(any("仍不合格" in b for b in rules.conclusion_blockers(inv, tests)))
        tests[1]["passed"] = 1
        self.assertEqual([], rules.conclusion_blockers(inv, tests))
        # single pass with no retest round is not enough to close
        self.assertIn("尚无复验", rules.conclusion_blockers(inv, [{"test_type": "含量", "passed": 1}])[0])


if __name__ == "__main__":
    unittest.main()
