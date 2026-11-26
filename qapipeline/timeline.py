"""设备时间线：读数相对校准窗口的定位。

核心纪律（对应演练要求）：

* 校准发生在本批结束之后时，批次窗口内的读数属于“校准前读数”，
  不能作为放行依据；
* 叙述没有给出足够时间信息、无法确定读数在校准之前还是之后时，
  关系标记为 :data:`~qapipeline.models.CalibrationRelation.UNKNOWN`，
  “后来读数正常”不得倒推为本批合格；
* 只有明确落在某次有效校准之后的读数，才可作为合格性判断的输入。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import Batch, CalibrationRelation, EquipmentEvent, Fact

CN_TZ = timezone(timedelta(hours=8))


def _as_cn(moment: datetime) -> datetime:
    """统一到东八区的可比较时间（naive 视为东八区本地时间）。"""

    if moment.tzinfo is None:
        return moment.replace(tzinfo=CN_TZ)
    return moment.astimezone(CN_TZ)


class EquipmentTimeline:
    def __init__(self) -> None:
        self._events: dict[str, list[EquipmentEvent]] = {}

    def add_event(self, event: EquipmentEvent) -> None:
        bucket = self._events.setdefault(event.equipment_id, [])
        if event not in bucket:
            bucket.append(event)
            bucket.sort(key=lambda item: item.occurred_at)

    def calibrations(self, equipment_id: str) -> list[EquipmentEvent]:
        return [
            event
            for event in self._events.get(equipment_id, [])
            if event.event_type == "calibration"
        ]

    def events(self, equipment_id: str) -> list[EquipmentEvent]:
        return list(self._events.get(equipment_id, []))

    def _valid_window(self, equipment_id: str, index: int,
                      calibrations: list[EquipmentEvent]) -> tuple[datetime, datetime | None]:
        """某次校准的有效区间：到显式 valid_to 或下一次校准为止。"""

        event = calibrations[index]
        start = _as_cn(event.occurred_at)
        if event.valid_to is not None:
            return start, _as_cn(event.valid_to)
        if index + 1 < len(calibrations):
            return start, _as_cn(calibrations[index + 1].occurred_at)
        return start, None

    def calibration_covering(self, equipment_id: str, moment: datetime) -> EquipmentEvent | None:
        """覆盖 ``moment`` 的有效校准；设备漂移 / 重新校准后旧校准失效。"""

        moment = _as_cn(moment)
        calibrations = self.calibrations(equipment_id)
        for index, event in enumerate(calibrations):
            start, end = self._valid_window(equipment_id, index, calibrations)
            # 半开区间 [start, end)：end 时刻起属于下一次校准窗口
            if start <= moment and (end is None or moment < end):
                return event
        return None

    def relation(self, equipment_id: str, moment: datetime | None) -> CalibrationRelation:
        """读数时间与校准窗口的关系。

        没有时间戳即无法判定先后，返回 UNKNOWN，调用方不得按合格处理。
        关系只取决于设备的完整校准记录，不依赖事件到达顺序（迟到的
        校准事件到达后，早到的读数关系会在重算时自动纠正）。
        """

        if moment is None:
            return CalibrationRelation.UNKNOWN
        covering = self.calibration_covering(equipment_id, moment)
        if covering is None:
            # 读数时刻不存在覆盖它的有效校准：校准前 / 校准已失效
            return CalibrationRelation.BEFORE
        return CalibrationRelation.AFTER

    def relation_for_fact(self, fact: Fact) -> CalibrationRelation:
        if not fact.equipment_id:
            return CalibrationRelation.UNKNOWN
        return self.relation(fact.equipment_id, fact.occurred_at)

    def calibration_after_batch(self, batch: Batch, equipment_id: str) -> EquipmentEvent | None:
        """批次结束后才完成的那次校准（倒推风险的来源）。"""

        later = [
            event
            for event in self.calibrations(equipment_id)
            if event.occurred_at > _as_cn(batch.end_at)
        ]
        return later[0] if later else None
