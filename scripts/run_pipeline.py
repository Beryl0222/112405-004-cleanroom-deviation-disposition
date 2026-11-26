#!/usr/bin/env python3 -I
"""隔离模式运行入口（-I：忽略环境变量与用户站点包，防止 planted module）。

把项目根加入 sys.path 后转交 qapipeline.cli，数据路径全部以参数传入，
不在数据目录内启动解释器。
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from qapipeline.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
