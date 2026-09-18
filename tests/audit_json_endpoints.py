"""全接口严格 JSON 审计（需要服务已在 127.0.0.1:8848 运行）：

用法：python3 server.py & 然后 python3 tests/audit_json_endpoints.py

按浏览器 JSON.parse 的严格性校验每个接口的响应体。

Python 的 json.loads 默认接受 Infinity / NaN，所以历史上这类"服务端发的不是
合法 JSON"的问题能躲过 Python 侧测试，只在浏览器里炸出来。这里统一用
parse_constant 抛错的方式，把每个接口的响应体按浏览器的标准校验一遍。
"""

import json
import urllib.request

OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
import os

BASE = os.environ.get("AD_BASE", "http://127.0.0.1:8848")
bad = []
checked = []


def strict(text, label):
    def bad_const(v):
        raise ValueError("非有限数值字面量 %s" % v)
    try:
        json.loads(text, parse_constant=bad_const)
        checked.append(label)
    except Exception as exc:  # noqa: BLE001
        bad.append((label, str(exc)[:80], text[:120]))


def hit(path, body=None):
    url = BASE + path
    if body is None:
        req = urllib.request.Request(url)
        label = "GET " + path
    else:
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        label = "POST " + path
    try:
        with OP.open(req, timeout=300) as resp:
            strict(resp.read().decode("utf-8", "ignore"), label)
    except urllib.error.HTTPError as exc:
        strict(exc.read().decode("utf-8", "ignore"), label + " (HTTP %d)" % exc.code)


print("=== GET 接口 ===")
for p in ("/api/health", "/api/sysinfo", "/api/logs?limit=50",
          "/api/strategy/meta", "/api/strategy/overview",
          "/api/features", "/api/features/limit_up", "/api/features/dragon_tiger",
          "/api/features/auction?code=600519", "/api/features/ticks?code=000001&limit=10",
          "/api/notify", "/api/indices", "/api/overview?market=cn",
          "/api/movers?market=cn&type=gainers", "/api/sectors?market=cn",
          "/api/news?limit=10", "/api/search?q=600519",
          "/api/quote?market=cn&codes=600519", "/api/kline?market=cn&code=600519&period=day&limit=120",
          "/api/stock?market=cn&code=600519", "/api/orderbook?market=cn&code=600519",
          "/api/trends?market=cn&code=600519", "/api/fundflow?market=cn&code=600519",
          # 美股数据源（常规时段 / 币安 bStocks 7×24）：清单与币安源的四类出口
          "/api/us/source",
          "/api/stock?market=us&code=AAPL&source=binance",
          "/api/kline?market=us&code=AAPL&period=day&limit=120&source=binance",
          "/api/trends?market=us&code=AAPL&source=binance",
          "/api/orderbook?market=us&code=AAPL&source=binance",
          "/api/quote?market=us&codes=AAPL,NVDA&source=binance",
          "/api/list?market=cn&page=1&size=10"):
    hit(p)

print("=== 任务详情（逐个任务）===")
ov = json.loads(OP.open(BASE + "/api/strategy/overview", timeout=120).read())
for row in ov["rows"]:
    hit("/api/strategy/run?id=" + row["id"])

print("=== POST 接口 ===")
hit("/api/backtest", {"market": "cn", "code": "600519", "strategy": "maCross",
                      "params": {"fast": 5, "slow": 20}, "limit": 300, "initial": 1000000})
hit("/api/backtest", {"market": "cn", "code": "600519", "strategy": "maCross",
                      "params": {"fast": 5, "slow": 20}, "limit": 300, "initial": 1000000,
                      "fillModel": "depthWeighted"})
hit("/api/search/params", {"market": "cn", "code": "600519", "strategy": "maCross",
                           "limit": 300, "initial": 1000000, "metric": "sharpe",
                           "space": {"fast": {"enabled": True, "min": 3, "max": 9, "step": 3}}})
hit("/api/advisor/recommend", {"market": "cn", "codes": ["600519", "000001", "300750"],
                               "horizon": 20, "capital": 100000,
                               "kellyFraction": 0.5, "maxWeight": 0.25})
hit("/api/advisor/recommend", {"market": "cn", "codes": []})          # 缺标的：错误分支也要是严格 JSON

print("=== AI 选股记录（先落一条再读回来）===")
hit("/api/advisor/recommend", {"market": "cn", "symbols": [{"code": "600519", "market": "cn"}],
                               "capital": 1000000, "save": True, "trigger": "list",
                               "note": "browser-json-audit"})
for p in ("/api/advisor/history?limit=5",
          "/api/advisor/history?limit=5&market=cn&action=buy&q=%E8%8C%85%E5%8F%B0&pinned=1",
          "/api/advisor/record?id=__MISSING__", "/api/advisor/review?id=__MISSING__"):
    hit(p)
try:
    hist = json.loads(OP.open(BASE + "/api/advisor/history?limit=1", timeout=120).read())
    rid = (hist.get("rows") or [{}])[0].get("id")
    if rid:
        hit("/api/advisor/record?id=" + rid)
        hit("/api/advisor/review?id=" + rid)
        hit("/api/advisor/note", {"id": rid, "note": "audit", "pinned": True})
        hit("/api/advisor/note", {"id": rid, "pinned": False})
        hit("/api/advisor/delete", {"id": rid})
    hit("/api/advisor/prune", {"keep": 500})
except Exception as exc:  # noqa: BLE001
    print("  记录链路审计跳过：%s" % exc)

print("=== 标的名称识别 ===")
hit("/api/symbols/resolve?market=cn&tokens=600519,%E8%8C%85%E5%8F%B0,%E5%B9%B3%E5%AE%89")
hit("/api/symbols/resolve")                                   # 缺 tokens：错误分支
hit("/api/symbols/lookup?code=601398")
hit("/api/symbols/lookup")                                    # 缺 code/q：错误分支
hit("/api/symbols/resolve", {"market": "cn", "tokens": ["600519", "贵州茅台", "不存在公司"]})

hit("/api/notify", {"webhook": "", "events": ["on_fill", "on_exit", "on_skip", "on_error"]})

print("=== 实时推送与自动交易（含权限门与 SSE 端点）===")
hit("/api/stream/status")
hit("/api/stream/test?kind=quotes&symbols=600519")
hit("/api/stream/quotes?symbols=")                      # 缺标的：SSE 端点的错误分支也必须是严格 JSON
hit("/api/trade/config")
hit("/api/trade/account")
hit("/api/trade/orders?limit=5")
hit("/api/trade/export")
print("=== 买入扫描 / 交易规则 / 买卖点位 / 复盘 ===")
hit("/api/rules?market=cn")
hit("/api/rules?market=us")
hit("/api/scan/config")
hit("/api/scan/config", {"patch": {}})                      # 保存（空 patch，无副作用）
hit("/api/levels?code=600519")                             # 单只买卖点位
hit("/api/levels")                                         # 缺 code：错误分支
hit("/api/levels?code=__NOT_EXIST__")                      # 无效代码：降级分支
hit("/api/review/summary")
hit("/api/scan/run", {"market": "cn", "limit": 3, "barsLimit": 6})   # 小样本扫描（重操作，只取 6 只K线）
hit("/api/trade/status")                                 # 调度器状态（含为什么没动作）
hit("/api/trade/scheduler", {"running": False})           # 停调度（幂等）
hit("/api/trade/scheduler", {"once": True})               # 试跑：跳过时段限制，但仍要求 enabled
hit("/api/trade/config", {"patch": {"enabled": False, "mode": "dryrun"}})   # 回到安全默认
hit("/api/trade/execute", {"market": "cn"})             # 无口令：必须被拒且是严格 JSON
hit("/api/trade/cancel", {})                            # 缺 id：错误分支
hit("/api/trade/ack", {"id": "__MISSING__"})            # 不存在的委托

print("\n已校验接口 %d 个" % len(checked))
if bad:
    print("发现非严格 JSON 响应 %d 个：" % len(bad))
    for label, err, snippet in bad:
        print("  - %s → %s\n      %s" % (label, err, snippet))
else:
    print("全部为严格合法 JSON（浏览器 JSON.parse 可解析）")

# 附带确认：无亏损任务的 profitFactor 现在是 null 且带显式标记
null_pf = [r["code"] for r in ov["rows"] if r["stats"]["profitFactor"] is None]
flag = [r["code"] for r in ov["rows"] if r["stats"].get("profitFactorInfinite")]
print("\n盈亏比为空（无亏损）的任务: %s" % (null_pf or "无"))
print("其中带 profitFactorInfinite 标记: %s" % (flag or "无"))
