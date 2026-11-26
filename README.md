# 洁净制造偏差记录的工序适用性判定（持续处理流水线）

本项目把洁净制造现场分开补写的三类记录——操作员叙述、设备日志、质量复核——
与受控工艺卡版本一起，作为**带溯源的事实流**持续处理，帮助质量人员回答：

1. 当前批次能否继续流转；
2. 哪些检验需要重做、哪些复检仍未关闭；
3. 每个批次适用哪一版工艺卡，并把当前生效版本交给下一班；
4. 偏差纠正后，哪些批次受影响、还有什么事项未关闭。

> 演练中的产线、读数、人员与工艺卡均为合成内容，不对应真实人员、机构或业务决定。

## 判定纪律（不可绕过）

- **建议 ≠ 执行 ≠ 复核**：操作建议（RECOMMENDATION）、实际执行
  （EXECUTION/OBSERVATION）、复核结论（REVIEW）拆成不同事实，建议不参与
  合格性判断，也不能用于关闭复检。
- **校准不倒推**：校准发生在本批结束之后时，批次期间的读数属于校准前读数
  （R02，阻断）；设备漂移会终止上一次校准的有效性，不允许用历史校准覆盖漂移期。
- **先后不明即不可放行**：叙述无法确定读数在校准之前还是之后时标记 R03，
  “后来读数正常”不能倒推为本批合格。
- **换版不溯及既往**：旧批次永远按其批次窗口内有效的工艺版本解释
  （R01：复核引用非当时有效版本时阻断；补齐有效版本复核后降级为信息项）。
- **复检双证据关闭**：复检事项必须同时具备“已执行的复检”与“引用有效版本的
  复核结论”才能关闭，关闭后对应阻断解除但证据链保留。
- **无证据不放行**：没有任何可判断证据时给 `insufficient`，而不是默认合格。

## 模块结构

| 模块 | 职责 |
|---|---|
| `qapipeline/models.py` | 领域模型、事实类别、判定规则编号、复检事项 |
| `qapipeline/aliases.py` | 设备/工序别名归并，交叉指称解析与歧义检测（歧义不猜测） |
| `qapipeline/extraction.py` | 自由文本的确定性事实抽取，建议/执行/复核分流，保留原文与子句号 |
| `qapipeline/timeline.py` | 校准有效窗口与读数先后判定（不依赖事件到达顺序） |
| `qapipeline/procedures.py` | 工艺卡版本注册表，按时刻选择有效版本 |
| `qapipeline/engine.py` | 规则引擎，输出绑定证据事实编号的阻断/提示发现 |
| `qapipeline/processor.py` | 持续事件处理器、复检生命周期、受影响批次追踪、判定重算 |
| `qapipeline/store.py` | 追加式 JSONL 事件存储（幂等、可重放） |
| `qapipeline/watcher.py` | 文件持续监视，偏移文件支持断点续处理 |
| `qapipeline/report.py` | 交班 Markdown 报告与结构化判定导出 |
| `qapipeline/cli.py` | 命令行入口（run / watch / report） |

## 数据

- `data/case.json`：最初的合成素材（自由文本 + 工艺卡编号），保持原样；
  `tests/test_legacy_case.py` 演示如何把它适配为事件流并适用同一套判定纪律。
- `data/demo/events.jsonl`：完整合成事件流（26 个事件，4 个批次），覆盖
  正常放行、批次后校准、读数先后不明、纠正后复检关闭、换版解释等场景。

事件类型：`equipment-assignment`、`procedure-published`、`batch-registered`、
`record-ingested`（内含原始记录）、`equipment-event`、
`reinspection-registered`、`reinspection-updated`。所有事件带唯一 `event_id`，
处理是幂等的，乱序/迟到事实到达后会自动重算受影响批次。

## 使用

运行环境为 Python 3.11+，无第三方运行依赖。按隔离要求，入口脚本位于独立的
`scripts/` 目录，数据路径以参数传入，并以 `-I` 模式运行（忽略环境变量与
用户站点包，防止数据目录中的 planted module 被导入）：

```sh
# 一次性处理事件文件，输出各批次判定 JSON
python3 -I scripts/run_pipeline.py run \
  --events data/demo/events.jsonl --store out/event-store.jsonl --replay

# 持续监视一个或多个追加日志（断点记录在 offset 文件）
python3 -I scripts/run_pipeline.py watch \
  --files out/watch-demo/feed.jsonl \
  --store out/event-store.jsonl --offset out/offsets.json

# 生成交班报告（Markdown）与结构化判定（JSON）
python3 -I scripts/run_pipeline.py report \
  --events data/demo/events.jsonl --out out/handover.md
```

## 测试

```sh
python3 -m unittest discover -s tests -v
```

测试包含：原始素材稳定性、别名歧义、事实三分流、校准窗口（含漂移失效与
迟到事件）、版本选择、复检双证据关闭、事件存储幂等、监视断点恢复，以及
覆盖完整证据链的端到端行为测试。
