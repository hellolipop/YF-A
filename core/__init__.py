# -*- coding: utf-8 -*-
"""stock-terminal 核心计算包。

对外主入口是 metrics 中的 :func:`summarize` —— 纯标准库实现（无第三方依赖）的
绩效指标库，供回测引擎（engine）、接口层（server）、存储层（storage）与离线脚本复用。
"""

from .metrics import MODES, summarize  # noqa: F401

__all__ = ["summarize", "MODES"]
