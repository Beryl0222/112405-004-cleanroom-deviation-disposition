"""端到端行为测试：基于合成演练事件流验证判定纪律。"""

import json
import unittest
from datetime import datetime
from pathlib import Path

from qapipeline.models import Disposition, FactCategory, RuleCode
from qapipeline.processor import Processor

EVENTS_PATH = Path(__file__).parents[1] / "data" / "demo" / "events.jsonl"
TZ = "+08:00"


def load_events() -> list[dict]:
    return [json.loads(line) for line in EVENTS_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_processor(stop_before: str | None = None) -> Processor:
    """重放事件；``stop_before`` 给出事件 ID，到达该事件前停止（模拟中途状态）。"""

    processor = Processor()
    for event in load_events():
        if stop_before and event["event_id"] == stop_before:
            break
        processor.handle(event)
    now = datetime.fromisoformat(f"2026-11-19T10:00:00{TZ}")
    for batch_id in processor.batches:
        processor.recompute(batch_id, now)
    return processor


class EndToEndTest(unittest.TestCase):
    def test_post_batch_calibration_cannot_backfill_release(self):
        # 复检证据到达之前（EV-023 起为复检阶段），R-22 必须暂缓
        processor = build_processor(stop_before="EV-023")
        decision = processor.decisions["R-22"]
        self.assertEqual(decision.disposition, Disposition.HOLD)
        rules = {f.rule for f in decision.findings if f.blocking}
        self.assertIn(RuleCode.R02_READING_PRECALIBRATION, rules)
        self.assertIn(RuleCode.R01_REVIEW_WRONG_VERSION, rules)
        self.assertIn(RuleCode.R04_FIRST_ANOMALY_MISSING, rules)
        # 校准确实发生在批次结束之后
        batch = processor.batches["R-22"]
        calib = processor.timeline.calibration_after_batch(batch, "EQ-EC-02")
        self.assertIsNotNone(calib)
        self.assertGreater(calib.occurred_at, batch.end_at)
        # 必须产生未关闭复检
        open_ri = {ri.ri_id for ri in processor.open_reinspections() if ri.batch_id == "R-22"}
        self.assertTrue(open_ri)

    def test_unknown_timing_never_treated_as_good(self):
        processor = build_processor()
        decision = processor.decisions["R-23"]
        self.assertEqual(decision.disposition, Disposition.HOLD)
        rules = {f.rule for f in decision.findings if f.blocking}
        self.assertIn(RuleCode.R03_READING_TIMING_UNKNOWN, rules)
        open_ri = [ri for ri in processor.open_reinspections() if ri.batch_id == "R-23"]
        self.assertEqual(len(open_ri), 1)
        # 建议类事实不能关闭复检
        self.assertEqual(open_ri[0].status.value, "open")

    def test_reinspection_closure_requires_execution_and_review(self):
        processor = build_processor()
        decision = processor.decisions["R-22"]
        self.assertEqual(decision.disposition, Disposition.RELEASE)
        self.assertEqual(decision.open_ri_ids, [])
        closed = [ri for ri in processor.reinspections.values() if ri.batch_id == "R-22"]
        self.assertTrue(closed)
        for ri in closed:
            self.assertEqual(ri.status.value, "closed")
            # 关闭依据必须同时包含复检执行事实与有效版本复核事实
            closing = processor._all_facts
            by_id = {f.fact_id: f for f in closing}
            cats = {by_id[fid].category for fid in ri.closed_by_fact_ids if fid in by_id}
            self.assertIn(FactCategory.EXECUTION, cats)
            self.assertIn(FactCategory.REVIEW, cats)

    def test_old_batch_interpreted_under_version_in_force_at_that_time(self):
        processor = build_processor()
        old = processor.decisions["R-18"]
        self.assertEqual((old.applicable_card_id, old.applicable_version), ("PC-6", "1"))
        self.assertEqual(old.disposition, Disposition.RELEASE)
        # 新批次必须按 PC-7 解释
        new = processor.decisions["R-24"]
        self.assertEqual((new.applicable_card_id, new.applicable_version), ("PC-7", "1"))

    def test_clean_batch_releases_and_uses_tightened_limit(self):
        processor = build_processor()
        decision = processor.decisions["R-24"]
        self.assertEqual(decision.disposition, Disposition.RELEASE)
        self.assertFalse(any(f.blocking for f in decision.findings))

    def test_recommendation_execution_review_never_merged(self):
        processor = build_processor()
        r23_facts = processor.facts["R-23"]
        categories = {f.category for f in r23_facts}
        self.assertIn(FactCategory.RECOMMENDATION, categories)
        self.assertIn(FactCategory.REVIEW, categories)
        self.assertIn(FactCategory.OBSERVATION, categories)
        # 每条事实只属于一个类别，且建议类没有读数合格性效力
        for fact in r23_facts:
            self.assertIsInstance(fact.category, FactCategory)
        warnings = [f for f in processor.decisions["R-23"].findings
                    if f.rule is RuleCode.W01_RECOMMENDATION_NOT_EVIDENCE]
        self.assertTrue(warnings)
        self.assertFalse(any(f.blocking for f in warnings))

    def test_affected_batches_and_open_items_after_correction(self):
        processor = build_processor()
        affected = {item["batch_id"]: item for item in processor.affected_batches("EQ-EC-02")}
        self.assertEqual(set(affected), {"R-18", "R-22", "R-23", "R-24"})
        # R-22 纠正后不再挂起，R-23 仍有未关闭复检
        self.assertEqual(affected["R-22"]["disposition"], "release")
        self.assertEqual(affected["R-22"]["open_ri_ids"], [])
        self.assertEqual(affected["R-23"]["disposition"], "hold")
        self.assertEqual(len(affected["R-23"]["open_ri_ids"]), 1)
        # 全局未关闭事项清单
        open_items = processor.open_reinspections()
        self.assertEqual({ri.batch_id for ri in open_items}, {"R-23"})

    def test_limit_uses_effective_version(self):
        # R-22 设备读数 4.6：按 PC-6(≤5.0) 不超限，但按 PC-7(≤4.0) 超限
        processor = build_processor(stop_before="EV-023")
        facts = [f for f in processor.facts["R-22"]
                 if f.predicate == "reading" and f.value == 4.6]
        self.assertEqual(len(facts), 1)
        card = processor.procedures.effective_for_step("STEP-RINSE",
                                                        processor.batches["R-22"].end_at)
        self.assertEqual(card.card_id, "PC-7")
        # 该读数首先因校准前（R02）被拦截；若校准关系满足则 R05 生效
        from qapipeline.models import CalibrationRelation
        self.assertEqual(processor.timeline.relation_for_fact(facts[0]),
                         CalibrationRelation.BEFORE)
        decision = processor.decisions["R-22"]
        rules = {f.rule for f in decision.findings if f.blocking}
        self.assertIn(RuleCode.R02_READING_PRECALIBRATION, rules)


if __name__ == "__main__":
    unittest.main()
