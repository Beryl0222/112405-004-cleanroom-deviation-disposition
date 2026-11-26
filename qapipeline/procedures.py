"""受控工艺卡的版本选择。

规则：工艺卡换版时，旧批次仍按其批次窗口内有效（in-force）的版本
解释，新版本不溯及既往。复核结论引用的版本若不是当时有效版本，
该复核结论不能作为放行依据（见引擎 R01）。
"""

from __future__ import annotations

from datetime import datetime

from .models import ProcessCard


class ProcedureRegistry:
    def __init__(self) -> None:
        self._cards: dict[str, list[ProcessCard]] = {}

    def add_card(self, card: ProcessCard) -> None:
        bucket = self._cards.setdefault(card.card_id, [])
        bucket.append(card)
        bucket.sort(key=lambda item: item.effective_from)

    def all_cards(self) -> list[ProcessCard]:
        return [card for versions in self._cards.values() for card in versions]

    def versions(self, card_id: str) -> list[ProcessCard]:
        return list(self._cards.get(card_id, ()))

    def effective_version(self, card_id: str, moment: datetime) -> ProcessCard | None:
        """``moment`` 时刻有效的版本；无版本覆盖时返回 None。"""

        for card in self.versions(card_id):
            if card.in_force_at(moment):
                return card
        return None

    def for_step(self, step_id: str) -> list[ProcessCard]:
        return [card for card in self.all_cards() if card.step_id == step_id]

    def effective_for_step(self, step_id: str, moment: datetime) -> ProcessCard | None:
        for card in self.for_step(step_id):
            if card.in_force_at(moment):
                return card
        return None

    def is_current(self, card_id: str, version: str, moment: datetime) -> bool:
        card = self.effective_version(card_id, moment)
        return card is not None and card.version == version
