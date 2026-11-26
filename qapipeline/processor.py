"""持续处理核心：事件处理器。

处理器按事件流方式工作：批次登记、来源记录到达、设备日志到达、
工艺卡发布、复检登记/更新都以事件形式输入，每次处理后重算受影响
批次的判定。乱序到达与迟到事实同样支持——状态由事件重放得到，
判定始终基于当前全部证据重新计算。

事实三分流的约束在两个层面保证：

1. :mod:`qapipeline.extraction` 已把建议 / 执行 / 复核拆成不同类别的事实；
2. 引擎只承认 EXECUTION/OBSERVATION 为现场证据，RECOMMENDATION 不参与
   合格性判断，复检关闭必须同时具备“已执行的复检”与“引用有效版本的
   复核结论”。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .aliases import AliasRegistry, build_default_registry
from .engine import DecisionEngine
from .extraction import parse_record
from .procedures import ProcedureRegistry
from .timeline import EquipmentTimeline
from .models import (
    Batch,
    BatchDecision,
    CalibrationRelation,
    Disposition,
    EquipmentEvent,
    Fact,
    FactCategory,
    ProcessCard,
    ReinspectionItem,
    RIStatus,
    RuleCode,
    SourceType,
)

CN_TZ = timezone(timedelta(hours=8))


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        # 未注明时区的时间戳按演练现场东八区解释
        moment = moment.replace(tzinfo=CN_TZ)
    return moment.astimezone(CN_TZ)


class Processor:
    def __init__(self, registry: AliasRegistry | None = None) -> None:
        self.registry = registry or build_default_registry()
        self.timeline = EquipmentTimeline()
        self.procedures = ProcedureRegistry()
        self.batches: dict[str, Batch] = {}
        self.facts: dict[str, list[Fact]] = {}
        self._all_facts: list[Fact] = []
        self.reinspections: dict[str, ReinspectionItem] = {}
        self.decisions: dict[str, BatchDecision] = {}
        self.source_records: dict[str, dict] = {}
        # 设备固定服务的工序（用于受影响批次的时间窗口追踪）
        self.equipment_steps: dict[str, set[str]] = {}
        self.engine = DecisionEngine(self.procedures, self.timeline)
        self._processed_events: set[str] = set()

    # ------------------------------------------------------------------
    def handle(self, event: dict[str, Any]) -> list[BatchDecision]:
        """处理一个事件，返回本次被重算的批次判定。"""

        event_id = event["event_id"]
        if event_id in self._processed_events:
            return []  # 幂等：重复事件不产生副作用
        self._processed_events.add(event_id)
        etype = event["type"]
        affected: set[str] = set()

        if etype == "batch-registered":
            self._on_batch(event)
        elif etype == "record-ingested":
            affected.update(self._on_record(event))
        elif etype == "equipment-assignment":
            self._on_equipment_assignment(event)
        elif etype == "equipment-event":
            affected.update(self._on_equipment_event(event))
        elif etype == "procedure-published":
            self._on_procedure(event)
        elif etype == "reinspection-registered":
            self._on_ri_registered(event)
            affected.add(event["batch_id"])
        elif etype == "reinspection-updated":
            affected.update(self._on_ri_updated(event))
        else:
            raise ValueError(f"未知事件类型: {etype}")

        results: list[BatchDecision] = []
        for batch_id in sorted(affected):
            if batch_id in self.batches:
                results.append(self.recompute(batch_id))
        return results

    def replay(self, events: list[dict[str, Any]]) -> None:
        for event in events:
            self.handle(event)

    # ------------------------------------------------------------------
    def _on_batch(self, event: dict) -> None:
        batch = Batch(
            batch_id=event["batch_id"],
            step_id=event["step_id"],
            start_at=_parse_dt(event["start_at"]),
            end_at=_parse_dt(event["end_at"]),
            product=event.get("product", ""),
        )
        self.batches[batch.batch_id] = batch
        self.facts.setdefault(batch.batch_id, [])

    def _on_record(self, event: dict) -> set[str]:
        record = dict(event["record"])
        self.source_records[record["id"]] = record
        default_date = _parse_dt(event.get("date")) or datetime.now(tz=CN_TZ)
        facts = parse_record(record, self.registry, default_date=default_date)
        affected: set[str] = set()
        for fact in facts:
            batch_id = fact.batch_id or event.get("batch_id")
            if not batch_id:
                continue
            self.facts.setdefault(batch_id, []).append(fact)
            self._all_facts.append(fact)
            affected.add(batch_id)
        return affected

    def _on_equipment_assignment(self, event: dict) -> None:
        bucket = self.equipment_steps.setdefault(event["equipment_id"], set())
        bucket.update(event["step_ids"])

    def _on_equipment_event(self, event: dict) -> set[str]:
        equipment_event = EquipmentEvent(
            event_id=event["event_id"],
            equipment_id=event["equipment_id"],
            event_type=event["equipment_type"],
            occurred_at=_parse_dt(event["occurred_at"]),
            record_id=event.get("record_id", event["event_id"]),
            detail=event.get("detail", ""),
            before_value=event.get("before_value"),
            after_value=event.get("after_value"),
            unit=event.get("unit"),
            batch_id=event.get("batch_id"),
            valid_to=_parse_dt(event.get("valid_to")),
        )
        self.timeline.add_event(equipment_event)
        affected: set[str] = set()
        if event.get("batch_id"):
            affected.add(event["batch_id"])
            # 设备读数事件同时落成 EXECUTION 事实：设备日志是独立来源，
            # 其读数与操作员叙述、质量复核分开参与判定
            if event["equipment_type"] == "reading":
                self.facts.setdefault(event["batch_id"], []).append(Fact(
                    fact_id=f"{event['event_id']}-READING",
                    record_id=event.get("record_id", event["event_id"]),
                    source=SourceType.EQUIPMENT,
                    category=FactCategory.EXECUTION,
                    text=event.get("detail", "设备读数"),
                    occurred_at=equipment_event.occurred_at,
                    batch_id=event["batch_id"],
                    equipment_id=event["equipment_id"],
                    predicate="reading",
                    value=event.get("after_value"),
                    unit=event.get("unit"),
                ))
                self._all_facts.append(self.facts[event["batch_id"]][-1])
        return affected

    def _on_procedure(self, event: dict) -> None:
        card = ProcessCard(
            card_id=event["card_id"],
            version=str(event["version"]),
            step_id=event["step_id"],
            effective_from=_parse_dt(event["effective_from"]),
            effective_to=_parse_dt(event.get("effective_to")),
            limits=dict(event.get("limits", {})),
            clauses=tuple(event.get("clauses", ())),
            record_id=event.get("record_id", event["event_id"]),
        )
        self.procedures.add_card(card)

    def _on_ri_registered(self, event: dict) -> None:
        rule = RuleCode(event["reason_rule"])
        ri = ReinspectionItem(
            ri_id=event["ri_id"],
            batch_id=event["batch_id"],
            reason_rule=rule,
            description=event["description"],
            status=RIStatus.OPEN,
            opened_from_fact_ids=tuple(event.get("opened_from_fact_ids", ())),
            opened_at=_parse_dt(event.get("opened_at")) or datetime.now(tz=CN_TZ),
        )
        self.reinspections[ri.ri_id] = ri

    def _on_ri_updated(self, event: dict) -> set[str]:
        ri = self.reinspections[event["ri_id"]]
        if event.get("status") == RIStatus.CLOSED.value:
            ri.status = RIStatus.CLOSED
            ri.closed_at = _parse_dt(event.get("closed_at")) or datetime.now(tz=CN_TZ)
            ri.closed_by_fact_ids = tuple(
                event.get("closed_by_fact_ids", ri.closed_by_fact_ids)
            )
        return {ri.batch_id}

    # ------------------------------------------------------------------
    def recompute(self, batch_id: str, now: datetime | None = None) -> BatchDecision:
        now = now or datetime.now(tz=CN_TZ)
        batch = self.batches[batch_id]
        facts = list(self.facts.get(batch_id, []))
        self._sync_reinspections(batch_id, facts, now)
        self._try_close_reinspections(batch_id, facts, now)
        decision = self.engine.evaluate(
            batch=batch,
            facts=facts,
            reinspections=list(self.reinspections.values()),
            now=now,
        )
        self.decisions[batch_id] = decision
        return decision

    def _sync_reinspections(self, batch_id: str, facts: list[Fact], now: datetime) -> None:
        """根据阻断性发现自动登记复检事项（按 批次+规则 去重）。"""

        provisional = self.engine.evaluate(
            self.batches[batch_id], facts,
            list(self.reinspections.values()), now,
        )
        rules = self.engine.rules_requiring_reinspection()
        existing = {ri.reason_rule for ri in self.reinspections.values()
                    if ri.batch_id == batch_id}
        for finding in provisional.findings:
            if finding.rule not in rules or finding.rule in existing:
                continue
            if self._is_suppressed(batch_id, finding.rule):
                continue
            ri_id = f"RI-{batch_id}-{finding.rule.name.split('_')[0]}"
            self.reinspections[ri_id] = ReinspectionItem(
                ri_id=ri_id,
                batch_id=batch_id,
                reason_rule=finding.rule,
                description=finding.message,
                status=RIStatus.OPEN,
                opened_from_fact_ids=finding.evidence_fact_ids,
                opened_at=now,
            )
            existing.add(finding.rule)

    def _is_suppressed(self, batch_id: str, rule: RuleCode) -> bool:
        return any(
            ri.batch_id == batch_id
            and ri.reason_rule is rule
            and ri.status is RIStatus.CLOSED
            for ri in self.reinspections.values()
        )

    def _try_close_reinspections(self, batch_id: str, facts: list[Fact], now: datetime) -> None:
        """复检关闭条件：已执行的复检 + 引用有效版本的复核，二者缺一不可。"""

        batch = self.batches[batch_id]
        card = self.procedures.effective_for_step(batch.step_id, batch.end_at)
        retest_facts = [
            f for f in facts
            if f.predicate == "reinspection"
            and f.category in (FactCategory.EXECUTION, FactCategory.OBSERVATION)
            and (not f.equipment_id
                 or self.timeline.relation_for_fact(f) is not CalibrationRelation.BEFORE)
        ]
        valid_reviews = [
            f for f in facts
            if f.category is FactCategory.REVIEW
            and card is not None
            and f.referenced_card_id == card.card_id
            and (f.referenced_version is None
                 or str(f.referenced_version) == str(card.version))
        ]
        if not retest_facts or not valid_reviews:
            return
        close_ids = tuple(f.fact_id for f in retest_facts + valid_reviews)
        close_time = max(
            (f.occurred_at for f in retest_facts + valid_reviews if f.occurred_at),
            default=now,
        )
        for ri in self.reinspections.values():
            if ri.batch_id == batch_id and ri.status is RIStatus.OPEN:
                ri.status = RIStatus.CLOSED
                ri.closed_at = close_time
                ri.closed_by_fact_ids = close_ids

    # ------------------------------------------------------------------
    def open_reinspections(self) -> list[ReinspectionItem]:
        return [ri for ri in self.reinspections.values() if ri.status is RIStatus.OPEN]

    def affected_batches(self, equipment_id: str) -> list[dict[str, Any]]:
        """返回某台设备涉及的批次及其当前判定 / 未关闭复检。

        纳入范围：
        1. 事实或设备事件直接关联该设备的批次；
        2. 与该设备同工序、且处于该设备任一校准 / 漂移时间窗口内的批次
           （设备失效可能影响其测量过的全部批次，即使叙述未点名设备）。
        """

        batch_ids = {
            fact.batch_id
            for fact in self._all_facts
            if fact.equipment_id == equipment_id and fact.batch_id
        }
        batch_ids.update(
            event.batch_id
            for event in self.timeline.events(equipment_id)
            if event.batch_id
        )
        equipment_steps = {
            fact.step_id for fact in self._all_facts
            if fact.equipment_id == equipment_id and fact.step_id
        }
        equipment_steps.update(self.equipment_steps.get(equipment_id, ()))
        windows = [
            (event.occurred_at, event.valid_to or self._next_calibration(equipment_id, event))
            for event in self.timeline.calibrations(equipment_id)
        ]
        for batch_id, batch in self.batches.items():
            if batch_id in batch_ids or batch.step_id not in equipment_steps:
                continue
            if any(start <= batch.end_at and (end is None or batch.start_at < end)
                   for start, end in windows):
                batch_ids.add(batch_id)
        result = []
        for batch_id in sorted(batch_ids):
            decision = self.decisions.get(batch_id)
            open_ri = [
                ri.ri_id for ri in self.reinspections.values()
                if ri.batch_id == batch_id and ri.status is RIStatus.OPEN
            ]
            result.append({
                "batch_id": batch_id,
                "disposition": decision.disposition.value if decision else None,
                "open_ri_ids": open_ri,
                "blocking_rules": [
                    finding.rule.value for finding in decision.findings
                    if finding.blocking
                ] if decision else [],
            })
        return result

    def _next_calibration(self, equipment_id: str, event) -> datetime | None:
        calibrations = self.timeline.calibrations(equipment_id)
        later = [c for c in calibrations if c.occurred_at > event.occurred_at]
        return later[0].occurred_at if later else None

    def held_batches(self) -> list[str]:
        return [
            batch_id for batch_id, decision in self.decisions.items()
            if decision.disposition in (Disposition.HOLD, Disposition.INSUFFICIENT)
        ]
