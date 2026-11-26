"""偏差叙述的确定性抽取。

输入是一条来源记录（操作员报告 / 设备日志 / 质量补记的原始文字），
输出是若干条 :class:`~qapipeline.models.Fact`。设计原则：

1. 只做确定性的关键词与数值抽取，不做语义猜测；
2. 一条叙述中的不同主张拆成独立事实，``建议``、设备 ``执行``、
   质量 ``复核`` 进入不同的 :class:`~qapipeline.models.FactCategory`，
   永远不会合并成同一条事实；
3. 每条事实保留记录编号、子句序号与原文，结论可逐条回溯；
4. 设备别名与工序名在同一子句交叉出现时，分别解析并回填标准 ID，
   解析歧义时留空而不是猜一个实体。
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone

from .aliases import AliasRegistry, EntityType
from .models import Fact, FactCategory, SourceType

# 演练现场统一使用东八区；叙述里的钟点按该时区解释
CN_TZ = timezone(timedelta(hours=8))

_CLAUSE_SPLIT = re.compile(r"[；;。\n]+|，(?=建议|复检安排|复核意见)")
_TIME_RE = re.compile(r"(\d{1,2}:\d{2})")
_NUMERIC_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(μS/cm|uS/cm|ppb|%|℃)")
_CALIB_PAIR_RE = re.compile(r"校准(前|后)[^0-9]{0,6}(\d+(?:\.\d+)?)")
_CARD_RE = re.compile(r"工艺卡\s*([A-Za-z]+-\d+)(?:[（(]v?(\d+(?:\.\d+)?)[)）])?")

_SOURCE_MAP = {
    "operator-report": SourceType.OPERATOR,
    "equipment-log": SourceType.EQUIPMENT,
    "review-note": SourceType.QUALITY,
    "controlled-procedure": SourceType.PROCEDURE,
}

_RECOMMEND_MARKERS = ("建议", "复检安排", "应重做", "需复检")
_REVIEW_MARKERS = ("质量补记", "复核", "审查", "同意放行", "暂缓流转", "结论")
_EXECUTION_MARKERS = ("设备日志", "校准完成", "系统记录", "日志显示")
_LIMIT_HINT = ("上限", "下限", "限值", "范围")


def _parse_time(text: str, default_date: datetime) -> datetime | None:
    match = _TIME_RE.search(text)
    if not match:
        return None
    hour, minute = (int(part) for part in match.group(1).split(":"))
    base = default_date.astimezone(CN_TZ) if default_date.tzinfo else default_date.replace(tzinfo=CN_TZ)
    return datetime.combine(base.date(), time(hour=hour, minute=minute), tzinfo=CN_TZ)


def _parse_card_reference(text: str) -> tuple[str | None, str | None]:
    match = _CARD_RE.search(text)
    if not match:
        return None, None
    version = match.group(2)
    return match.group(1), version


def _first_numeric(text: str) -> tuple[float | None, str | None]:
    for match in _NUMERIC_RE.finditer(text):
        # 纯时间数字（如 09:10）已在分句中保留，这里跳过时间片段
        if _TIME_RE.match(text, match.start()):
            continue
        return float(match.group(1)), match.group(2)
    return None, None


def _classify(text: str, source: SourceType) -> FactCategory:
    if any(marker in text for marker in _RECOMMEND_MARKERS):
        # 设备日志中的“复检安排：已执行/重新取样检测”是实际执行记录；
        # 明确的建议（“建议先…”）或“尚未执行/待确认”表述仍归入建议类
        if source is SourceType.EQUIPMENT and not (
            "尚未执行" in text or "待" in text or "建议" in text
        ):
            return FactCategory.EXECUTION
        return FactCategory.RECOMMENDATION
    if source is SourceType.EQUIPMENT or any(m in text for m in _EXECUTION_MARKERS):
        return FactCategory.EXECUTION
    if source is SourceType.QUALITY or any(m in text for m in _REVIEW_MARKERS):
        return FactCategory.REVIEW
    # 现场观察为默认类别
    return FactCategory.OBSERVATION


def _predicate(text: str) -> str:
    if "校准" in text:
        return "calibration"
    if "放行" in text:
        return "release-decision"
    if "暂缓" in text or "扣留" in text:
        return "hold-decision"
    if "复检" in text or "重做" in text:
        return "reinspection"
    # 带量纲的定量读数优先判定，避免被定性规则抢先匹配
    if _first_numeric(text)[0] is not None:
        return "reading"
    # 定性读数优先于报警：该子句的主张是“读数回到范围内”
    if "读数" in text or "回到范围内" in text or "回到线内" in text:
        return "qualitative-reading"
    if "报警" in text or "异常" in text:
        return "alarm"
    if "冲洗" in text or "漂洗" in text:
        return "flush"
    if any(hint in text for hint in _LIMIT_HINT):
        return "limit-statement"
    return "note"


def parse_record(
    record: dict,
    registry: AliasRegistry,
    default_date: datetime | None = None,
) -> list[Fact]:
    """解析一条来源记录为事实列表。

    ``record`` 至少包含 ``id``、``text`` 与 ``kind``（或 ``source``）；
    可选 ``date``(YYYY-MM-DD) 与 ``batch_id``。
    """

    record_id = record["id"]
    raw_source = record.get("kind") or record.get("source")
    source = _SOURCE_MAP.get(raw_source, SourceType.OPERATOR)
    default_date = default_date or datetime.now(tz=CN_TZ)
    if record.get("date"):
        default_date = datetime.fromisoformat(record["date"]).replace(tzinfo=CN_TZ)

    facts: list[Fact] = []
    clauses = [part.strip() for part in _CLAUSE_SPLIT.split(record["text"]) if part and part.strip()]
    # 先解析整段记录里出现过的实体，用于子句间回填（设备别名与工序名交叉出现）
    record_mentions = registry.resolve_text(record["text"])
    record_equipment = next(
        (m for m in record_mentions if m.entity_type is EntityType.EQUIPMENT and m.resolved), None
    )
    record_step = next(
        (m for m in record_mentions if m.entity_type is EntityType.STEP and m.resolved), None
    )
    fact_index = 0
    for clause_index, clause in enumerate(clauses):
        category = _classify(clause, source)
        mentions = registry.resolve_text(clause)
        equipment = next(
            (m for m in mentions if m.entity_type is EntityType.EQUIPMENT and m.resolved), None
        )
        step = next(
            (m for m in mentions if m.entity_type is EntityType.STEP and m.resolved), None
        )
        card_id, card_version = _parse_card_reference(clause)
        value, unit = _first_numeric(clause)
        occurred = _parse_time(clause, default_date)
        predicate = _predicate(clause)

        fact = Fact(
            fact_id=f"{record_id}-F{fact_index}",
            record_id=record_id,
            source=source,
            category=category,
            text=clause,
            occurred_at=occurred,
            batch_id=record.get("batch_id"),
            equipment_id=(equipment.entity_id if equipment else None)
            or (record_equipment.entity_id if record_equipment else None),
            step_id=(step.entity_id if step else None)
            or (record_step.entity_id if record_step else None),
            predicate=predicate,
            value=value,
            unit=unit,
            referenced_card_id=card_id,
            referenced_version=card_version,
            clause_index=clause_index,
        )
        facts.append(fact)
        fact_index += 1

        # 对工艺卡的引用单独再落一条 REFERENCE 事实，引用关系不与复核结论混写
        if card_id and category is not FactCategory.REFERENCE:
            facts.append(
                Fact(
                    fact_id=f"{record_id}-F{fact_index}",
                    record_id=record_id,
                    source=source,
                    category=FactCategory.REFERENCE,
                    text=clause,
                    occurred_at=occurred,
                    batch_id=record.get("batch_id"),
                    predicate="document-reference",
                    referenced_card_id=card_id,
                    referenced_version=card_version,
                    clause_index=clause_index,
                )
            )
            fact_index += 1
    return facts
