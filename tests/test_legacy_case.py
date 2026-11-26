"""既有合成素材 data/case.json 的适配测试。

原始素材只含自由文本记录与工艺卡编号，没有时间戳；适配层把它转成
事件流后，流水线应能识别“先后不明不可放行”等纪律，而不是默认合格。
"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from qapipeline.aliases import EntityType, build_default_registry
from qapipeline.models import Disposition, RuleCode
from qapipeline.processor import Processor

CN_TZ = timezone(timedelta(hours=8))
CASE_PATH = Path(__file__).parents[1] / "data" / "case.json"


def case_to_events(case: dict) -> list[dict]:
    registry = build_default_registry()
    registry.register(EntityType.EQUIPMENT, "EQ-EC-02", "电导探头", "探头")
    # 旧素材只给了生效日，适配层补齐为版本区间：PC-7 生效即 PC-6 失效
    effective_dates = sorted(
        (procedure["id"], datetime.fromisoformat(procedure["effective"]))
        for procedure in case["procedures"]
    )
    events: list[dict] = []
    seq = 0
    for index, (card_id, effective) in enumerate(effective_dates):
        seq += 1
        end = (
            effective_dates[index + 1][1] - timedelta(seconds=1)
            if index + 1 < len(effective_dates)
            else None
        )
        events.append({
            "event_id": f"LEGACY-P{seq}",
            "type": "procedure-published",
            "card_id": card_id,
            "version": "1",
            "step_id": "STEP-RINSE",
            "effective_from": effective.isoformat(),
            "effective_to": end.isoformat() if end else None,
            "limits": {"conductivity": 5.0},
        })
    for record in case["records"]:
        seq += 1
        events.append({
            "event_id": f"LEGACY-E{seq}",
            "type": "record-ingested",
            "date": "2026-11-16",
            "batch_id": "R-22",
            "record": {
                "id": record["id"],
                "kind": record["kind"],
                "text": record["text"],
            },
        })
    return events


class LegacyCaseTest(unittest.TestCase):
    def test_legacy_case_ingested_without_release_assumption(self):
        case = json.loads(CASE_PATH.read_text(encoding="utf-8"))
        processor = Processor()
        processor.handle({
            "event_id": "LEGACY-B1",
            "type": "batch-registered",
            "batch_id": "R-22",
            "step_id": "STEP-RINSE",
            "start_at": "2026-11-16T08:00:00+08:00",
            "end_at": "2026-11-16T12:00:00+08:00",
        })
        for event in case_to_events(case):
            processor.handle(event)
        decision = processor.recompute(
            "R-22", datetime(2026, 11, 16, 18, 0, tzinfo=CN_TZ)
        )
        # “回到范围内”的叙述不能支持放行：该读数早于批次结束后的校准
        self.assertEqual(decision.disposition, Disposition.HOLD)
        rules = {f.rule for f in decision.findings}
        self.assertIn(RuleCode.R02_READING_PRECALIBRATION, rules)
        # 质量员引用的 PC-6 不是 11 月批次的有效版本（PC-7 才是）
        self.assertIn(RuleCode.R01_REVIEW_WRONG_VERSION, rules)
        # 必须登记未关闭复检，而不是默认合格
        self.assertTrue(processor.open_reinspections())

    def test_legacy_material_test_still_passes(self):
        # 与 tests/test_materials.py 的稳定编号约定保持一致
        case = json.loads(CASE_PATH.read_text(encoding="utf-8"))
        ids = [r["id"] for r in case["records"]]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
