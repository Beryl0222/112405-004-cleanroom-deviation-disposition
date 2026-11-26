"""设备与工序别名归并。

现场叙述里同一台设备 / 同一道工序会以多种别名出现，而且一段文字中
设备别名与工序名会交叉。本注册表把每个出现的指称（mention）解析到
唯一的标准实体；当一个指称同时匹配多个实体时不做猜测，而是返回
歧义结果，由判定层记为待人工确认。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class EntityType(str, Enum):
    EQUIPMENT = "equipment"
    STEP = "step"


@dataclass(frozen=True)
class ResolvedMention:
    mention: str
    entity_type: EntityType
    entity_id: str | None          # 歧义且无法唯一解析时为 None
    ambiguous: bool
    candidates: tuple[str, ...]

    @property
    def resolved(self) -> bool:
        return self.entity_id is not None and not self.ambiguous


class AliasRegistry:
    """标准实体 -> 别名集合 的双向解析表。"""

    def __init__(self) -> None:
        # entity_type -> entity_id -> set(aliases)，别名统一小写匹配
        self._aliases: dict[EntityType, dict[str, set[str]]] = {
            EntityType.EQUIPMENT: {},
            EntityType.STEP: {},
        }

    def register(self, entity_type: EntityType, entity_id: str, *aliases: str) -> None:
        bucket = self._aliases[entity_type].setdefault(entity_id, {entity_id})
        for alias in aliases:
            bucket.add(alias.strip())

    # ------------------------------------------------------------------
    def _all_aliases(self, entity_type: EntityType) -> dict[str, str]:
        """alias(lower) -> entity_id；构造时检测跨实体重名。"""
        mapping: dict[str, str] = {}
        for entity_id, names in self._aliases[entity_type].items():
            for name in names:
                key = name.lower()
                if key in mapping and mapping[key] != entity_id:
                    # 同一别名登记给两个实体：保留首个，但解析时按歧义处理
                    mapping[key] = "__conflict__"
                else:
                    mapping[key] = entity_id
        return mapping

    def resolve(self, entity_type: EntityType, mention: str) -> ResolvedMention:
        mention = mention.strip()
        mapping = self._all_aliases(entity_type)
        # 精确匹配
        hit = mapping.get(mention.lower())
        if hit and hit != "__conflict__":
            return ResolvedMention(mention, entity_type, hit, False, (hit,))
        if hit == "__conflict__":
            candidates = tuple(
                eid for eid, names in self._aliases[entity_type].items()
                if mention.lower() in {n.lower() for n in names}
            )
            return ResolvedMention(mention, entity_type, None, True, candidates)
        # 包含匹配（叙述中常见“2号电导探头读数”这类带修饰的指称）
        candidates = tuple(
            eid for alias, eid in mapping.items()
            if eid != "__conflict__" and alias and alias in mention.lower()
        )
        unique = tuple(dict.fromkeys(candidates))
        if len(unique) == 1:
            return ResolvedMention(mention, entity_type, unique[0], False, unique)
        if len(unique) > 1:
            return ResolvedMention(mention, entity_type, None, True, unique)
        return ResolvedMention(mention, entity_type, None, False, ())

    def resolve_text(self, text: str) -> list[ResolvedMention]:
        """在一段文本中同时解析设备指称与工序指称（交叉出现场景）。"""
        results: list[ResolvedMention] = []
        for entity_type in (EntityType.EQUIPMENT, EntityType.STEP):
            for entity_id, names in self._aliases[entity_type].items():
                for name in names:
                    if name and name.lower() in text.lower():
                        results.append(self.resolve(entity_type, name))
        # 去重，保持稳定顺序
        seen: set[tuple[EntityType, str]] = set()
        deduped: list[ResolvedMention] = []
        for item in results:
            key = (item.entity_type, item.mention)
            if key not in seen:
                seen.add(key)
                deduped.append(item)
        return deduped


def build_default_registry() -> AliasRegistry:
    """演练资料使用的标准别名表（内容均为合成）。"""

    registry = AliasRegistry()
    registry.register(
        EntityType.EQUIPMENT,
        "EQ-EC-02",
        "2号电导探头", "2号探头", "电导探头", "在线电导仪",
        "在线电导仪报警", "探头",
    )
    registry.register(
        EntityType.STEP,
        "STEP-RINSE",
        "漂洗", "漂洗工序", "末道漂洗",
    )
    registry.register(
        EntityType.STEP,
        "STEP-FILL",
        "灌装", "灌装工序",
    )
    return registry
