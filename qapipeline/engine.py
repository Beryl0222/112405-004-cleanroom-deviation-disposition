"""批次流转判定引擎。

引擎只消费已经带来源的事实与设备事件，输出绑定证据编号的发现
（:class:`~qapipeline.models.Finding`）与判定。关键纪律：

* 操作建议（RECOMMENDATION）不参与合格性判断，只产生提示 W01；
* 实际读数必须有设备时间线支撑：校准前 → R02，先后不明 → R03，
  两者都是阻断项，且“后来正常”不能倒推为本批合格；
* 复核结论引用非当时有效工艺版本 → R01 阻断；
* 限值按批次当时有效版本的条款解释；
* 任一阻断项或未关闭复检事项存在时，批次只能暂缓（HOLD）；
  完全没有可判断证据时给 INSUFFICIENT，而不是默认放行。
"""

from __future__ import annotations

from datetime import datetime

from .procedures import ProcedureRegistry
from .timeline import EquipmentTimeline
from .models import (
    Batch,
    BatchDecision,
    CalibrationRelation,
    Disposition,
    Fact,
    FactCategory,
    Finding,
    ReinspectionItem,
    RuleCode,
)

# 读数单位 -> 工艺卡限值参数名
_UNIT_PARAM = {
    "μS/cm": "conductivity",
    "uS/cm": "conductivity",
    "ppb": "toc",
}

# 哪些规则触发时必须登记复检事项
_REINSPECTION_RULES = {
    RuleCode.R02_READING_PRECALIBRATION,
    RuleCode.R03_READING_TIMING_UNKNOWN,
    RuleCode.R04_FIRST_ANOMALY_MISSING,
    RuleCode.R05_LIMIT_EXCEEDED,
}


class DecisionEngine:
    def __init__(
        self,
        procedures: ProcedureRegistry,
        timeline: EquipmentTimeline,
    ) -> None:
        self.procedures = procedures
        self.timeline = timeline

    # ------------------------------------------------------------------
    def evaluate(
        self,
        batch: Batch,
        facts: list[Fact],
        reinspections: list[ReinspectionItem],
        now: datetime,
    ) -> BatchDecision:
        findings: list[Finding] = []
        card = self.procedures.effective_for_step(batch.step_id, batch.end_at)

        self._check_review_versions(facts, card, findings)
        self._check_readings(batch, facts, card, findings)
        self._check_first_anomaly(facts, findings)
        self._check_recommendations(facts, findings)
        self._check_review_presence(facts, card, findings)

        open_ri = [ri.ri_id for ri in reinspections if ri.batch_id == batch.batch_id
                   and ri.status.value == "open"]
        closed_ri = [ri.ri_id for ri in reinspections if ri.batch_id == batch.batch_id
                     and ri.status.value == "closed"]
        if open_ri:
            findings.append(
                Finding(
                    rule=RuleCode.R06_OPEN_REINSPECTION,
                    blocking=True,
                    message=f"批次 {batch.batch_id} 存在 {len(open_ri)} 项未关闭复检："
                            f"{', '.join(open_ri)}",
                )
            )

        self._apply_resolution(facts, card, findings, reinspections, batch.batch_id)
        blocking = [f for f in findings if f.blocking]
        has_evidence = any(
            f.category in (FactCategory.OBSERVATION, FactCategory.EXECUTION, FactCategory.REVIEW)
            for f in facts
        )
        if not blocking and not open_ri:
            disposition = Disposition.RELEASE
        elif not has_evidence:
            disposition = Disposition.INSUFFICIENT
        else:
            disposition = Disposition.HOLD

        return BatchDecision(
            batch_id=batch.batch_id,
            disposition=disposition,
            applicable_card_id=card.card_id if card else None,
            applicable_version=card.version if card else None,
            findings=findings,
            open_ri_ids=open_ri,
            closed_ri_ids=closed_ri,
            recomputed_at=now,
        )

    # ------------------------------------------------------------------
    def _check_review_versions(
        self,
        facts: list[Fact],
        effective_card,
        findings: list[Finding],
    ) -> None:
        for fact in facts:
            if fact.category is not FactCategory.REVIEW or not fact.referenced_card_id:
                continue
            referenced = (fact.referenced_card_id, str(fact.referenced_version or ""))
            if effective_card is None:
                findings.append(Finding(
                    rule=RuleCode.R01_REVIEW_WRONG_VERSION,
                    blocking=True,
                    message=f"复核 {fact.fact_id} 引用 {referenced[0]}，但该工序没有生效版本",
                    evidence_fact_ids=(fact.fact_id,),
                ))
                continue
            if (
                fact.referenced_card_id != effective_card.card_id
                or (
                    fact.referenced_version is not None
                    and str(fact.referenced_version) != str(effective_card.version)
                )
            ):
                findings.append(Finding(
                    rule=RuleCode.R01_REVIEW_WRONG_VERSION,
                    blocking=True,
                    message=(
                        f"复核 {fact.fact_id} 引用 {fact.referenced_card_id} "
                        f"v{fact.referenced_version}，批次当时有效版本为 "
                        f"{effective_card.card_id} v{effective_card.version}，"
                        "换版不溯及既往，该复核不能作为放行依据"
                    ),
                    evidence_fact_ids=(fact.fact_id,),
                ))

    def _check_readings(self, batch, facts, effective_card, findings) -> None:
        reading_predicates = {"reading", "qualitative-reading"}
        for fact in facts:
            if fact.predicate not in reading_predicates:
                continue
            if fact.category not in (FactCategory.OBSERVATION, FactCategory.EXECUTION):
                continue  # 建议与复核里的数字不是现场读数
            if not fact.equipment_id:
                # 无法归并到具体设备的读数不臆测归属，不能作为合格性证据
                continue
            relation = self.timeline.relation_for_fact(fact)
            if relation is CalibrationRelation.BEFORE:
                findings.append(Finding(
                    rule=RuleCode.R02_READING_PRECALIBRATION,
                    blocking=True,
                    message=(
                        f"读数 {fact.fact_id}（{fact.value if fact.value is not None else fact.text}）"
                        "发生在有效校准之前；批次结束后的校准不能倒推为当时合格，需校准后复检"
                    ),
                    evidence_fact_ids=(fact.fact_id,),
                ))
            elif relation is CalibrationRelation.UNKNOWN:
                findings.append(Finding(
                    rule=RuleCode.R03_READING_TIMING_UNKNOWN,
                    blocking=True,
                    message=(
                        f"叙述 {fact.fact_id} 无法确定读数在本次校准之前还是之后，"
                        "不得按‘后来正常’放行，需补做读数"
                    ),
                    evidence_fact_ids=(fact.fact_id,),
                ))
                continue
            else:
                # 定性读数无值可比较，只在校准关系明确时接受其“范围内”叙述
                if fact.predicate == "qualitative-reading":
                    continue
                self._check_limit(fact, effective_card, findings)

    def _check_limit(self, fact: Fact, effective_card, findings) -> None:
        if effective_card is None:
            return
        param = _UNIT_PARAM.get((fact.unit or "").strip())
        if not param or param not in effective_card.limits:
            return
        limit = effective_card.limits[param]
        if fact.value > limit:
            findings.append(Finding(
                rule=RuleCode.R05_LIMIT_EXCEEDED,
                blocking=True,
                message=(
                    f"读数 {fact.fact_id} {fact.value}{fact.unit} 超出有效版本 "
                    f"{effective_card.card_id} v{effective_card.version} 限值 {limit}"
                ),
                evidence_fact_ids=(fact.fact_id,),
            ))

    def _check_first_anomaly(self, facts: list[Fact], findings: list[Finding]) -> None:
        for fact in facts:
            if "首次异常" in fact.text and ("未注明" in fact.text or "缺失" in fact.text):
                findings.append(Finding(
                    rule=RuleCode.R04_FIRST_ANOMALY_MISSING,
                    blocking=True,
                    message=f"{fact.fact_id} 表明首次异常读数未被记录，偏差经过不完整，需补齐",
                    evidence_fact_ids=(fact.fact_id,),
                ))

    def _check_recommendations(self, facts: list[Fact], findings: list[Finding]) -> None:
        for fact in facts:
            if fact.category is FactCategory.RECOMMENDATION:
                findings.append(Finding(
                    rule=RuleCode.W01_RECOMMENDATION_NOT_EVIDENCE,
                    blocking=False,
                    message=(
                        f"{fact.fact_id} 是操作建议（{fact.text}），"
                        "建议不等于已执行，不能作为放行或关闭复检的证据"
                    ),
                    evidence_fact_ids=(fact.fact_id,),
                ))

    def _check_review_presence(self, facts, effective_card, findings) -> None:
        valid_review = any(
            f.category is FactCategory.REVIEW
            and effective_card is not None
            and f.referenced_card_id == effective_card.card_id
            and (
                f.referenced_version is None
                or str(f.referenced_version) == str(effective_card.version)
            )
            for f in facts
        )
        if not valid_review:
            findings.append(Finding(
                rule=RuleCode.R07_REVIEW_MISSING,
                blocking=True,
                message="缺少引用批次当时有效工艺版本的质量复核结论",
            ))

    # ------------------------------------------------------------------
    def _apply_resolution(self, facts, effective_card, findings, reinspections, batch_id):
        """纠正措施完成后解除阻断，但保留可审计的非阻断记录。

        * 某规则对应的复检事项已关闭 → 该规则的阻断降级为信息项；
        * 已存在引用有效版本的复核 → 旧版本引用（R01）降级为信息项；
        降级后的发现仍然保留在判定中，证据链不消失。
        """

        closed_rules = {
            ri.reason_rule for ri in reinspections
            if ri.batch_id == batch_id and ri.status.value == "closed"
        }
        has_valid_review = any(
            f.category is FactCategory.REVIEW
            and effective_card is not None
            and f.referenced_card_id == effective_card.card_id
            and (f.referenced_version is None
                 or str(f.referenced_version) == str(effective_card.version))
            for f in facts
        )
        for finding in findings:
            if finding.rule in closed_rules:
                finding.blocking = False
                finding.message += "（对应复检已关闭，阻断解除）"
            elif finding.rule is RuleCode.R01_REVIEW_WRONG_VERSION and has_valid_review:
                finding.blocking = False
                finding.message += "（已补引用有效版本的复核，原问题保留为信息项）"

    @staticmethod
    def rules_requiring_reinspection() -> set[RuleCode]:
        return set(_REINSPECTION_RULES)
