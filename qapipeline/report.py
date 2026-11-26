"""面向质量人员 / 下一班的交班报告。

报告把流水线状态渲染成 Markdown，覆盖演练要求的四类输出：

1. 当前批次能否继续流转（判定与阻断规则）；
2. 哪些检验需要重做、哪些复检仍未关闭；
3. 适用的工艺版本（按批次当时有效版本解释，并给出当前生效版本供下一班使用）；
4. 纠正偏差后受影响批次与未关闭事项的追踪；
5. 证据附录：每条结论绑定来源记录与事实编号，建议 / 执行 / 复核分列。
"""

from __future__ import annotations

from datetime import datetime

from .models import Disposition, FactCategory
from .processor import Processor

_DISPOSITION_LABEL = {
    Disposition.RELEASE: "可放行",
    Disposition.HOLD: "暂缓流转",
    Disposition.INSUFFICIENT: "证据不足",
}
_CATEGORY_LABEL = {
    FactCategory.OBSERVATION: "现场观察",
    FactCategory.EXECUTION: "实际执行",
    FactCategory.RECOMMENDATION: "操作建议",
    FactCategory.REVIEW: "复核结论",
    FactCategory.REFERENCE: "文件引用",
}


def render_handover(processor: Processor, equipment_id: str, now: datetime) -> str:
    lines: list[str] = []
    lines.append("# 偏差处置交班报告（合成演练资料）")
    lines.append("")
    lines.append(f"- 生成时间：{now.isoformat(timespec='minutes')}")
    lines.append(f"- 关注设备：{equipment_id}")
    lines.append("- 本报告所有产线、读数与人员均为合成内容，不构成真实业务决定。")
    lines.append("")

    lines.append("## 1. 当前生效工艺版本（交下一班）")
    lines.append("")
    lines.append("| 工艺卡 | 版本 | 工序 | 生效时间 | 关键限值 |")
    lines.append("|---|---|---|---|---|")
    for card in processor.procedures.all_cards():
        limits = "；".join(f"{k} ≤ {v}" for k, v in card.limits.items()) or "—"
        end = card.effective_to.isoformat(timespec="minutes") if card.effective_to else "至今"
        lines.append(
            f"| {card.card_id} | v{card.version} | {card.step_id} | "
            f"{card.effective_from.date()} 起，{end} | {limits} |"
        )
    lines.append("")
    lines.append("> 旧批次仍按其批次窗口内有效版本解释，换版不溯及既往。")
    lines.append("")

    lines.append("## 2. 批次流转判定")
    lines.append("")
    lines.append("| 批次 | 工序 | 判定 | 适用版本 | 阻断规则 | 未关闭复检 |")
    lines.append("|---|---|---|---|---|---|")
    for batch_id in sorted(processor.batches):
        decision = processor.decisions.get(batch_id)
        batch = processor.batches[batch_id]
        if decision is None:
            decision = processor.recompute(batch_id, now)
        blocking = [f.rule.value for f in decision.findings if f.blocking]
        lines.append(
            f"| {batch_id} | {batch.step_id} | "
            f"{_DISPOSITION_LABEL[decision.disposition]} | "
            f"{decision.applicable_card_id} v{decision.applicable_version} | "
            f"{'；'.join(blocking) or '—'} | "
            f"{'；'.join(decision.open_ri_ids) or '—'} |"
        )
    lines.append("")

    lines.append("## 3. 复检事项")
    lines.append("")
    open_ri = processor.open_reinspections()
    if not open_ri:
        lines.append("当前没有未关闭的复检事项。")
    else:
        lines.append("| 复检编号 | 批次 | 触发规则 | 内容 | 状态 | 依据事实 |")
        lines.append("|---|---|---|---|---|---|")
        for ri in open_ri:
            lines.append(
                f"| {ri.ri_id} | {ri.batch_id} | {ri.reason_rule.value} | "
                f"{ri.description} | 未关闭 | {', '.join(ri.opened_from_fact_ids) or '—'} |"
            )
    lines.append("")
    closed = [ri for ri in processor.reinspections.values() if ri.status.value == "closed"]
    if closed:
        lines.append("已关闭复检：")
        for ri in closed:
            lines.append(
                f"- {ri.ri_id}（{ri.batch_id}，{ri.reason_rule.value}）"
                f"，关闭依据：{', '.join(ri.closed_by_fact_ids)}"
            )
        lines.append("")

    lines.append(f"## 4. 设备 {equipment_id} 受影响批次追踪")
    lines.append("")
    affected = processor.affected_batches(equipment_id)
    if affected:
        lines.append("| 批次 | 当前判定 | 阻断规则 | 未关闭复检 |")
        lines.append("|---|---|---|---|")
        for item in affected:
            lines.append(
                f"| {item['batch_id']} | {item['disposition'] or '未判定'} | "
                f"{'；'.join(item['blocking_rules']) or '—'} | "
                f"{'；'.join(item['open_ri_ids']) or '—'} |"
            )
    else:
        lines.append("该设备当前没有关联批次。")
    lines.append("")

    lines.append("## 5. 证据附录（建议 / 执行 / 复核分列）")
    lines.append("")
    for batch_id in sorted(processor.batches):
        lines.append(f"### 批次 {batch_id}")
        lines.append("")
        grouped: dict[FactCategory, list] = {}
        for fact in processor.facts.get(batch_id, []):
            grouped.setdefault(fact.category, []).append(fact)
        if not grouped:
            lines.append("- （暂无来源事实）")
            lines.append("")
            continue
        for category in (
            FactCategory.OBSERVATION,
            FactCategory.EXECUTION,
            FactCategory.RECOMMENDATION,
            FactCategory.REVIEW,
            FactCategory.REFERENCE,
        ):
            for fact in grouped.get(category, []):
                when = fact.occurred_at.isoformat(timespec="minutes") if fact.occurred_at else "时间未注明"
                lines.append(
                    f"- [{_CATEGORY_LABEL[category]}] {fact.fact_id} "
                    f"（来源 {fact.record_id}，{when}）：{fact.text}"
                )
        lines.append("")

    lines.append("### 判定纪律提示")
    lines.append("")
    lines.append("- 操作建议不等于已执行，不能作为放行或关闭复检的证据。")
    lines.append("- 批次结束后的校准不能倒推为当时合格；先后不明的读数按不可放行处理。")
    lines.append("- 复核引用非当时有效版本时结论无效，须按有效版本重新复核。")
    lines.append("")
    return "\n".join(lines)


def export_decisions(processor: Processor, now: datetime) -> list[dict]:
    """结构化导出全部批次判定，供下游系统消费。"""

    return [
        processor.recompute(batch_id, now).to_dict()
        for batch_id in sorted(processor.batches)
    ]
