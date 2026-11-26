"""命令行入口。

用法：

    python -m qapipeline.cli run   --events data/demo/events.jsonl [--replay]
    python -m qapipeline.cli watch --files a.jsonl b.jsonl --store out/events.jsonl
    python -m qapipeline.cli report --events data/demo/events.jsonl --out out/report.md

``run`` 默认只处理新事件；加 ``--replay`` 时清空存储后全量重放。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .processor import Processor
from .report import export_decisions, render_handover
from .store import EventStore
from .watcher import TailRunner

CN_TZ = timezone(timedelta(hours=8))


def _load_jsonl(path: Path) -> list[dict]:
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def _build_processor(events: list[dict]) -> Processor:
    processor = Processor()
    processor.replay(events)
    return processor


def cmd_run(args: argparse.Namespace) -> int:
    events_path = Path(args.events)
    events = _load_jsonl(events_path)
    store = EventStore(args.store)
    if args.replay and store.path.exists():
        store.path.unlink()
        store = EventStore(args.store)
    processor = Processor()
    for event in events:
        if store.append(event):
            processor.handle(event)
    now = datetime.now(tz=CN_TZ)
    decisions = export_decisions(processor, now)
    print(json.dumps(decisions, ensure_ascii=False, indent=2))
    print(f"\n已处理事件 {len(store)} 条；未关闭复检 "
          f"{len(processor.open_reinspections())} 项", file=sys.stderr)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    store = EventStore(args.store)
    processor = Processor()

    def _print(decisions: list) -> None:
        for decision in decisions:
            print(f"[重算] {decision.batch_id} -> {decision.disposition.value} "
                  f"（未关闭复检 {len(decision.open_ri_ids)} 项）")

    runner = TailRunner(
        processor=processor,
        store=store,
        watched_files=[Path(p) for p in args.files],
        offset_path=args.offset,
        poll_interval=args.interval,
    )
    print(f"开始监视 {len(args.files)} 个文件，Ctrl+C 退出 …", file=sys.stderr)
    runner.watch(on_decisions=_print)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    events = _load_jsonl(Path(args.events))
    processor = _build_processor(events)
    now = datetime.now(tz=CN_TZ)
    for batch_id in sorted(processor.batches):
        processor.recompute(batch_id, now)
    equipment_id = args.equipment
    markdown = render_handover(processor, equipment_id, now)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(markdown, encoding="utf-8")
    decisions_path = Path(args.out).with_suffix(".json")
    decisions_path.write_text(
        json.dumps(export_decisions(processor, now), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"报告已写入 {args.out}；结构化判定写入 {decisions_path}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="洁净制造偏差持续处理流水线（合成演练）")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="一次性处理事件文件并输出判定")
    run.add_argument("--events", required=True)
    run.add_argument("--store", default="out/event-store.jsonl")
    run.add_argument("--replay", action="store_true", help="清空存储后全量重放")
    run.set_defaults(func=cmd_run)

    watch = sub.add_parser("watch", help="持续监视 JSONL 追加日志")
    watch.add_argument("--files", nargs="+", required=True)
    watch.add_argument("--store", default="out/event-store.jsonl")
    watch.add_argument("--offset", default="out/offsets.json")
    watch.add_argument("--interval", type=float, default=1.0)
    watch.set_defaults(func=cmd_watch)

    report = sub.add_parser("report", help="生成交班报告与结构化判定")
    report.add_argument("--events", required=True)
    report.add_argument("--out", default="out/handover.md")
    report.add_argument("--equipment", default="EQ-EC-02")
    report.set_defaults(func=cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
