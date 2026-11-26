"""追加式事件存储。

持续处理的持久化采用只追加的 JSONL 事件日志：每个事件带唯一
``event_id``，重启后重放即可恢复全部状态，天然保留审计轨迹。
不引入第三方依赖。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator


class EventStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seen: set[str] = set()
        if self.path.exists():
            for event in self.read_all():
                self._seen.add(event["event_id"])

    def append(self, event: dict[str, Any]) -> bool:
        """追加事件；``event_id`` 重复时忽略并返回 False（幂等）。"""

        event_id = event["event_id"]
        if event_id in self._seen:
            return False
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._seen.add(event_id)
        return True

    def read_all(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"事件日志第 {line_no} 行不是合法 JSON: {exc}") from exc

    def seen_ids(self) -> set[str]:
        return set(self._seen)

    def __len__(self) -> int:
        return len(self._seen)
