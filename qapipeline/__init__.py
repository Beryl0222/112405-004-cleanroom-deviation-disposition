"""洁净制造偏差持续处理流水线（合成演练）。

模块组成：

* :mod:`qapipeline.models`     领域模型与枚举
* :mod:`qapipeline.aliases`    设备 / 工序别名归并（含歧义检测）
* :mod:`qapipeline.extraction` 偏差叙述的确定性事实抽取（建议/执行/复核分流）
* :mod:`qapipeline.timeline`   设备校准时间线与读数先后判定
* :mod:`qapipeline.procedures` 受控工艺卡版本管理（换版不溯及既往）
* :mod:`qapipeline.engine`     批次判定规则引擎
* :mod:`qapipeline.processor`  持续事件处理器与复检事项生命周期
* :mod:`qapipeline.store`      追加式事件存储
* :mod:`qapipeline.watcher`    文件持续监视
* :mod:`qapipeline.report`     交班报告
"""

from .processor import Processor
from .store import EventStore
from .watcher import TailRunner

__all__ = ["Processor", "EventStore", "TailRunner"]
