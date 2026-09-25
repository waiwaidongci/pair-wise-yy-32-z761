"""放行与调查判定规则:纯函数,独立于存储与页面,单独维护。"""
from __future__ import annotations

DECISIONS = {"release", "reject", "conditional", "resample"}
TERMINAL_STATES = {"released", "rejected"}
NEXT_STATE = {"release": "released", "reject": "rejected", "conditional": "conditional", "resample": "awaiting_resample"}


def latest_by_type(tests) -> dict:
    """按检验项目取最新一轮结果(tests 需按 id 升序)。"""
    latest = {}
    for row in tests:
        latest[row["test_type"]] = row
    return latest


def verdict_for(latest_test) -> str:
    """最新检验对应的调查判定:pass / fail / none。"""
    if latest_test is None:
        return "none"
    return "pass" if latest_test["passed"] else "fail"


def investigation_blockers(investigation, verdict: str) -> list[str]:
    """调查仍未关闭的具体原因,用于页面展示与放行拦截说明。"""
    if investigation["status"] == "closed":
        return []
    reasons = []
    if not investigation["cause"]:
        reasons.append("未登记异常原因")
    if not investigation["retest_plan"]:
        reasons.append("未登记复验方案")
    if not investigation["owner"]:
        reasons.append("未指定调查负责人")
    if verdict == "fail":
        reasons.append("最新复验结果仍不合格")
    if verdict == "none":
        reasons.append("尚无复验结果")
    if investigation["status"] == "pending":
        reasons.append("调查结论未提交")
    elif investigation["status"] == "concluded":
        reasons.append("结论待另一名质量人员确认")
    return reasons


def decision_blockers(decision: str, batch_state: str, deviations, latest_tests: dict, investigations, after_now) -> list[str]:
    """按优先级返回阻止该质量决定的全部原因;空列表表示允许。"""
    blockers = []
    open_deviations = [d for d in deviations if d["status"] == "open"]
    if decision in ("release", "conditional"):
        if not latest_tests:
            blockers.append("放行前至少需要一项检验结果")
        elif any(not row["passed"] for row in latest_tests.values()):
            blockers.append("最新检验结果仍有不合格项")
    if decision == "resample":
        if batch_state == "conditional":
            blockers.append("有条件放行后不能直接改为再取样")
    elif decision != "reject":
        if any(d["severity"] == "critical" for d in open_deviations):
            blockers.append("未关闭的关键偏差阻止放行")
        elif decision == "release" and open_deviations:
            blockers.append("仍有未关闭偏差，不能正式放行")
        elif decision == "conditional":
            for deviation in open_deviations:
                if not deviation["exception_reason"] or not after_now(deviation["exception_until"]):
                    blockers.append(f"偏差 {deviation['id']} 没有有效例外批准")
        if decision == "release" and any(i["status"] != "closed" for i in investigations):
            blockers.append("存在未关闭的实验室异常调查，不能正式放行")
    return blockers
