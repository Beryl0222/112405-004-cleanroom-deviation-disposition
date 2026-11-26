"""持续文件监视。

监视一个或多个 JSONL 追加日志：定期读取新增行，交给
:class:`~qapipeline.processor.Processor` 处理并写入事件存储，
读取位置记录在 sidecar 偏移文件中，进程重启后从断点继续，不重复处理。
不依赖 inotify / 第三方库，采用轮询方式，适合本地联调与离线演练。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from .store import EventStore


class TailRunner:
    def __init__(
        self,
        processor,
        store: EventStore,
        watched_files: list[str | Path],
        offset_path: str | Path,
        poll_interval: float = 1.0,
    ) -> None:
        self.processor = processor
        self.store = store
        self.watched_files = [Path(p) for p in watched_files]
        self.offset_path = Path(offset_path)
        self.poll_interval = poll_interval
        self._offsets = self._load_offsets()

    def _load_offsets(self) -> dict[str, int]:
        if self.offset_path.exists():
            return json.loads(self.offset_path.read_text(encoding="utf-8"))
        return {}

    def _save_offsets(self) -> None:
        self.offset_path.parent.mkdir(parents=True, exist_ok=True)
        self.offset_path.write_text(
            json.dumps(self._offsets, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def drain_once(self, on_decisions: Callable[[list], None] | None = None) -> int:
        """处理所有被监视文件当前的新增内容，返回处理的事件数。"""

        processed = 0
        for path in self.watched_files:
            key = str(path)
            if not path.exists():
                continue
            # 文件被截断 / 替换（演练中重新生成数据）时从头读取
            size = path.stat().st_size
            offset = self._offsets.get(key, 0)
            if offset > size:
                offset = 0
            with path.open("r", encoding="utf-8") as handle:
                handle.seek(offset)
                for line in handle:
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if self.store.append(event):
                        decisions = self.processor.handle(event)
                        processed += 1
                        if on_decisions and decisions:
                            on_decisions(decisions)
                self._offsets[key] = handle.tell()
        self._save_offsets()
        return processed

    def watch(self, max_cycles: int | None = None,
              on_decisions: Callable[[list], None] | None = None) -> None:
        """持续轮询；``max_cycles`` 用于测试，生产传 None 永久运行。"""

        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            self.drain_once(on_decisions=on_decisions)
            cycles += 1
            if max_cycles is None or cycles < max_cycles:
                time.sleep(self.poll_interval)
