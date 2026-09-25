import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, BatchService, Store


class LabInvestigationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.s = BatchService(Store(Path(self.tmp.name) / "b.db"))
        self.f1 = self.s.register_factory("qa", "qa", "F1", "一厂", "CN")["id"]

    def tearDown(self): self.s.store.close(); self.tmp.cleanup()

    def _batch(self):
        return self.s.create_batch("operator", "operator", self.f1, "B-1", "药片", "2026-01-01", "2028-01-01")

    def _revision(self, batch_id):
        return self.s.batch_detail(batch_id)["batch"]["revision"]

    def _investigation(self, batch_id):
        invs = self.s.batch_detail(batch_id)["investigations"]
        self.assertEqual(1, len(invs))
        return invs[0]

    def test_first_failure_creates_investigation_and_blocks_release(self):
        batch = self._batch()
        failed = self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 89, 95, 105, batch["revision"])
        self.assertFalse(failed["passed"])
        inv = self._investigation(batch["id"])
        self.assertEqual("pending", inv["status"]); self.assertEqual("fail", inv["verdict"]); self.assertEqual(1, inv["version"])
        self.assertIn("未登记异常原因", inv["blockers"])
        passed = self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 99, 95, 105, self._revision(batch["id"]))
        self.assertTrue(passed["passed"])
        with self.assertRaises(ApiError) as blocked:
            self.s.decide("qa", "qa", batch["id"], "release", "复测合格，尝试放行", self._revision(batch["id"]))
        self.assertIn("调查", blocked.exception.message)

    def test_retest_writeback_conclude_and_confirm_by_another_qa(self):
        batch = self._batch()
        self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 89, 95, 105, batch["revision"])
        inv = self._investigation(batch["id"])
        with self.assertRaises(ApiError):
            self.s.conclude_investigation("qa", "qa", inv["id"], "结论")
        self.s.register_investigation("lab", "lab", inv["id"], "灌装泵校准偏移", "同一样品复测两次", "王工")
        passed = self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 99, 95, 105, self._revision(batch["id"]))
        self.assertTrue(passed["passed"])
        inv = self._investigation(batch["id"])
        self.assertEqual("pass", inv["verdict"]); self.assertEqual(2, inv["version"])
        self.assertEqual("retest", inv["archive"][-1]["event"])
        self.assertEqual(99, inv["latest_test"]["result"])
        self.s.conclude_investigation("qa", "qa", inv["id"], "偏差明确，复测合格")
        with self.assertRaises(ApiError) as same:
            self.s.confirm_investigation("qa", "qa", inv["id"])
        self.assertIn("另一名质量人员", same.exception.message)
        closed = self.s.confirm_investigation("qa-lead", "qa", inv["id"])
        self.assertEqual("closed", closed["status"])
        result = self.s.decide("qa", "qa", batch["id"], "release", "调查关闭，检验合格", self._revision(batch["id"]))
        self.assertEqual("released", result["batch"]["state"])

    def test_data_change_rejudges_and_archives_old_record(self):
        batch = self._batch()
        self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 89, 95, 105, batch["revision"])
        inv = self._investigation(batch["id"])
        self.s.register_investigation("qa", "qa", inv["id"], "原料水分偏高", "复测含量", "李工")
        self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 99, 95, 105, self._revision(batch["id"]))
        self.s.conclude_investigation("qa", "qa", inv["id"], "复测合格")
        self.s.record_supplier_change("operator", "operator", self.f1, batch["id"], "供应商A", "变更产地", "原料产地变更", self._revision(batch["id"]))
        inv = self._investigation(batch["id"])
        self.assertEqual("concluded", inv["status"]); self.assertEqual(3, inv["version"])
        self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 90, 95, 105, self._revision(batch["id"]))
        inv = self._investigation(batch["id"])
        self.assertEqual("pending", inv["status"]); self.assertEqual("fail", inv["verdict"]); self.assertEqual(4, inv["version"])
        self.assertIsNone(inv["conclusion"])
        events = [a["event"] for a in inv["archive"]]
        self.assertIn("retest", events); self.assertIn("rejudged", events)
        self.assertIn(3, [a["version"] for a in inv["archive"]])
        snapshots = [a["snapshot"] for a in inv["archive"]]
        self.assertTrue(any(s["status"] == "concluded" and s["conclusion"] == "复测合格" for s in snapshots))
        passed = self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 100, 95, 105, self._revision(batch["id"]))
        self.assertTrue(passed["passed"])
        with self.assertRaises(ApiError) as blocked:
            self.s.decide("qa", "qa", batch["id"], "release", "复测合格，尝试放行", self._revision(batch["id"]))
        self.assertIn("调查", blocked.exception.message)


if __name__ == "__main__": unittest.main()
