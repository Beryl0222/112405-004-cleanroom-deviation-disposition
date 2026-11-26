"""领域模型。

所有被流水线处理的对象都保留来源标识（``source_id`` / ``record_id``），
以便每个结论都能回溯到原始叙述、设备日志或受控工艺条款。

本模块只定义数据结构与枚举，不做业务推断；推断规则见
:mod:`qapipeline.engine`。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class SourceType(str, Enum):
    """记录来源类型。三类来源在任何环节都不得互相改写。"""

    OPERATOR = "operator-report"        # 操作员现场叙述
    EQUIPMENT = "equipment-log"         # 设备工程师 / 设备日志
    QUALITY = "review-note"             # 质量审查员复核
    PROCEDURE = "controlled-procedure"  # 受控工艺条款
    SYSTEM = "system"                   # 流水线自身产生的事件（如复检登记）


class FactCategory(str, Enum):
    """事实类别。

    操作建议（RECOMMENDATION）、实际执行（OBSERVATION / EXECUTION）与
    复核结论（REVIEW）是三种不可互相合成的事实：一条建议不能被当成
    已发生的操作，一条复核结论也不能替代现场读数。
    """

    OBSERVATION = "observation"          # 操作员观察 / 现场叙述
    EXECUTION = "execution"              # 设备实际执行的动作或读数
    RECOMMENDATION = "recommendation"    # 操作建议（尚未执行）
    REVIEW = "review"                    # 质量复核结论
    REFERENCE = "reference"              # 对受控文件 / 条款的引用


class CalibrationRelation(str, Enum):
    """读数与设备校准窗口的时间关系。"""

    BEFORE = "before-calibration"  # 读数发生在批次结束后的校准之前（不可放行）
    AFTER = "after-calibration"    # 读数发生在有效校准之后（可作为依据）
    UNKNOWN = "unknown"            # 叙述无法确定先后（不得倒推为合格）


class RIStatus(str, Enum):
    """复检事项状态。"""

    OPEN = "open"
    CLOSED = "closed"


class Disposition(str, Enum):
    """批次流转判定。"""

    RELEASE = "release"          # 可继续流转
    HOLD = "hold"                # 暂缓，需完成复检 / 纠正
    INSUFFICIENT = "insufficient"  # 证据不足，不能做出放行判断


class RuleCode(str, Enum):
    """判定规则编号。规则语义集中在 :mod:`qapipeline.engine`。"""

    R01_REVIEW_WRONG_VERSION = "R01-引用了非当时有效工艺版本"
    R02_READING_PRECALIBRATION = "R02-读数早于批次后校准"
    R03_READING_TIMING_UNKNOWN = "R03-读数与校准先后不明"
    R04_FIRST_ANOMALY_MISSING = "R04-首次异常读数缺失"
    R05_LIMIT_EXCEEDED = "R05-读数超出有效版本限值"
    R06_OPEN_REINSPECTION = "R06-存在未关闭复检事项"
    R07_REVIEW_MISSING = "R07-缺少引用有效版本的复核结论"
    W01_RECOMMENDATION_NOT_EVIDENCE = "W01-建议类表述不得作为执行证据"
    W02_CROSS_ALIAS_RESOLVED = "W02-交叉别名已归并"


@dataclass(frozen=True)
class Fact:
    """从一条来源记录中拆分出的单条事实。

    一条叙述可拆成多条事实（建议、执行、复核分开），每条事实都带
    自己的子句序号与原文，保证可回溯。
    """

    fact_id: str
    record_id: str
    source: SourceType
    category: FactCategory
    text: str
    occurred_at: datetime | None = None
    batch_id: str | None = None
    equipment_id: str | None = None
    step_id: str | None = None
    predicate: str | None = None
    value: float | None = None
    unit: str | None = None
    referenced_card_id: str | None = None
    referenced_version: str | None = None
    clause_index: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["source"] = self.source.value
        data["category"] = self.category.value
        data["occurred_at"] = self.occurred_at.isoformat() if self.occurred_at else None
        return data


@dataclass
class Batch:
    batch_id: str
    step_id: str
    start_at: datetime
    end_at: datetime
    product: str = ""
    facts: list[Fact] = field(default_factory=list)


@dataclass(frozen=True)
class EquipmentEvent:
    """设备日志事件（校准 / 报警 / 读数）。

    设备日志属于实际执行类证据，与操作员叙述、质量复核分开存放。
    ``valid_to`` 表示本次校准的有效区间终点：设备漂移 / 下一次校准
    都会终止前一次校准的有效性；缺省表示有效至下一次校准。
    """

    event_id: str
    equipment_id: str
    event_type: str  # calibration / reading / alarm
    occurred_at: datetime
    record_id: str
    detail: str = ""
    before_value: float | None = None
    after_value: float | None = None
    unit: str | None = None
    batch_id: str | None = None
    valid_to: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["occurred_at"] = self.occurred_at.isoformat()
        return data


@dataclass(frozen=True)
class ProcessCard:
    """受控工艺卡的一个版本。

    ``effective_from`` / ``effective_to`` 界定版本有效期；旧批次永远按
    当时有效版本解释（见 :class:`qapipeline.procedures.ProcedureRegistry`)。
    """

    card_id: str
    version: str
    step_id: str
    effective_from: datetime
    effective_to: datetime | None
    limits: dict[str, float] = field(default_factory=dict)
    clauses: tuple[str, ...] = ()
    record_id: str = ""

    def in_force_at(self, moment: datetime) -> bool:
        if moment < self.effective_from:
            return False
        return self.effective_to is None or moment <= self.effective_to


@dataclass
class Finding:
    """一条判定发现，绑定规则编号与证据事实编号。

    阻断标记可在纠正措施（复检关闭 / 有效复核补齐）后降级为非阻断
    信息项，但发现本身与证据编号始终保留。
    """

    rule: RuleCode
    blocking: bool
    message: str
    evidence_fact_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule.value,
            "blocking": self.blocking,
            "message": self.message,
            "evidence_fact_ids": list(self.evidence_fact_ids),
        }


@dataclass
class ReinspectionItem:
    """复检事项。

    只有执行类复检事实（EXECUTION）加上引用有效版本的复核事实（REVIEW）
    同时到位才能关闭；建议类事实不产生关闭效果。
    """

    ri_id: str
    batch_id: str
    reason_rule: RuleCode
    description: str
    status: RIStatus = RIStatus.OPEN
    opened_from_fact_ids: tuple[str, ...] = ()
    closed_by_fact_ids: tuple[str, ...] = ()
    opened_at: datetime | None = None
    closed_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ri_id": self.ri_id,
            "batch_id": self.batch_id,
            "reason_rule": self.reason_rule.value,
            "description": self.description,
            "status": self.status.value,
            "opened_from_fact_ids": list(self.opened_from_fact_ids),
            "closed_by_fact_ids": list(self.closed_by_fact_ids),
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
        }


@dataclass
class BatchDecision:
    """一个批次在当前证据状态下的判定结果（可随新事实重算）。"""

    batch_id: str
    disposition: Disposition
    applicable_card_id: str | None
    applicable_version: str | None
    findings: list[Finding]
    open_ri_ids: list[str]
    closed_ri_ids: list[str]
    recomputed_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "disposition": self.disposition.value,
            "applicable_card_id": self.applicable_card_id,
            "applicable_version": self.applicable_version,
            "findings": [f.to_dict() for f in self.findings],
            "open_ri_ids": list(self.open_ri_ids),
            "closed_ri_ids": list(self.closed_ri_ids),
            "recomputed_at": self.recomputed_at.isoformat(),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
