"""组件级单元测试。"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

CN_TZ = timezone(timedelta(hours=8))


def cn(*args) -> datetime:
    return datetime(*args, tzinfo=CN_TZ)

from qapipeline.aliases import AliasRegistry, EntityType
from qapipeline.extraction import parse_record
from qapipeline.models import CalibrationRelation, FactCategory, SourceType
from qapipeline.procedures import ProcedureRegistry
from qapipeline.models import ProcessCard
from qapipeline.timeline import EquipmentTimeline, EquipmentEvent
from qapipeline.processor import Processor
from qapipeline.store import EventStore
from qapipeline.watcher import TailRunner


class AliasTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = AliasRegistry()
        self.registry.register(EntityType.EQUIPMENT, "EQ-EC-02", "电导探头", "探头")
        self.registry.register(EntityType.STEP, "STEP-RINSE", "漂洗", "末道漂洗")

    def test_cross_mentions_in_one_clause_resolve_separately(self):
        text = "末道漂洗段2号电导探头读数异常"
        self.registry.register(EntityType.EQUIPMENT, "EQ-EC-02", "2号电导探头")
        mentions = {m.entity_type: m for m in self.registry.resolve_text(text)}
        self.assertTrue(mentions[EntityType.EQUIPMENT].resolved)
        self.assertEqual(mentions[EntityType.EQUIPMENT].entity_id, "EQ-EC-02")
        self.assertEqual(mentions[EntityType.STEP].entity_id, "STEP-RINSE")

    def test_ambiguous_mention_is_not_guessed(self):
        self.registry.register(EntityType.EQUIPMENT, "EQ-EC-09", "备用探头")
        self.registry.register(EntityType.EQUIPMENT, "EQ-EC-10", "备用探头")
        resolved = self.registry.resolve(EntityType.EQUIPMENT, "备用探头")
        self.assertTrue(resolved.ambiguous)
        self.assertIsNone(resolved.entity_id)
        self.assertEqual(len(resolved.candidates), 2)

    def test_unknown_mention(self):
        resolved = self.registry.resolve(EntityType.EQUIPMENT, "不存在的仪表")
        self.assertFalse(resolved.resolved)
        self.assertFalse(resolved.ambiguous)


class ExtractionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = AliasRegistry()
        self.registry.register(EntityType.EQUIPMENT, "EQ-EC-02", "2号电导探头", "电导探头", "探头")
        self.registry.register(EntityType.STEP, "STEP-RINSE", "漂洗", "末道漂洗")

    def test_recommendation_observation_review_split(self):
        record = {
            "id": "T-1",
            "kind": "operator-report",
            "date": "2026-11-17",
            "batch_id": "B1",
            "text": "在线电导仪报警后冲洗，冲洗后读数回到范围内；建议对本批末道漂洗重新取样检测。",
        }
        facts = parse_record(record, self.registry)
        cats = {f.category for f in facts}
        self.assertIn(FactCategory.OBSERVATION, cats)
        self.assertIn(FactCategory.RECOMMENDATION, cats)
        # 建议和观察必须是不同事实，不能合成同一事实
        self.assertNotEqual(
            next(f for f in facts if f.category is FactCategory.RECOMMENDATION).fact_id,
            next(f for f in facts if f.category is FactCategory.OBSERVATION).fact_id,
        )
        reading = next(f for f in facts if f.predicate == "qualitative-reading")
        self.assertIsNone(reading.occurred_at)  # 无时间信息，留给引擎判 UNKNOWN

    def test_numeric_reading_and_equipment_execution(self):
        record = {
            "id": "T-2",
            "kind": "equipment-log",
            "date": "2026-11-16",
            "batch_id": "B1",
            "text": "设备日志：14:20 探头校准完成，校准前偏差 0.18，校准后 0.03。",
        }
        facts = parse_record(record, self.registry)
        self.assertTrue(all(f.category is FactCategory.EXECUTION for f in facts))
        calib = next(f for f in facts if f.predicate == "calibration")
        self.assertEqual(calib.equipment_id, "EQ-EC-02")
        self.assertEqual(calib.occurred_at, cn(2026, 11, 16, 14, 20))

    def test_review_with_card_reference_emits_reference_fact(self):
        record = {
            "id": "T-3",
            "kind": "review-note",
            "date": "2026-11-16",
            "batch_id": "B1",
            "text": "质量补记：批次暂缓流转，引用工艺卡 PC-6。",
        }
        facts = parse_record(record, self.registry)
        review = next(f for f in facts if f.category is FactCategory.REVIEW)
        self.assertEqual(review.referenced_card_id, "PC-6")
        reference = next(f for f in facts if f.category is FactCategory.REFERENCE)
        self.assertEqual(reference.referenced_card_id, "PC-6")

    def test_time_digits_not_parsed_as_reading_value(self):
        record = {
            "id": "T-4",
            "kind": "operator-report",
            "date": "2026-11-16",
            "batch_id": "B1",
            "text": "09:10 冲洗后电导回到范围内。",
        }
        facts = parse_record(record, self.registry)
        reading = next(f for f in facts if f.predicate == "qualitative-reading")
        self.assertIsNone(reading.value)


class TimelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.timeline = EquipmentTimeline()
        self.timeline.add_event(EquipmentEvent(
            event_id="C1", equipment_id="EQ-EC-02", event_type="calibration",
            occurred_at=datetime(2026, 11, 16, 14, 20), record_id="L1",
        ))

    def test_relation_unknown_without_timestamp(self):
        from qapipeline.models import Fact
        fact = Fact(fact_id="F1", record_id="R1", source=SourceType.OPERATOR,
                    category=FactCategory.OBSERVATION, text="读数回到范围内",
                    equipment_id="EQ-EC-02", predicate="qualitative-reading")
        self.assertEqual(self.timeline.relation_for_fact(fact), CalibrationRelation.UNKNOWN)

    def test_relation_before_and_after_calibration(self):
        self.assertEqual(
            self.timeline.relation("EQ-EC-02", datetime(2026, 11, 16, 9, 10)),
            CalibrationRelation.BEFORE,
        )
        self.assertEqual(
            self.timeline.relation("EQ-EC-02", datetime(2026, 11, 17, 9, 0)),
            CalibrationRelation.AFTER,
        )

    def test_no_calibration_history_means_before(self):
        self.assertEqual(
            self.timeline.relation("EQ-OTHER", datetime(2026, 11, 16, 9, 10)),
            CalibrationRelation.BEFORE,
        )


class ProcedureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ProcedureRegistry()
        self.registry.add_card(ProcessCard(
            card_id="PC-6", version="1", step_id="S1",
            effective_from=datetime(2026, 5, 1), effective_to=datetime(2026, 8, 14, 23, 59),
        ))
        self.registry.add_card(ProcessCard(
            card_id="PC-7", version="1", step_id="S1",
            effective_from=datetime(2026, 8, 15), effective_to=None,
        ))

    def test_old_batch_keeps_old_version(self):
        card = self.registry.effective_for_step("S1", datetime(2026, 7, 10, 12, 0))
        self.assertEqual(card.card_id, "PC-6")

    def test_new_batch_uses_new_version(self):
        card = self.registry.effective_for_step("S1", datetime(2026, 11, 16, 12, 0))
        self.assertEqual(card.card_id, "PC-7")

    def test_no_coverage(self):
        self.assertIsNone(self.registry.effective_for_step("S1", datetime(2026, 1, 1)))


class ContinuousProcessingTest(unittest.TestCase):
    def test_idempotent_replay_and_late_fact_recompute(self):
        events = [
            {"event_id": "E1", "type": "batch-registered", "batch_id": "B1",
             "step_id": "STEP-RINSE",
             "start_at": "2026-11-17T08:00:00+08:00",
             "end_at": "2026-11-17T12:00:00+08:00"},
            {"event_id": "E2", "type": "record-ingested", "date": "2026-11-17",
             "batch_id": "B1",
             "record": {"id": "DV-X", "kind": "review-note",
                        "text": "质量补记：批次 B1 暂缓流转，引用工艺卡 PC-7（v1）。"}},
        ]
        p1 = Processor()
        p1.replay(events)
        p1.replay(events)  # 重复重放不产生重复事实
        self.assertEqual(len(p1.facts["B1"]), 2)  # REVIEW + REFERENCE 各一条

        p2 = Processor()
        p2.handle(events[0])
        before = p2.recompute("B1", datetime(2026, 11, 17, 13, 0))
        # 只有批次、没有有效复核事实时证据不足
        from qapipeline.models import Disposition
        self.assertEqual(before.disposition, Disposition.INSUFFICIENT)
        p2.handle(events[1])  # 迟到事实
        after = p2.recompute("B1", datetime(2026, 11, 17, 13, 0))
        self.assertNotEqual(after.disposition, Disposition.INSUFFICIENT)

    def test_event_store_dedup_and_tail_runner_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            feed = tmp / "feed.jsonl"
            store_path = tmp / "store.jsonl"
            offset = tmp / "offset.json"
            event = {"event_id": "X1", "type": "batch-registered", "batch_id": "B9",
                     "step_id": "STEP-RINSE",
                     "start_at": "2026-11-18T08:00:00+08:00",
                     "end_at": "2026-11-18T12:00:00+08:00"}
            feed.write_text(json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8")

            store = EventStore(store_path)
            runner = TailRunner(Processor(), store, [feed], offset)
            self.assertEqual(runner.drain_once(), 1)
            self.assertEqual(runner.drain_once(), 0)  # 偏移记录生效，不重复
            self.assertFalse(store.append(event))      # 存储层幂等

            # 新进程从偏移文件恢复，追加的新事件能继续处理
            runner2 = TailRunner(Processor(), EventStore(store_path), [feed], offset)
            event2 = dict(event, event_id="X2")
            with feed.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event2, ensure_ascii=False) + "\n")
            self.assertEqual(runner2.drain_once(), 1)
            self.assertIn("B9", runner2.processor.batches)


if __name__ == "__main__":
    unittest.main()
