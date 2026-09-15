#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AlphaDesk · A股数据提供层（providers）

本包只做「抓取 + 解析 + 统一结构」，不含业务逻辑、不依赖 server.py / engine.py，
可被服务端、脚本、定时任务直接复用。零第三方依赖（仅标准库）。

当前内容：
  · features —— A股四类「新数据」：集合竞价快照 / 分笔成交 / 龙虎榜 / 涨停梯队

设计约定：
  1. 四个函数返回**同一外层信封**：{ok, data, source, fetchedAt, dataTime, error, degraded, stale}；
  2. 失败时不抛异常，data 返回「空实现」形状（键名固定），便于前端直接渲染降级态；
  3. 每条数据都标注 source（数据源）与时间（fetchedAt 取数时间 / dataTime 数据时间）；
  4. HTTP 统一绕过本机代理并容忍证书问题（详见 features 模块头部的实测说明）。

用法::

    from providers import features

    r = features.auction("600519")
    if r["ok"]:
        print(r["source"], r["data"]["price"], r["data"]["volume"])

    r = features.limit_up_ladder()          # date 传 None = 最近一个有数据的交易日
    for lv in r["data"]["ladders"]:
        print(lv["level"], "连板", lv["count"], "家")
"""

from . import features  # noqa: F401
from .features import auction, dragon_tiger, limit_up_ladder, ticks  # noqa: F401

__all__ = ["features", "auction", "ticks", "dragon_tiger", "limit_up_ladder"]
