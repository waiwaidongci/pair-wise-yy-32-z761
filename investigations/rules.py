#!/usr/bin/env python3
"""Pure release/investigation rules.

No database or HTTP access: callers pass rows/dicts and receive structured
findings so the same rules drive the API and the investigation page.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

OPEN = "open"
AWAITING_CONFIRMATION = "awaiting_confirmation"
CLOSED = "closed"
OPEN_STATES = (OPEN, AWAITING_CONFIRMATION)


def latest_tests(tests: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """Latest recorded row per test_type (input assumed in id order)."""
    latest: dict[str, Mapping[str, Any]] = {}
    for row in tests:
        latest[row["test_type"]] = row
    return latest


def retests_of(tests: Iterable[Mapping[str, Any]], test_type: str) -> list[Mapping[str, Any]]:
    rows = [t for t in tests if t["test_type"] == test_type]
    return rows[1:] if len(rows) > 1 else []


def is_blocked(inv: Mapping[str, Any], tests: Iterable[Mapping[str, Any]] | None = None) -> list[str]:
    """Reasons this investigation blocks formal release. Empty list = it does not block."""
    status = inv["status"]
    if status == CLOSED:
        return []
    if status == AWAITING_CONFIRMATION:
        return [f"调查 v{inv['version']} 结论待另一名质量人员确认"]
    # open: registration, passing retest and a submitted conclusion are all required
    reasons: list[str] = missing_registration(inv)
    reasons = [f"调查 v{inv['version']} 未登记{m}" for m in reasons]
    if tests is not None:
        latest = latest_tests(tests).get(inv["test_type"])
        if latest is None or not latest["passed"]:
            reasons.append(f"检验项目 {inv['test_type']} 尚未复验合格")
    if not reasons:
        reasons.append(f"调查 v{inv['version']} 已具备条件但尚未提交调查结论")
    return reasons


def missing_registration(inv: Mapping[str, Any]) -> list[str]:
    missing = []
    if not (inv.get("reason") or "").strip():
        missing.append("异常原因")
    if not (inv.get("retest_plan") or "").strip():
        missing.append("复验方案")
    if not (inv.get("owner") or "").strip():
        missing.append("负责人")
    return missing


def conclusion_blockers(inv: Mapping[str, Any], tests: Iterable[Mapping[str, Any]]) -> list[str]:
    """What prevents submitting the investigation conclusion."""
    blockers = missing_registration(inv)
    latest = latest_tests(tests).get(inv["test_type"])
    if latest is None:
        blockers.append("缺少检验结果")
    elif not latest["passed"]:
        blockers.append(f"检验项目 {inv['test_type']} 最新结果仍不合格，复验未通过")
    elif not retests_of(tests, inv["test_type"]):
        blockers.append("只有首检记录，尚无复验结果回写")
    return blockers


def release_blockers(investigations: Iterable[Mapping[str, Any]],
                     tests_by_batch: Mapping[int, Iterable[Mapping[str, Any]]] | None = None) -> list[str]:
    """Aggregate investigation reasons that stop formal release of a batch."""
    blockers: list[str] = []
    for inv in investigations:
        tests = (tests_by_batch or {}).get(inv["batch_id"])
        blockers.extend(is_blocked(inv, tests))
    return blockers
