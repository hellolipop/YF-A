#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模拟交易 / 自动交易引擎（core/trader.py）的单元测试。

覆盖范围（与需求逐条对应）
--------------------------
A. TestConstants        公开 API 面、常量取值、DEFAULT_CONFIG 字段集与默认值；
B. TestNormalizeConfig  合并 / 夹取 / 生成口令 / 丢未知键 / 脏输入不抛异常；
C. TestConfigStore      get_config / save_config 的读写与「只提交一个字段」的 PATCH 语义；
D. TestAccount          account_id 形态、建账、已有账户不被重置、视图（含 priced 标记）、重置；
E. TestPlanGates        **十一个闸门逐个**用例（特别是不开启时 orders 必须为空）；
F. TestPlanSizing       open / add / reduce / close 的股数、整手取整、三种上限缩减、原因文案；
G. TestExecuteDryrun    dryrun 前后现金 / 持仓 / 累计量 / 权益曲线**逐字段不变**；
H. TestExecutePaper     成交成本与现金 / 持仓 / 均价与手工复算逐位一致（容差 1e-6）；
I. TestRejected         拒单三情形（无报价、现金不足、无持仓/持仓不足）不部分成交；
J. TestOrderFlows       撤单、回执 ack、导出待执行意图、手动平仓（不受 enabled 限制）；
K. TestScan             一步到底（symbols 构造、recommend 入参、默认 execute=False）；
L. TestSnapshotJson     快照结构 + 所有公开返回值都能 json.dumps(allow_nan=False)；
M. TestDirtyInput       脏输入（advice / rows / quotes / code / orders / config）不抛异常；
N. TestPersistence      新 Store 实例读回一致（配置 / 账户 / 累计量 / 委托单）。

设计原则（为什么这样造数据）
----------------------------
· **确定性**：不联网、不用 random、不依赖真实时间（时间戳只断言「是正整数」）；
· **手算对账**：成本类断言一律用与实现同源的公式在测试里重算
  （``qty × price × (1+滑点) × (1+费率)``），而不是把实现算出来的数字抄一遍；
· **用被测 API 造夹具**：建仓走 ``execute_orders``（paper），因此后续断言站在
  「同一个口径」上；只有需要隔离时（例如现金不足）才直接改写 state；
· **断言的 docstring 说明「为什么断言这件事」**，而不是复述代码。

运行方式::
    python3 tests/test_trader.py
    python3 -m unittest discover -s tests -p "test_*.py"
"""

import functools
import json
import math
import os
import re
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta

# 让测试既能在 stock-terminal/ 下跑，也能在仓库根目录下跑
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import advisor as A                    # noqa: E402
from core import trader as T                     # noqa: E402
from core.storage import Store, now_ms           # noqa: E402

# --------------------------------------------------------------------------- #
# 常量：被测代码的契约集中放这里，避免散落在断言里
# --------------------------------------------------------------------------- #
#: 成本参数（与 core/advisor._round_trip / storage 默认列值同源）
FEE = T.DEFAULT_FEE                 # 0.0003
SLIP = T.DEFAULT_SLIPPAGE           # 0.001
CAP = T.DEFAULT_CONFIG["capital"]   # 100000.0
LOT = 100                           # A 股一手

#: 配置字段集（normalize_config 必须一字不差地返回这些键）
CONFIG_KEYS = {
    "enabled", "mode", "market", "capital", "maxWeight", "maxPositions",
    "maxOrdersPerDay", "maxOrderAmount", "minConfidence", "allowReduce",
    "universe", "whitelist", "interval", "webhook", "confirmToken", "updatedAt",
}

#: 委托单字段集（与 store.save_trade_order 的列一一对应）
ORDER_KEYS = {
    "id", "createdAt", "updatedAt", "market", "code", "name", "side", "intent",
    "action", "mode", "status", "source", "reason", "confidence", "score",
    "kellyWeight", "targetWeight", "qty", "lot", "limitPrice", "signalPrice",
    "fillPrice", "fee", "slippage", "amount", "filledAt", "reviewId", "extRef",
    "error", "payload",
}


# --------------------------------------------------------------------------- #
# 手算公式（与 core/advisor._round_trip 完全同源，故意在测试里重写一遍）
# --------------------------------------------------------------------------- #
def buy_all_in(qty, price, fee=FEE, slip=SLIP):
    """买入总成本：qty × price × (1+滑点) × (1+费率)。"""
    return qty * price * (1.0 + slip) * (1.0 + fee)


def sell_net_of(qty, price, fee=FEE, slip=SLIP):
    """卖出净收入：qty × price × (1−滑点) × (1−费率)。"""
    return qty * price * (1.0 - slip) * (1.0 - fee)


def fill_buy_price(price, slip=SLIP):
    return price * (1.0 + slip)


def fill_sell_price(price, slip=SLIP):
    return price * (1.0 - slip)


def new_avg(q_old, avg_old, qty, price, fee, slip=SLIP):
    """加仓均价：new_avg = (qty_old×avg_old + qty×fillPrice + fee) / (qty_old + qty)。"""
    fill = fill_buy_price(price, slip)
    return (q_old * avg_old + qty * fill + qty * fill * fee) / (q_old + qty)


# --------------------------------------------------------------------------- #
# 夹具构造
# --------------------------------------------------------------------------- #
def _scaled(v, k):
    """价格派生位（止损 / 目标位）：价格为 None 时保持 None（不臆造）。"""
    return None if not isinstance(v, (int, float)) else v * k


def make_row(code="600519", action="buy", price=100.0, weight=0.25, confidence=0.6,
             score=80.0, ok=True, market="cn", **over):
    """构造一行 recommend 结论（字段与 core/advisor.py / advisor.js 的契约一致）。"""
    row = {
        "ok": ok, "code": code, "name": code, "market": market,
        "price": price, "changePct": 1.0, "asOf": "2026-03-16", "bars": 300,
        "action": action, "actionText": action or "数据不足",
        "score": score, "confidence": confidence,
        "signals": [{"key": "ma", "label": "均线", "dir": "up", "brief": "MA5 上穿"}],
        "ensemble": {"net": 3, "votes": {}},
        "edge": {"trades": 8, "winRate": 0.5, "payoff": 1.4},
        "kelly": {"fStar": 0.3, "kind": "discrete", "weight": weight,
                  "rawWeight": weight, "amount": 0.0, "shares": 0, "lot": LOT},
        "forecast": {"expectedReturn": 3.2, "upProb": 0.6, "sample": 40,
                     "quantiles": {"5": -3.0, "50": 1.2, "95": 8.0}},
        "plan": {"entry": price, "stop": _scaled(price, 0.9),
                 "target1": _scaled(price, 1.2), "target2": _scaled(price, 1.3),
                 "riskReward": 1.5},
        "risk": {"atrPct": 2.1, "maxDrawdown": 12.5},
        "advisor": {"marks": [], "forecast": {"path": [], "horizon": 20},
                    "plan": {"entry": price}},
        "note": "测试行",
    }
    row.update(over)
    return row


def make_advice(rows, **over):
    """构造一次 recommend 响应（plan_orders 的标的来源就是 advice["rows"]）。"""
    rows = list(rows)
    res = {
        "ok": True, "market": "cn", "horizon": 20, "capital": CAP,
        "requested": len(rows), "count": len(rows),
        "analyzed": sum(1 for r in rows if isinstance(r, dict) and r.get("ok")),
        "rows": rows, "portfolio": {"totalWeight": 0.0, "count": 0, "rows": []},
        "reviewId": "ar-1700000000000-abcd",
    }
    res.update(over)
    return res


def buy_order(code="600519", qty=200, price=100.0, market="cn", intent="open",
              oid=None, source="manual", **over):
    """一张现成的买单（用于「按报价成交」类的精确对账，绕开计划层的上限夹取）。"""
    ts = now_ms()
    order = {
        "id": oid or T.order_id(ts), "createdAt": ts, "updatedAt": ts,
        "market": market, "code": code, "name": code, "side": "buy", "intent": intent,
        "action": "buy", "mode": "paper", "status": "pending", "source": source,
        "reason": "测试建仓", "confidence": 0.6, "score": 80.0, "kellyWeight": 0.25,
        "targetWeight": 0.2, "qty": qty, "lot": T.lot_of(market),
        "limitPrice": price, "signalPrice": price, "fillPrice": None, "fee": 0.0,
        "slippage": 0.0, "amount": qty * price, "filledAt": None, "reviewId": "",
        "extRef": "", "error": "",
        "payload": {"plan": {}, "forecast": {}, "gates": {},
                    "account": {"cash": CAP, "positions": 0},
                    "input": {"price": price, "capital": CAP}},
    }
    order.update(over)
    return order


def sell_order(code="600519", qty=100, price=100.0, market="cn", intent="reduce",
               oid=None, **over):
    order = buy_order(code=code, qty=qty, price=price, market=market, oid=oid, **over)
    order.update({"side": "sell", "intent": intent, "action": "reduce"})
    return order


def paper_cfg(**over):
    """paper 模式配置（enabled=True）；默认其余字段与 DEFAULT_CONFIG 一致。"""
    patch = {"enabled": True, "mode": "paper"}
    patch.update(over)
    return T.normalize_config(patch)


class TraderCase(unittest.TestCase):
    """测试基类：提供内存库 / 文件库 / 计划快捷方式 / JSON 自检。"""

    def make_store(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        return store

    def file_store(self):
        tmp = tempfile.mkdtemp(prefix="trader-test-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "strategy.db")
        store = Store(path)
        self.addCleanup(store.close)
        return store, path

    def plan(self, store, cfg, rows, quotes=None, account=None, market="cn"):
        acc = account if account is not None else T.ensure_account(store, cfg, market)
        return T.plan_orders(make_advice(rows), acc, cfg, quotes or {}, store)

    def seed(self, store, cfg, code="600519", qty=200, price=100.0, market="cn",
             intent="open"):
        """用被测的 paper 成交路径建仓（保证后续断言与实现同口径）。"""
        res = T.execute_orders(store, cfg, [buy_order(code=code, qty=qty, price=price,
                                                     market=market, intent=intent)],
                               {code: price})
        self.assertEqual(res["filled"], 1, "夹具建仓失败：%s" % res["reason"])
        return res

    def json_ok(self, obj, label=""):
        """公开返回值必须能 json.dumps(allow_nan=False)：NaN / inf 会直接让前端崩。"""
        try:
            text = json.dumps(obj, allow_nan=False)
        except (TypeError, ValueError) as e:  # pragma: no cover - 失败即断言
            self.fail("%s 无法 json.dumps(allow_nan=False)：%s" % (label, e))
        return text

    def state_json(self, store, aid):
        """账户状态的逐字段快照（用于 dryrun 前后比对）。"""
        return json.dumps(store.get_trade_state(aid), sort_keys=True, allow_nan=False)


# --------------------------------------------------------------------------- #
# A. 常量与公开 API
# --------------------------------------------------------------------------- #
class TestConstants(TraderCase):
    def test_public_api_surface(self):
        """服务端按名字调用这些 API；缺一个就是线上 500，因此显式锁定名字。"""
        for name in ("MODES", "ORDER_STATUS", "DEFAULT_CONFIG", "CONFIRM_HEADER",
                     "default_config", "normalize_config", "get_config", "save_config",
                     "account_id", "ensure_account", "account_view", "plan_orders",
                     "execute_orders", "cancel_order", "close_position",
                     "reset_account", "scan", "ack_order", "export_intents",
                     "trade_snapshot"):
            self.assertTrue(hasattr(T, name), "缺少公开 API：%s" % name)

    def test_modes_and_status(self):
        """模式只有 dryrun / paper；状态机覆盖外发与终态，顺序对外可见。"""
        self.assertEqual(T.MODES, ("dryrun", "paper"))
        self.assertEqual(T.ORDER_STATUS, ("pending", "filled", "rejected", "cancelled",
                                          "submitted", "acked", "expired"))
        self.assertEqual(T.CONFIRM_HEADER, "X-Trade-Confirm")

    def test_default_config_is_safe(self):
        """默认必须「装好也不会自己下单」：enabled=False、mode=dryrun、无外发地址。"""
        self.assertIs(T.DEFAULT_CONFIG["enabled"], False)
        self.assertEqual(T.DEFAULT_CONFIG["mode"], "dryrun")
        self.assertEqual(T.DEFAULT_CONFIG["confirmToken"], "")
        self.assertEqual(set(T.DEFAULT_CONFIG), CONFIG_KEYS)
        cfg = T.default_config()
        self.assertIs(cfg["enabled"], False)
        self.assertEqual(cfg["mode"], "dryrun")
        self.assertRegex(cfg["confirmToken"], r"^[0-9a-f]{6,10}$")

    def test_module_notes_do_not_leak_token(self):
        """说明文案里不允许出现口令（口令是密钥，只能在配置里）。"""
        cfg = paper_cfg()
        self.assertNotIn(cfg["confirmToken"], T.ORDER_NOTE)
        self.assertNotIn(cfg["confirmToken"], T.ACCOUNT_NOTE)
        self.assertNotIn(cfg["confirmToken"], T.CONFIG_NOTE)


# --------------------------------------------------------------------------- #
# B. 配置归一
# --------------------------------------------------------------------------- #
class TestNormalizeConfig(TraderCase):
    def test_full_keyset_and_unknown_dropped(self):
        """无论输入多脏都必须返回完整字段集；未知键一律丢弃（结构稳定）。"""
        for patch in (None, {}, [], "x", 3, {"nope": 1}, {"enabled": []}):
            cfg = T.normalize_config(patch)
            self.assertEqual(set(cfg), CONFIG_KEYS, "patch=%r" % (patch,))
            self.assertNotIn("nope", cfg)
        self.assertEqual(set(T.normalize_config({"x": 1})), CONFIG_KEYS)

    def test_bool_fields_strict(self):
        """布尔字段严格转 bool：真值词表内的字符串才算 True，其余回退默认值。"""
        self.assertIs(T.normalize_config({"enabled": True})["enabled"], True)
        self.assertIs(T.normalize_config({"enabled": "yes"})["enabled"], True)
        self.assertIs(T.normalize_config({"enabled": 1})["enabled"], True)
        self.assertIs(T.normalize_config({"enabled": "on"})["enabled"], True)
        self.assertIs(T.normalize_config({"enabled": "false"})["enabled"], False)
        self.assertIs(T.normalize_config({"enabled": 0})["enabled"], False)
        # 非法（无法判定）回退默认值 False，而不是「非空即真」
        for bad in ("maybe", [], {}, 3.5 * 0):
            self.assertIs(T.normalize_config({"enabled": bad})["enabled"], False,
                          "bad=%r" % (bad,))
        # allowReduce 默认 True，显式关掉必须生效（0 / "no" 都算 False）
        self.assertIs(T.normalize_config({})["allowReduce"], True)
        self.assertIs(T.normalize_config({"allowReduce": 0})["allowReduce"], False)
        self.assertIs(T.normalize_config({"allowReduce": "no"})["allowReduce"], False)

    def test_mode_and_market(self):
        """mode 只允许 dryrun/paper；market 归一到 cn/us（hk 等未知市场按 cn）。"""
        self.assertEqual(T.normalize_config({"mode": "PAPER"})["mode"], "paper")
        self.assertEqual(T.normalize_config({"mode": "dryrun"})["mode"], "dryrun")
        for bad in ("backtest", "", None, 3, "pa per"):
            self.assertEqual(T.normalize_config({"mode": bad})["mode"], "dryrun",
                             "bad=%r" % (bad,))
        self.assertEqual(T.normalize_config({"market": "US"})["market"], "us")
        self.assertEqual(T.normalize_config({"market": "usa"})["market"], "us")
        self.assertEqual(T.normalize_config({"market": "hk"})["market"], "cn")

    def test_ratio_clamped(self):
        """比例字段一律夹到 [0, 1]：负权重会变成「做空」，绝不能放行。"""
        self.assertEqual(T.normalize_config({"maxWeight": -0.5})["maxWeight"], 0.0)
        self.assertEqual(T.normalize_config({"maxWeight": 2})["maxWeight"], 1.0)
        self.assertEqual(T.normalize_config({"maxWeight": "0.1"})["maxWeight"], 0.1)
        self.assertEqual(T.normalize_config({"minConfidence": -3})["minConfidence"], 0.0)
        self.assertEqual(T.normalize_config({"minConfidence": 9})["minConfidence"], 1.0)
        self.assertEqual(T.normalize_config({})["maxWeight"], 0.25)

    def test_int_clamped(self):
        """整数字段夹到合理区间：interval 15..3600 是硬要求（防刷接口 / 防呆）。"""
        self.assertEqual(T.normalize_config({"interval": 5})["interval"], 15)
        self.assertEqual(T.normalize_config({"interval": 99999})["interval"], 3600)
        self.assertEqual(T.normalize_config({"interval": "abc"})["interval"], 60)
        self.assertEqual(T.normalize_config({"interval": 120.6})["interval"], 121)
        self.assertEqual(T.normalize_config({"maxPositions": 0})["maxPositions"], 1)
        self.assertEqual(T.normalize_config({"maxPositions": 99999})["maxPositions"], 1000)
        self.assertEqual(T.normalize_config({"maxOrdersPerDay": -3})["maxOrdersPerDay"], 1)
        self.assertEqual(T.normalize_config({"maxOrdersPerDay": 5000})["maxOrdersPerDay"], 1000)

    def test_capital_and_amount(self):
        """本金非法（≤0/非数值）回退默认；负数单笔上限按 0（= 不允许下单）。"""
        for bad in (0, -1, "x", None, float("nan"), float("inf")):
            self.assertEqual(T.normalize_config({"capital": bad})["capital"], CAP,
                             "bad=%r" % (bad,))
        self.assertEqual(T.normalize_config({"capital": 50000})["capital"], 50000.0)
        self.assertEqual(T.normalize_config({"capital": 1e12})["capital"], 1e10)
        self.assertEqual(T.normalize_config({"maxOrderAmount": -5})["maxOrderAmount"], 0.0)
        self.assertEqual(T.normalize_config({"maxOrderAmount": "abc"})["maxOrderAmount"], 50000.0)

    def test_codes_upper_dedup_and_split(self):
        """universe/whitelist 统一大写去重且保持输入顺序；字符串按逗号/空白切分。"""
        cfg = T.normalize_config({"universe": ["aapl", "AAPL", " 600519 ", "", None, 1]})
        self.assertEqual(cfg["universe"], ["AAPL", "600519", "1"])
        cfg = T.normalize_config({"universe": "600519, 000001 600519；aapl"})
        self.assertEqual(cfg["universe"], ["600519", "000001", "AAPL"])
        self.assertEqual(T.normalize_config({"whitelist": ("b", "a", "b")})["whitelist"],
                         ["B", "A"])
        # 非法类型回退默认（空数组），不抛异常
        self.assertEqual(T.normalize_config({"universe": 3})["universe"], [])
        self.assertEqual(T.normalize_config({"universe": {"a": 1}})["universe"], [])

    def test_confirm_token_generated_and_preserved(self):
        """缺失时生成 6~10 位十六进制；合法值沿用（小写化）；非法值重新生成。"""
        tok = T.normalize_config({})["confirmToken"]
        self.assertRegex(tok, r"^[0-9a-f]{6,10}$")
        self.assertEqual(T.normalize_config({"confirmToken": "ABC123"})["confirmToken"],
                         "abc123")
        again = T.normalize_config({"confirmToken": "zzz"})["confirmToken"]
        self.assertRegex(again, r"^[0-9a-f]{6,10}$")
        self.assertNotEqual(again, "zzz")
        # base 里已有口令时，未提及该字段的 patch 不得把它冲掉（外部桥接要一直用同一口令）
        base = {"confirmToken": "deadbe"}
        self.assertEqual(T.normalize_config({"interval": 30}, base)["confirmToken"],
                         "deadbe")

    def test_updated_at_passthrough(self):
        """updatedAt 原样透传（由 save_config 盖戳），非法值归 None。"""
        self.assertEqual(T.normalize_config({"updatedAt": 1700000000000})["updatedAt"],
                         1700000000000)
        self.assertIsNone(T.normalize_config({"updatedAt": -5})["updatedAt"])
        self.assertIsNone(T.normalize_config({})["updatedAt"])

    def test_webhook_only_http(self):
        """外发地址只接受 http(s)：其它协议（file:// 等）一律清空，避免误发。"""
        self.assertEqual(T.normalize_config({"webhook": " http://a/b "})["webhook"],
                         "http://a/b")
        self.assertEqual(T.normalize_config({"webhook": "https://x/y"})["webhook"],
                         "https://x/y")
        for bad in ("file:///etc/passwd", "javascript:alert(1)", "//evil", 3, None):
            self.assertEqual(T.normalize_config({"webhook": bad})["webhook"], "",
                             "bad=%r" % (bad,))

    def test_base_merge_semantics(self):
        """patch 覆盖 base，但 patch 里的 None（=未提交）不能把 base 冲掉。"""
        base = {"enabled": True, "mode": "paper", "capital": 5000.0, "universe": ["A"]}
        cfg = T.normalize_config({"maxWeight": 0.5}, base)
        self.assertIs(cfg["enabled"], True)
        self.assertEqual(cfg["mode"], "paper")
        self.assertEqual(cfg["capital"], 5000.0)
        self.assertEqual(cfg["universe"], ["A"])
        self.assertEqual(cfg["maxWeight"], 0.5)
        cfg = T.normalize_config({"capital": None, "universe": []}, base)
        self.assertEqual(cfg["capital"], 5000.0, "None 不应覆盖 base")
        self.assertEqual(cfg["universe"], [], "空数组是合法的「清空」语义")

    def test_garbage_never_raises(self):
        """任何脏 patch 都不许抛异常，且返回可 JSON 化（前端直接吃这个对象）。"""
        patches = [None, 0, "x", [], {}, {"enabled": []}, {"capital": float("nan")},
                   {"universe": {}}, {"mode": {"a": 1}}, {"interval": [1]},
                   {"confirmToken": 12345}, {"maxOrderAmount": float("-inf")}]
        for patch in patches:
            cfg = T.normalize_config(patch)
            self.assertEqual(set(cfg), CONFIG_KEYS, "patch=%r" % (patch,))
            self.json_ok(cfg, "normalize_config(%r)" % (patch,))


# --------------------------------------------------------------------------- #
# C. 配置落库
# --------------------------------------------------------------------------- #
class TestConfigStore(TraderCase):
    def test_get_config_defaults_on_empty_store(self):
        """从未写过配置时返回默认值（默认值的唯一来源是 core/trader.py）。"""
        store = self.make_store()
        cfg = T.get_config(store)
        self.assertEqual(set(cfg), CONFIG_KEYS)
        self.assertIs(cfg["enabled"], False)
        self.assertEqual(cfg["mode"], "dryrun")
        self.assertEqual(store.get_trade_config(), {}, "只读操作不得顺手写库")
        # store 缺失（None / 非法对象）也不能 500
        self.assertEqual(T.get_config(None)["mode"], "dryrun")
        self.assertEqual(T.get_config(object())["capital"], CAP)

    def test_save_config_merges_and_persists(self):
        """save_config 是「合并 + 校验 + 落库」，返回新配置并盖 updatedAt。"""
        store = self.make_store()
        cfg = T.save_config(store, {"enabled": True, "mode": "paper", "capital": 200000})
        self.assertIs(cfg["enabled"], True)
        self.assertEqual(cfg["mode"], "paper")
        self.assertEqual(cfg["capital"], 200000.0)
        self.assertIsInstance(cfg["updatedAt"], int)
        self.assertGreater(cfg["updatedAt"], 0)
        self.assertEqual(store.get_trade_config()["capital"], 200000.0)
        self.assertEqual(T.get_config(store)["enabled"], True)
        # 只提交一个字段：其余字段保持（PATCH 语义）
        token = cfg["confirmToken"]
        cfg2 = T.save_config(store, {"interval": 30})
        self.assertEqual(cfg2["interval"], 30)
        self.assertIs(cfg2["enabled"], True)
        self.assertEqual(cfg2["mode"], "paper")
        self.assertEqual(cfg2["confirmToken"], token, "口令不应因为改 interval 而漂移")

    def test_save_config_dirty_does_not_raise(self):
        """脏 patch 回退默认值，不能把脏值写进库（否则一次误提交毁掉配置）。"""
        store = self.make_store()
        cfg = T.save_config(store, {"mode": "???", "capital": -1, "universe": "aaa bbb",
                                    "interval": 1, "webhook": "ftp://x"})
        self.assertEqual(cfg["mode"], "dryrun")
        self.assertEqual(cfg["capital"], CAP)
        self.assertEqual(cfg["universe"], ["AAA", "BBB"])
        self.assertEqual(cfg["interval"], 15)
        self.assertEqual(cfg["webhook"], "")

    def test_config_roundtrip_new_store_instance(self):
        """换一个 Store 实例读回必须一致（配置是审计链的一部分）。"""
        store, path = self.file_store()
        saved = T.save_config(store, {"enabled": True, "mode": "paper",
                                      "universe": ["600519"], "maxOrderAmount": 30000})
        store.close()
        again = Store(path)
        self.addCleanup(again.close)
        got = T.get_config(again)
        self.json_ok(got, "get_config")
        for key in CONFIG_KEYS:
            self.assertEqual(got[key], saved[key], "字段 %s 往返不一致" % key)


# --------------------------------------------------------------------------- #
# D. 账户
# --------------------------------------------------------------------------- #
class TestAccount(TraderCase):
    def test_account_id_format(self):
        """账户 id 是 '<mode>:<market>'，缺省 mode=paper（服务端按它取模拟成交账户）。"""
        self.assertEqual(T.account_id("cn"), "paper:cn")
        self.assertEqual(T.account_id("cn", "dryrun"), "dryrun:cn")
        self.assertEqual(T.account_id("us", "paper"), "paper:us")
        self.assertEqual(T.account_id("hk"), "paper:cn")
        self.assertEqual(T.account_id(None, "nope"), "paper:cn")

    def test_ensure_account_creates_from_config(self):
        """不存在时按 config 建账：本金 = capital、整手按市场、成本参数与 advisor 同源。"""
        store = self.make_store()
        cfg = paper_cfg(capital=200000.0)
        acc = T.ensure_account(store, cfg, "cn")
        self.assertEqual(acc["accountId"], "paper:cn")
        self.assertEqual(acc["market"], "cn")
        self.assertEqual(acc["mode"], "paper")
        self.assertEqual(acc["cash"], 200000.0)
        self.assertEqual(acc["initial"], 200000.0)
        self.assertEqual(acc["lot"], LOT)
        self.assertEqual(acc["feeRate"], FEE)
        self.assertEqual(acc["slippage"], SLIP)
        self.assertEqual(acc["positions"], [])
        self.assertEqual(T.ensure_account(store, cfg, "us")["lot"], 1, "美股 1 股/手")

    def test_ensure_account_existing_not_reset(self):
        """已有账户不因配置变化而重置（账户是资产，配置是运行参数）。"""
        store = self.make_store()
        cfg = paper_cfg()
        self.seed(store, cfg, qty=200, price=100.0)
        acc = T.ensure_account(store, cfg, "cn")
        cash = acc["cash"]
        self.assertEqual(len(acc["positions"]), 1)
        # 配置改了本金，也不能把账户冲掉
        acc2 = T.ensure_account(store, paper_cfg(capital=1.0), "cn")
        self.assertAlmostEqual(acc2["cash"], cash, delta=1e-9)
        self.assertEqual(len(acc2["positions"]), 1)

    def test_dryrun_and_paper_ledgers_are_separate(self):
        """dryrun 与 paper 各自一本账：演练账户的现金不因成交而变化。"""
        store = self.make_store()
        self.seed(store, paper_cfg(), qty=100, price=100.0)
        dry = T.ensure_account(store, T.normalize_config({"mode": "dryrun"}), "cn")
        self.assertEqual(dry["accountId"], "dryrun:cn")
        self.assertEqual(dry["cash"], CAP)
        self.assertEqual(dry["positions"], [])

    def test_account_view_priced_flag(self):
        """缺报价时 lastPrice 回退 avgPrice 且标 priced=false（绝不臆造价格）。"""
        store = self.make_store()
        cfg = paper_cfg()
        self.seed(store, cfg, qty=200, price=100.0)
        view = T.account_view(store, cfg, None)
        self.assertTrue(set(["accountId", "market", "mode", "cash", "initial", "equity",
                             "marketValue", "pnl", "returnPct", "realizedPnl", "feeTotal",
                             "positionCount", "positions", "weights"]).issubset(view))
        self.assertFalse(view["priced"])
        pos = view["positions"][0]
        self.assertFalse(pos["priced"])
        self.assertAlmostEqual(pos["lastPrice"], pos["avgPrice"], delta=1e-9)
        self.assertAlmostEqual(view["equity"], view["cash"] + view["marketValue"], delta=1e-6)
        self.assertEqual(view["positionCount"], 1)

        view2 = T.account_view(store, cfg, {"600519": 120.0})
        self.assertTrue(view2["priced"])
        pos2 = view2["positions"][0]
        self.assertAlmostEqual(pos2["lastPrice"], 120.0, delta=1e-9)
        self.assertAlmostEqual(pos2["marketValue"], 200 * 120.0, delta=1e-6)
        self.assertAlmostEqual(pos2["pnl"], 200 * 120.0 - buy_all_in(200, 100.0), delta=1e-6)
        self.assertAlmostEqual(pos2["weight"], 200 * 120.0 / CAP, delta=1e-6)
        self.assertAlmostEqual(view2["weights"]["600519"], pos2["weight"], delta=1e-9)
        # 权益 = 现金 + 市值；收益率是相对初始本金的百分数
        self.assertAlmostEqual(view2["equity"], view2["cash"] + 200 * 120.0, delta=1e-6)
        self.assertAlmostEqual(view2["returnPct"],
                               (view2["equity"] / CAP - 1.0) * 100.0, delta=1e-4)

    def test_account_view_virtual_when_missing(self):
        """账户不存在时返回「按配置推出来的空账户」，且不落库（只读接口不该有副作用）。"""
        store = self.make_store()
        cfg = paper_cfg()
        view = T.account_view(store, cfg, None)
        self.assertFalse(view["exists"])
        self.assertEqual(view["cash"], CAP)
        self.assertEqual(view["positionCount"], 0)
        self.assertIsNone(store.get_trade_state("paper:cn"), "account_view 不应建账")
        self.json_ok(view, "account_view")

    def test_reset_account_keeps_history(self):
        """重置只清账户与累计量，**不删委托单**（审计需要），并在返回值里说明。"""
        store = self.make_store()
        cfg = paper_cfg()
        self.seed(store, cfg, qty=200, price=100.0)
        view = T.execute_orders(store, cfg, [sell_order(qty=200, intent="close")],
                                {"600519": 100.0})
        self.assertEqual(view["account"]["positionCount"], 0)
        orders_before = store.list_trade_orders(limit=100)["total"]
        self.assertGreater(orders_before, 0)

        reset = T.reset_account(store, cfg, "cn")
        self.assertTrue(reset["reset"])
        self.assertEqual(reset["cash"], CAP)
        self.assertEqual(reset["positions"], [])
        self.assertEqual(reset["realizedPnl"], 0.0)
        self.assertEqual(reset["feeTotal"], 0.0)
        self.assertIn("保留", reset["note"])
        self.assertEqual(store.list_trade_orders(limit=100)["total"], orders_before,
                         "重置不得删除历史委托单")
        self.assertEqual(store.get_trade_state("paper:cn")["cash"], CAP)
        self.json_ok(reset, "reset_account")


# --------------------------------------------------------------------------- #
# E. 十一道风控闸门（逐个用例）
# --------------------------------------------------------------------------- #
class TestPlanGates(TraderCase):
    def test_gate1_disabled_means_no_orders(self):
        """闸门 1：总开关关闭时 orders 必须为空，且每只标的都写明原因（不静默丢弃）。"""
        store = self.make_store()
        cfg = T.normalize_config({"mode": "paper"})           # enabled 默认 False
        plan = self.plan(store, cfg, [make_row(), make_row(code="000001", price=10.0)],
                         {"600519": 100.0, "000001": 10.0})
        self.assertEqual(plan["orders"], [])
        self.assertEqual(plan["count"], 0)
        self.assertEqual(len(plan["skipped"]), 2)
        for s in plan["skipped"]:
            self.assertEqual(s["gate"], "enabled")
            self.assertIn("自动交易未开启（总开关关闭）", s["reason"])
        self.assertEqual(plan["skippedByGate"], {"enabled": 2})
        self.json_ok(plan, "plan_orders(gate1)")

    def test_gate2_empty_symbol_pool(self):
        """闸门 2：没有可研判的标的 → 空计划 + note 说明（advice 来源是 rows）。"""
        store = self.make_store()
        cfg = paper_cfg()
        acc = T.ensure_account(store, cfg, "cn")
        for advice in (make_advice([]), {}, None, "x", {"rows": "bad"}, {"rows": None}, 3):
            plan = T.plan_orders(advice, acc, cfg, {}, store)
            self.assertEqual(plan["orders"], [], "advice=%r" % (advice,))
            self.assertIn("标的池为空", plan["note"])
            self.json_ok(plan, "plan_orders(gate2)")

    def test_gate3_whitelist(self):
        """闸门 3：白名单非空时只允许白名单内的代码。"""
        store = self.make_store()
        cfg = paper_cfg(whitelist=["000001"])
        plan = self.plan(store, cfg,
                         [make_row(code="600519", price=100.0),
                          make_row(code="000001", price=10.0)],
                         {"600519": 100.0, "000001": 10.0})
        self.assertEqual([o["code"] for o in plan["orders"]], ["000001"])
        self.assertEqual(plan["skipped"][0]["gate"], "whitelist")
        self.assertEqual(plan["skipped"][0]["code"], "600519")
        self.assertIn("不在交易白名单内", plan["skipped"][0]["reason"])

    def test_gate4_data_insufficient_and_order_before_price(self):
        """闸门 4：ok=False → 数据不足；且它排在价格闸门**之前**（顺序即口径）。"""
        store = self.make_store()
        cfg = paper_cfg()
        row = make_row(ok=False, price=None,
                       error="历史数据不足（有效K线 10 根 < 60 根）")
        plan = self.plan(store, cfg, [row], {"600519": 100.0})
        self.assertEqual(plan["count"], 0)
        s = plan["skipped"][0]
        self.assertEqual(s["gate"], "data", "同时缺价格与数据时，必须先报数据不足")
        self.assertIn("研判数据不足：历史数据不足", s["reason"])

    def test_gate5_invalid_price(self):
        """闸门 5：价格为空 / 0 / 负数都算无有效价格（有报价时以报价为准）。"""
        store = self.make_store()
        cfg = paper_cfg()
        rows = [make_row(price=None), make_row(code="000001", price=0),
                make_row(code="000002", price=-5)]
        plan = self.plan(store, cfg, rows, {})
        self.assertEqual(plan["count"], 0)
        self.assertEqual([s["gate"] for s in plan["skipped"]],
                         ["price", "price", "price"])
        self.assertIn("无有效价格", plan["skipped"][0]["reason"])

    def test_gate6_confidence_threshold(self):
        """闸门 6：置信度缺失或低于阈值都拦；恰好等于阈值必须放行（口径是 <）。"""
        store = self.make_store()
        cfg = paper_cfg()                                     # minConfidence = 0.25
        rows = [make_row(confidence=0.1),
                make_row(code="000001", price=10.0, confidence=None)]
        plan = self.plan(store, cfg, rows, {"600519": 100.0, "000001": 10.0})
        self.assertEqual(plan["count"], 0)
        self.assertEqual([s["gate"] for s in plan["skipped"]],
                         ["confidence", "confidence"])
        self.assertIn("置信度 10.00% 低于阈值 25.00%", plan["skipped"][0]["reason"])
        self.assertIn("置信度缺失", plan["skipped"][1]["reason"])
        plan2 = self.plan(store, cfg, [make_row(confidence=0.25)], {"600519": 100.0})
        self.assertEqual(plan2["count"], 1)

    def test_gate7_neutral_actions_no_skip(self):
        """闸门 7：hold / watch（含未给档位）是明确的无需动作 —— 不记 skip。"""
        store = self.make_store()
        cfg = paper_cfg()
        rows = [make_row(action="hold", price=100.0),
                make_row(code="000001", action="watch", price=10.0),
                make_row(code="000002", action=None, price=10.0)]
        plan = self.plan(store, cfg, rows, {})
        self.assertEqual(plan["count"], 0)
        self.assertEqual(plan["skipped"], [])
        self.assertEqual(plan["neutral"], 3)

    def test_gate8_daily_order_limit(self):
        """闸门 8：当日委托（含本批已计划）达到上限后不再下单。"""
        store = self.make_store()
        cfg = paper_cfg(maxOrdersPerDay=2)
        self.seed(store, cfg, code="600519", qty=100, price=100.0)   # 当天第 1 笔
        self.seed(store, cfg, code="000001", qty=100, price=10.0)    # 当天第 2 笔
        plan = self.plan(store, cfg, [make_row(code="600858", price=50.0)],
                         {"600858": 50.0})
        self.assertEqual(plan["count"], 0)
        s = plan["skipped"][0]
        self.assertEqual(s["gate"], "dailyLimit")
        self.assertIn("当日委托已达上限 2 笔", s["reason"])

    def test_gate8_counts_orders_planned_in_this_batch(self):
        """同一批里也要逐单扣减额度，否则一次扫描可以突破每日上限。"""
        store = self.make_store()
        cfg = paper_cfg(maxOrdersPerDay=2)
        rows = [make_row(code=c, price=100.0) for c in ("600519", "000001", "600002")]
        plan = self.plan(store, cfg, rows, {})
        self.assertEqual(plan["count"], 2)
        self.assertEqual(len(plan["skipped"]), 1)
        self.assertEqual(plan["skipped"][0]["gate"], "dailyLimit")
        self.assertIn("当日委托已达上限 2 笔", plan["skipped"][0]["reason"])

    def test_gate9_max_positions(self):
        """闸门 9：持仓只数已达上限时不开新仓（加仓不受此限）。"""
        store = self.make_store()
        cfg = paper_cfg(maxPositions=1)
        self.seed(store, cfg, code="600519", qty=100, price=100.0)
        plan = self.plan(store, cfg, [make_row(code="000001", price=10.0)],
                         {"000001": 10.0})
        self.assertEqual(plan["count"], 0)
        s = plan["skipped"][0]
        self.assertEqual(s["gate"], "maxPositions")
        self.assertIn("持仓只数已达上限 1 只", s["reason"])
        # 对已有持仓的 add 不触发该闸门
        plan2 = self.plan(store, cfg, [make_row(action="add", price=100.0, weight=0.25)],
                          {"600519": 100.0})
        self.assertEqual([o["intent"] for o in plan2["orders"]], ["add"])

    def test_gate9_counts_opens_in_this_batch(self):
        """同一批里已经计划的 open 也要占住持仓名额。"""
        store = self.make_store()
        cfg = paper_cfg(maxPositions=1)
        rows = [make_row(code="600519", price=100.0), make_row(code="000001", price=10.0)]
        plan = self.plan(store, cfg, rows, {})
        self.assertEqual([o["code"] for o in plan["orders"]], ["600519"])
        self.assertEqual(plan["skipped"][0]["gate"], "maxPositions")

    def test_gate10_reduce_disabled(self):
        """闸门 10：关闭自动减仓后，reduce / sell / avoid 一律不下单。"""
        store = self.make_store()
        cfg = paper_cfg(allowReduce=False)
        self.seed(store, cfg, qty=300, price=100.0)
        for action in ("reduce", "sell", "avoid"):
            plan = self.plan(store, cfg, [make_row(action=action, price=100.0)],
                             {"600519": 100.0})
            self.assertEqual(plan["count"], 0, action)
            self.assertEqual(plan["skipped"][0]["gate"], "reduceDisabled")
            self.assertIn("已关闭自动减仓", plan["skipped"][0]["reason"])

    def test_gate11_no_position_to_reduce(self):
        """闸门 11：无持仓却要减仓 / 清仓 → skip（不是空订单）。"""
        store = self.make_store()
        cfg = paper_cfg()
        for action in ("reduce", "sell", "avoid"):
            plan = self.plan(store, cfg, [make_row(action=action, price=100.0)],
                             {"600519": 100.0})
            self.assertEqual(plan["count"], 0, action)
            self.assertEqual(plan["skipped"][0]["gate"], "noPosition")
            self.assertIn("无对应持仓", plan["skipped"][0]["reason"])

    def test_gate10_precedes_gate11(self):
        """关闭减仓时，即便也没有持仓，报的也应是「已关闭自动减仓」（顺序 10 在 11 前）。"""
        store = self.make_store()
        cfg = paper_cfg(allowReduce=False)
        plan = self.plan(store, cfg, [make_row(action="reduce", price=100.0)],
                         {"600519": 100.0})
        self.assertEqual(plan["skipped"][0]["gate"], "reduceDisabled")

    def test_nothing_is_silently_dropped(self):
        """核心不变量：每只标的要么有订单、要么有 skip、要么是明确的中性档位。"""
        store = self.make_store()
        cfg = paper_cfg(whitelist=["600519", "000001", "000002"])
        rows = [make_row(code="600519", price=100.0),
                make_row(code="000001", price=10.0, action="hold"),
                make_row(code="000002", price=10.0, confidence=0.01),
                make_row(code="000858", price=10.0),                 # 白名单外
                make_row(code="600519", price=None, weight=0.1),     # 无价格
                make_row(code="000001", price=10.0, action="reduce")  # 无持仓
                ]
        plan = self.plan(store, cfg, rows, {"600519": 100.0, "000001": 10.0, "000002": 10.0})
        covered = plan["count"] + plan["skippedCount"] + plan["neutral"]
        self.assertEqual(covered, len(rows))
        self.assertEqual(plan["count"] + plan["skippedCount"] + plan["neutral"],
                         len(rows) - 0)
        for s in plan["skipped"]:
            self.assertTrue(s["reason"], "skip 必须带中文原因")
            self.assertIn(s["gate"], ("whitelist", "data", "price", "confidence",
                                      "dailyLimit", "maxPositions", "reduceDisabled",
                                      "noPosition", "sizing"))
        self.json_ok(plan, "plan_orders(mixed)")


# --------------------------------------------------------------------------- #
# F. 定量：open / add / reduce / close
# --------------------------------------------------------------------------- #
class TestPlanSizing(TraderCase):
    def test_order_contract_and_payload(self):
        """委托单字段集与 payload 快照必须完整（外部桥接与审计都按它读）。"""
        store = self.make_store()
        cfg = paper_cfg()
        plan = self.plan(store, cfg, [make_row(price=100.0, weight=0.2)], {"600519": 100.0})
        o = plan["orders"][0]
        self.assertTrue(ORDER_KEYS.issubset(o), "缺少字段：%s" % (ORDER_KEYS - set(o),))
        self.assertTrue(re.match(r"^ord-\d{13}-[0-9a-f]{4}$", o["id"]), o["id"])
        self.assertEqual(o["payload"].keys() and set(o["payload"]),
                         {"plan", "forecast", "gates", "account", "input"})
        self.assertEqual(o["payload"]["plan"], make_row()["plan"])
        self.assertEqual(o["payload"]["forecast"], make_row()["forecast"])
        payload = o["payload"]
        self.assertEqual(payload["input"], {"price": 100.0, "capital": CAP})
        self.assertEqual(payload["account"]["cash"], CAP)
        self.assertEqual(payload["account"]["positions"], 0)
        self.assertEqual(payload["gates"]["minConfidence"], 0.25)
        self.assertEqual(payload["gates"]["confirmHeader"], T.CONFIRM_HEADER)
        self.assertNotIn("confirmToken", payload["gates"], "口令绝不能进审计快照")
        self.assertEqual(o["reviewId"], "ar-1700000000000-abcd")
        self.assertEqual(o["mode"], "paper")
        self.assertEqual(o["source"], "ai")
        self.assertEqual(o["status"], "pending")

    def test_open_sizing_and_reason(self):
        """open：目标金额 = capital × kelly.weight，股数向下取整到整手，reason 写全参数。"""
        store = self.make_store()
        cfg = paper_cfg()
        plan = self.plan(store, cfg, [make_row(price=100.0, weight=0.2)], {"600519": 100.0})
        o = plan["orders"][0]
        self.assertEqual(o["intent"], "open")
        self.assertEqual(o["side"], "buy")
        self.assertEqual(o["qty"], 200)                 # 100000×0.2/100 = 200
        self.assertEqual(o["lot"], LOT)
        self.assertEqual(o["amount"], 20000.0)          # 计划名义金额 = qty × 现价
        self.assertAlmostEqual(o["targetWeight"], 0.2, delta=1e-9)
        self.assertEqual(o["limitPrice"], 100.0)
        self.assertEqual(o["signalPrice"], 100.0)
        for token in ("开仓", "凯利权重", "目标金额", "单笔上限", "股数", "200 股", "向下取整"):
            self.assertIn(token, o["reason"], "reason 缺少关键参数：%s" % token)

    def test_open_lot_floor(self):
        """整手向下取整：10000 元 / 33.33 元 = 300.03 股 → 3 手（不是 4 手）。"""
        store = self.make_store()
        cfg = paper_cfg()
        plan = self.plan(store, cfg, [make_row(price=33.33, weight=0.1)], {"600519": 33.33})
        o = plan["orders"][0]
        self.assertEqual(o["qty"], 300)
        self.assertLessEqual(o["qty"] * 33.33, CAP * 0.1 + 1e-9)

    def test_open_capped_by_max_order_amount(self):
        """单笔上限只缩减不跳过；缩减后不足一手才 skip。"""
        store = self.make_store()
        cfg = paper_cfg(maxOrderAmount=5000.0)
        plan = self.plan(store, cfg, [make_row(price=10.0, weight=0.25)], {"600519": 10.0})
        o = plan["orders"][0]
        self.assertEqual(o["qty"], 500)                 # min(25000, 5000) = 5000 → 500 股
        self.assertIn("单笔金额上限", o["reason"])
        self.assertIn("缩减", o["reason"])

        plan2 = self.plan(store, cfg, [make_row(code="000001", price=100.0, weight=0.25)],
                          {"000001": 100.0})
        self.assertEqual(plan2["count"], 0)
        s = plan2["skipped"][0]
        self.assertEqual(s["gate"], "sizing")
        self.assertIn("可用资金不足一手（需 10000.00，可用 5000.00）", s["reason"])

    def test_open_kelly_weight_clamped_by_max_weight(self):
        """凯利权重超过 maxWeight 时按上限夹取（单只上限是硬约束）。"""
        store = self.make_store()
        cfg = paper_cfg(maxWeight=0.25)
        plan = self.plan(store, cfg, [make_row(price=10.0, weight=0.6)], {"600519": 10.0})
        o = plan["orders"][0]
        self.assertAlmostEqual(o["kellyWeight"], 0.25, delta=1e-9)
        self.assertEqual(o["qty"], 2500)                # 100000×0.25/10 = 2500
        self.assertAlmostEqual(o["targetWeight"], 0.25, delta=1e-9)

    def test_open_capped_by_cash(self):
        """可用现金是最后一道上限（账户可能因为已有持仓而现金不足）。"""
        store = self.make_store()
        cfg = paper_cfg()
        state = T.ensure_account(store, cfg, "cn")
        state["cash"] = 3000.0
        store.save_trade_state(state)
        plan = self.plan(store, cfg, [make_row(price=10.0, weight=0.25)], {"600519": 10.0})
        o = plan["orders"][0]
        self.assertEqual(o["qty"], 300)                 # min(25000, 50000, 25000, 3000)
        self.assertIn("可用现金", o["reason"])

    def test_open_insufficient_for_one_lot(self):
        """连一手都买不起时 skip，并写出「需多少 / 可用多少」。"""
        store = self.make_store()
        cfg = paper_cfg()
        state = T.ensure_account(store, cfg, "cn")
        state["cash"] = 500.0
        store.save_trade_state(state)
        plan = self.plan(store, cfg, [make_row(price=10.0, weight=0.25)], {"600519": 10.0})
        self.assertEqual(plan["count"], 0)
        s = plan["skipped"][0]
        self.assertEqual(s["gate"], "sizing")
        self.assertIn("可用资金不足一手（需 1000.00，可用 500.00）", s["reason"])

    def test_open_zero_kelly_weight(self):
        """凯利权重为 0（无优势）→ 不建仓，原因区别于「资金不足」。"""
        store = self.make_store()
        cfg = paper_cfg()
        plan = self.plan(store, cfg, [make_row(price=100.0, weight=0.0)], {"600519": 100.0})
        self.assertEqual(plan["count"], 0)
        self.assertIn("凯利权重为 0", plan["skipped"][0]["reason"])

    def test_add_sizing_and_target_weight(self):
        """add：可加金额 = 目标金额 − 当前持仓市值，再取单笔上限与现金。"""
        store = self.make_store()
        cfg = paper_cfg()
        self.seed(store, cfg, qty=100, price=100.0)     # 持仓市值 10000
        plan = self.plan(store, cfg,
                         [make_row(action="add", price=100.0, weight=0.25)],
                         {"600519": 100.0})
        o = plan["orders"][0]
        self.assertEqual(o["intent"], "add")
        self.assertEqual(o["side"], "buy")
        self.assertEqual(o["qty"], 100)                 # (25000−10000)=15000 → 150 → 1 手
        self.assertAlmostEqual(o["targetWeight"], 0.2, delta=1e-9)   # 200×100/100000
        self.assertIn("加仓", o["reason"])
        self.assertIn("当前持仓市值", o["reason"])

    def test_add_capped_by_max_order_amount(self):
        """加仓同样受单笔金额上限约束。"""
        store = self.make_store()
        cfg = paper_cfg(maxOrderAmount=5000.0)
        self.seed(store, cfg, qty=1000, price=10.0)     # 持仓市值 10000
        plan = self.plan(store, cfg,
                         [make_row(action="add", price=10.0, weight=0.25)],
                         {"600519": 10.0})
        o = plan["orders"][0]
        self.assertEqual(o["qty"], 500)                 # 可加 15000 → 被 5000 压缩 → 500 股

    def test_add_target_already_reached(self):
        """持仓市值已超过目标金额 → skip「已达目标仓位」。"""
        store = self.make_store()
        cfg = paper_cfg()
        self.seed(store, cfg, qty=500, price=100.0)     # 市值 50000 > 目标 25000
        plan = self.plan(store, cfg,
                         [make_row(action="add", price=100.0, weight=0.25)],
                         {"600519": 100.0})
        self.assertEqual(plan["count"], 0)
        self.assertIn("已达目标仓位", plan["skipped"][0]["reason"])

    def test_reduce_half_rounded_down(self):
        """reduce：卖出持仓的一半，向下取整到整手（3 手 → 卖 1 手）。"""
        store = self.make_store()
        cfg = paper_cfg()
        self.seed(store, cfg, qty=300, price=100.0)
        plan = self.plan(store, cfg, [make_row(action="reduce", price=100.0)],
                         {"600519": 100.0})
        o = plan["orders"][0]
        self.assertEqual(o["intent"], "reduce")
        self.assertEqual(o["side"], "sell")
        self.assertEqual(o["qty"], 100)
        self.assertAlmostEqual(o["targetWeight"], 0.2, delta=1e-9)   # 剩 200 股
        self.assertIn("一半", o["reason"])

    def test_reduce_less_than_two_lots_sells_all(self):
        """不足 2 手时全卖（半仓卖不出整手，留着碎仓没有意义）。"""
        store = self.make_store()
        cfg = paper_cfg()
        self.seed(store, cfg, code="600519", qty=100, price=100.0)
        self.seed(store, cfg, code="000001", qty=100, price=10.0)
        plan = self.plan(store, cfg, [make_row(code="000001", action="reduce", price=10.0)],
                         {"000001": 10.0})
        o = plan["orders"][0]
        self.assertEqual(o["qty"], 100)
        self.assertIn("不足 2 手", o["reason"])

    def test_reduce_odd_lot_sells_all(self):
        """脏持仓（碎股）也要能处理：150 股 < 2 手 → 全部卖出。"""
        store = self.make_store()
        cfg = paper_cfg()
        state = T.ensure_account(store, cfg, "cn")
        state["cash"] = 90000.0
        state["positions"] = [{"code": "600519", "name": "600519", "market": "cn",
                               "qty": 150, "avgPrice": 100.0, "cost": 15000.0,
                               "openedAt": now_ms(), "lastPrice": 100.0,
                               "updatedAt": now_ms()}]
        store.save_trade_state(state)
        plan = self.plan(store, cfg, [make_row(action="reduce", price=100.0)],
                         {"600519": 100.0})
        self.assertEqual(plan["orders"][0]["qty"], 150)

    def test_close_sells_everything(self):
        """close：sell / avoid 都清仓，成交后目标权重为 0。"""
        store = self.make_store()
        cfg = paper_cfg()
        self.seed(store, cfg, qty=300, price=100.0)
        for action in ("sell", "avoid"):
            plan = self.plan(store, cfg, [make_row(action=action, price=100.0)],
                             {"600519": 100.0})
            o = plan["orders"][0]
            self.assertEqual(o["intent"], "close", action)
            self.assertEqual(o["side"], "sell")
            self.assertEqual(o["qty"], 300)
            self.assertEqual(o["targetWeight"], 0.0)
            self.assertIn("清仓", o["reason"])

    def test_us_market_lot_is_one(self):
        """美股 1 股/手：同样的资金口径下股数不同（市场决定整手）。"""
        store = self.make_store()
        cfg = paper_cfg(market="us")
        plan = self.plan(store, cfg,
                         [make_row(code="AAPL", market="us", price=150.0, weight=0.1)],
                         {"AAPL": 150.0}, market="us")
        o = plan["orders"][0]
        self.assertEqual(o["market"], "us")
        self.assertEqual(o["lot"], 1)
        self.assertEqual(o["qty"], 66)                  # 10000/150 = 66.67 → 66 股
        self.assertEqual(T.ensure_account(store, cfg, "us")["accountId"], "paper:us")

    def test_quote_overrides_signal_price(self):
        """有报价时 limitPrice 用报价、signalPrice 保留研判价（两者不可混为一谈）。"""
        store = self.make_store()
        cfg = paper_cfg()
        plan = self.plan(store, cfg, [make_row(price=100.0, weight=0.25)],
                         {"600519": 90.0})
        o = plan["orders"][0]
        self.assertEqual(o["limitPrice"], 90.0)
        self.assertEqual(o["signalPrice"], 100.0)
        self.assertEqual(o["qty"], 200)                 # 25000/90 = 277 → 200 股


# --------------------------------------------------------------------------- #
# G. dryrun 的安全属性
# --------------------------------------------------------------------------- #
class TestExecuteDryrun(TraderCase):
    def test_dryrun_never_touches_money(self):
        """dryrun 前后现金 / 持仓 / 累计量 / 权益曲线逐字段不变（本项目第一约束）。"""
        store = self.make_store()
        paper = paper_cfg()
        self.seed(store, paper, qty=200, price=100.0)          # paper 账上有持仓
        dry = T.normalize_config({"mode": "dryrun", "enabled": True})
        acc = T.ensure_account(store, dry, "cn")

        before_state = self.state_json(store, acc["accountId"])
        before_paper = self.state_json(store, "paper:cn")
        before_meta = store.meta_get("trade:meta:dryrun:cn", None)
        before_equity = len(store.list_trade_equity("dryrun:cn"))

        plan = self.plan(store, dry, [make_row(price=100.0, weight=0.25)],
                         {"600519": 100.0}, account=acc)
        self.assertEqual(plan["count"], 1, "dryrun 仍要出计划（否则演练没意义）")
        res = T.execute_orders(store, dry, plan["orders"], {"600519": 100.0})

        self.assertEqual(res["filled"], 0)
        self.assertEqual(res["rejected"], 0)
        self.assertEqual(res["dryrun"], True)
        self.assertIn("dryrun 模式：仅生成计划，不产生成交", res["reason"])
        self.assertEqual(self.state_json(store, acc["accountId"]), before_state,
                         "dryrun 修改了 dryrun 账户状态")
        self.assertEqual(self.state_json(store, "paper:cn"), before_paper,
                         "dryrun 影响了 paper 账户")
        self.assertEqual(store.meta_get("trade:meta:dryrun:cn", None), before_meta,
                         "dryrun 写入了累计量")
        self.assertEqual(len(store.list_trade_equity("dryrun:cn")), before_equity,
                         "dryrun 追加了权益点")
        st = store.get_trade_state(acc["accountId"])
        self.assertEqual(st["cash"], CAP)
        self.assertEqual(st["positions"], [])
        view = res["account"]
        self.assertEqual(view["cash"], CAP)
        self.assertEqual(view["positionCount"], 0)
        self.assertEqual(view["realizedPnl"], 0.0)
        self.assertEqual(view["feeTotal"], 0.0)
        self.assertEqual(res["orders"][0]["status"], "pending")
        self.assertEqual(res["orders"][0]["fillPrice"], None)
        self.assertEqual(res["orders"][0]["fee"], 0.0)
        self.json_ok(res, "execute_orders(dryrun)")

    def test_dryrun_orders_persisted_as_pending(self):
        """dryrun 也要落库（审计链完整），状态保持 pending 且 mode=dryrun。"""
        store = self.make_store()
        cfg = T.normalize_config({"mode": "dryrun", "enabled": True})
        plan = self.plan(store, cfg, [make_row(price=100.0)], {"600519": 100.0})
        res = T.execute_orders(store, cfg, plan["orders"], {"600519": 100.0})
        oid = res["orders"][0]["id"]
        got = store.get_trade_order(oid)
        self.assertIsNotNone(got)
        self.assertEqual(got["status"], "pending")
        self.assertEqual(got["mode"], "dryrun")
        self.assertEqual(store.list_trade_orders(status="pending")["total"], 1)

    def test_dryrun_with_bad_quotes_still_no_money_movement(self):
        """dryrun 连报价都不需要：没有报价也绝不改账户（更不该抛异常）。"""
        store = self.make_store()
        cfg = T.normalize_config({"mode": "dryrun", "enabled": True})
        plan = self.plan(store, cfg, [make_row(price=100.0)], {})
        before = self.state_json(store, "dryrun:cn")
        res = T.execute_orders(store, cfg, plan["orders"], {})
        self.assertEqual(res["filled"], 0)
        self.assertEqual(self.state_json(store, "dryrun:cn"), before)


# --------------------------------------------------------------------------- #
# H. paper 成交：与手工复算逐位一致
# --------------------------------------------------------------------------- #
class TestExecutePaper(TraderCase):
    def test_paper_open_matches_manual_math(self):
        """paper 成交的成本 / 现金 / 持仓 / 均价与手算公式逐位一致（容差 1e-6）。"""
        store = self.make_store()
        cfg = paper_cfg()
        plan = self.plan(store, cfg, [make_row(price=100.0, weight=0.2)], {"600519": 100.0})
        res = T.execute_orders(store, cfg, plan["orders"], {"600519": 100.0})
        self.assertEqual(res["filled"], 1)
        self.assertEqual(res["rejected"], 0)

        qty, price = 200, 100.0
        fo = res["orders"][0]
        self.assertEqual(fo["status"], "filled")
        self.assertEqual(fo["qty"], qty)
        self.assertAlmostEqual(fo["fillPrice"], fill_buy_price(price), delta=1e-6)
        self.assertAlmostEqual(fo["amount"], qty * fill_buy_price(price), delta=1e-6)
        self.assertAlmostEqual(fo["fee"], qty * fill_buy_price(price) * FEE, delta=1e-6)
        self.assertAlmostEqual(fo["slippage"], qty * price * SLIP, delta=1e-6)
        self.assertIsInstance(fo["filledAt"], int)
        self.assertEqual(fo["error"], "")
        for token in ("已成交", "现金余额"):
            self.assertIn(token, fo["reason"])

        view = res["account"]
        self.assertAlmostEqual(view["cash"], CAP - buy_all_in(qty, price), delta=1e-6)
        self.assertEqual(view["initial"], CAP)
        self.assertEqual(view["positionCount"], 1)
        self.assertAlmostEqual(view["marketValue"], qty * price, delta=1e-6)
        self.assertAlmostEqual(view["realizedPnl"], 0.0, delta=1e-9)
        self.assertAlmostEqual(view["feeTotal"], qty * fill_buy_price(price) * FEE, delta=1e-6)
        pos = view["positions"][0]
        self.assertEqual(pos["qty"], qty)
        self.assertAlmostEqual(pos["avgPrice"], buy_all_in(qty, price) / qty, delta=1e-6)
        self.assertAlmostEqual(pos["cost"], buy_all_in(qty, price), delta=1e-6)
        # 权益点：成交后追加一个，且与本函数返回的账户视图一致
        eq = store.list_trade_equity("paper:cn")
        self.assertEqual(len(eq), 1)
        self.assertAlmostEqual(eq[0]["equity"], view["equity"], delta=1e-6)
        self.assertEqual(res["equityAt"], eq[0]["ts"])
        self.json_ok(res, "execute_orders(paper)")

    def test_paper_add_uses_add_average_formula(self):
        """加仓均价 = (老成本 + 新成交金额 + 费用) / 总股数，逐位对齐手算。"""
        store = self.make_store()
        cfg = paper_cfg()
        self.seed(store, cfg, qty=200, price=100.0)
        st = store.get_trade_state("paper:cn")
        avg_old = st["positions"][0]["avgPrice"]
        cash_old = st["cash"]

        res = T.execute_orders(store, cfg,
                               [buy_order(qty=100, price=110.0, intent="add")],
                               {"600519": 110.0})
        self.assertEqual(res["filled"], 1)
        view = res["account"]
        pos = view["positions"][0]
        self.assertEqual(pos["qty"], 300)
        expected_avg = new_avg(200, avg_old, 100, 110.0, FEE)
        self.assertAlmostEqual(pos["avgPrice"], expected_avg, delta=1e-6)
        self.assertAlmostEqual(pos["cost"], expected_avg * 300, delta=1e-6)
        self.assertAlmostEqual(view["cash"], cash_old - buy_all_in(100, 110.0), delta=1e-6)
        self.assertAlmostEqual(view["feeTotal"],
                               200 * fill_buy_price(100.0) * FEE
                               + 100 * fill_buy_price(110.0) * FEE, delta=1e-6)

    def test_paper_reduce_keeps_avg_and_accumulates_realized(self):
        """减仓不动均价，已实现盈亏 = 净收入 − 卖出部分的账面成本。"""
        store = self.make_store()
        cfg = paper_cfg()
        self.seed(store, cfg, qty=300, price=100.0)
        st = store.get_trade_state("paper:cn")
        avg_old = st["positions"][0]["avgPrice"]
        cash_old = st["cash"]

        res = T.execute_orders(store, cfg, [sell_order(qty=100, intent="reduce")],
                               {"600519": 100.0})
        self.assertEqual(res["filled"], 1)
        view = res["account"]
        pos = view["positions"][0]
        self.assertEqual(pos["qty"], 200)
        self.assertAlmostEqual(pos["avgPrice"], avg_old, delta=1e-9)
        self.assertAlmostEqual(pos["cost"], avg_old * 200, delta=1e-6)
        expected_realized = sell_net_of(100, 100.0) - avg_old * 100
        self.assertAlmostEqual(view["realizedPnl"], expected_realized, delta=1e-6)
        self.assertAlmostEqual(res["realizedPnl"], expected_realized, delta=1e-6)
        self.assertAlmostEqual(view["cash"], cash_old + sell_net_of(100, 100.0), delta=1e-6)
        self.assertAlmostEqual(view["feeTotal"],
                               300 * fill_buy_price(100.0) * FEE
                               + 100 * fill_sell_price(100.0) * FEE, delta=1e-6)
        self.assertEqual(len(store.list_trade_equity("paper:cn")), 2)

    def test_paper_close_removes_position_and_books_pnl(self):
        """清仓后持仓消失、权益 = 现金、已实现盈亏累计到累计量里。"""
        store = self.make_store()
        cfg = paper_cfg()
        self.seed(store, cfg, qty=300, price=100.0)
        st = store.get_trade_state("paper:cn")
        avg_old = st["positions"][0]["avgPrice"]

        res = T.execute_orders(store, cfg, [sell_order(qty=300, intent="close")],
                               {"600519": 100.0})
        self.assertEqual(res["filled"], 1)
        view = res["account"]
        self.assertEqual(view["positionCount"], 0)
        self.assertEqual(view["positions"], [])
        self.assertAlmostEqual(view["equity"], view["cash"], delta=1e-6)
        self.assertAlmostEqual(view["realizedPnl"],
                               sell_net_of(300, 100.0) - avg_old * 300, delta=1e-6)
        self.assertAlmostEqual(view["marketValue"], 0.0, delta=1e-9)
        self.assertAlmostEqual(view["returnPct"],
                               (view["equity"] / CAP - 1.0) * 100.0, delta=1e-4)

    def test_paper_recomputes_qty_from_live_quote(self):
        """执行时用最新报价重算股数：价格涨了自动少买，跌了可多买（同计划金额）。"""
        store = self.make_store()
        cfg = paper_cfg()
        plan = self.plan(store, cfg, [make_row(price=100.0, weight=0.25)], {"600519": 100.0})
        o = plan["orders"][0]
        self.assertEqual(o["qty"], 200)

        res_up = T.execute_orders(store, cfg, [dict(o, id=T.order_id())], {"600519": 125.0})
        self.assertEqual(res_up["filled"], 1)
        self.assertEqual(res_up["orders"][0]["qty"], 100)       # 200×100/125 = 160 → 100 股
        self.assertAlmostEqual(res_up["account"]["cash"],
                               CAP - buy_all_in(100, 125.0), delta=1e-6)

        store2 = self.make_store()
        plan2 = self.plan(store2, cfg, [make_row(price=100.0, weight=0.25)], {"600519": 100.0})
        res_down = T.execute_orders(store2, cfg, plan2["orders"], {"600519": 40.0})
        self.assertEqual(res_down["filled"], 1)
        self.assertEqual(res_down["orders"][0]["qty"], 500)     # 200×100/40 = 500 股
        self.assertAlmostEqual(res_down["account"]["cash"],
                               CAP - buy_all_in(500, 40.0), delta=1e-6)

    def test_paper_only_appends_one_equity_point_per_call(self):
        """一次 execute_orders 只追加一个权益点（否则曲线被重复点污染）。"""
        store = self.make_store()
        cfg = paper_cfg()
        plan = self.plan(store, cfg,
                         [make_row(code="600519", price=100.0, weight=0.2),
                          make_row(code="000001", price=10.0, weight=0.2)],
                         {"600519": 100.0, "000001": 10.0})
        self.assertEqual(plan["count"], 2)
        res = T.execute_orders(store, cfg, plan["orders"],
                               {"600519": 100.0, "000001": 10.0})
        self.assertEqual(res["filled"], 2)
        self.assertEqual(len(store.list_trade_equity("paper:cn")), 1)


# --------------------------------------------------------------------------- #
# I. 拒单（绝不部分成交）
# --------------------------------------------------------------------------- #
class TestRejected(TraderCase):
    def test_rejected_no_quote(self):
        """取不到报价 → rejected + error「无有效价格」，账户分毫不动。"""
        store = self.make_store()
        cfg = paper_cfg()
        plan = self.plan(store, cfg, [make_row(price=100.0)], {"600519": 100.0})
        res = T.execute_orders(store, cfg, plan["orders"], {})
        self.assertEqual(res["filled"], 0)
        self.assertEqual(res["rejected"], 1)
        o = res["orders"][0]
        self.assertEqual(o["status"], "rejected")
        self.assertEqual(o["error"], "无有效价格")
        self.assertIn("无有效价格", o["reason"])
        self.assertEqual(res["account"]["cash"], CAP)
        self.assertEqual(res["account"]["positionCount"], 0)
        self.assertEqual(len(store.list_trade_equity("paper:cn")), 0)
        self.assertEqual(store.get_trade_order(o["id"])["status"], "rejected")

    def test_rejected_insufficient_cash(self):
        """现金不足 → rejected（不缩量、不部分成交），持仓仍为空。"""
        store = self.make_store()
        cfg = paper_cfg()
        state = T.ensure_account(store, cfg, "cn")
        state["cash"] = 1000.0
        store.save_trade_state(state)
        res = T.execute_orders(store, cfg, [buy_order(qty=200, price=100.0)],
                               {"600519": 100.0})
        self.assertEqual(res["filled"], 0)
        self.assertEqual(res["rejected"], 1)
        o = res["orders"][0]
        self.assertEqual(o["status"], "rejected")
        self.assertIn("现金不足", o["error"])
        self.assertEqual(o["qty"], 200, "拒单不缩量：qty 仍是计划值，便于事后核对")
        self.assertEqual(res["account"]["cash"], 1000.0)
        self.assertEqual(res["account"]["positionCount"], 0)

    def test_rejected_no_position(self):
        """无持仓的卖单 → rejected「无对应持仓」。"""
        store = self.make_store()
        cfg = paper_cfg()
        res = T.execute_orders(store, cfg, [sell_order(qty=100, intent="close")],
                               {"600519": 100.0})
        self.assertEqual(res["filled"], 0)
        self.assertEqual(res["rejected"], 1)
        self.assertIn("无对应持仓", res["orders"][0]["error"])

    def test_rejected_insufficient_holdings_without_partial_fill(self):
        """卖出量超过持仓 → rejected，且持仓一股不动（不做部分成交）。"""
        store = self.make_store()
        cfg = paper_cfg()
        self.seed(store, cfg, qty=100, price=10.0)
        res = T.execute_orders(store, cfg, [sell_order(qty=500, price=10.0,
                                                      intent="reduce")],
                               {"600519": 10.0})
        self.assertEqual(res["filled"], 0)
        self.assertEqual(res["rejected"], 1)
        self.assertIn("持仓不足：需卖 500 股，当前持有 100 股",
                      res["orders"][0]["error"])
        self.assertEqual(res["account"]["positions"][0]["qty"], 100)

    def test_rejected_zero_qty(self):
        """没有股数的委托单直接拒掉（不许「按金额猜股数」）。"""
        store = self.make_store()
        cfg = paper_cfg()
        res = T.execute_orders(store, cfg, [buy_order(qty=0)], {"600519": 100.0})
        self.assertEqual(res["rejected"], 1)
        self.assertIn("委托数量为 0", res["orders"][0]["error"])


# --------------------------------------------------------------------------- #
# J. 撤单 / 回执 / 导出 / 手动平仓
# --------------------------------------------------------------------------- #
class TestOrderFlows(TraderCase):
    def test_cancel_pending_then_terminal_immutable(self):
        """撤单只对中间状态生效；已成交的单绝不能被改成「已撤销」（审计会崩）。"""
        store = self.make_store()
        cfg = paper_cfg()
        plan = self.plan(store, cfg, [make_row(price=100.0)], {"600519": 100.0})
        oid = store.save_trade_order(plan["orders"][0])
        res = T.cancel_order(store, oid)
        self.assertEqual(res["status"], "cancelled")
        self.assertIn("撤单", res["reason"])
        self.assertEqual(store.get_trade_order(oid)["status"], "cancelled")
        again = T.cancel_order(store, oid, reason="重复撤单")
        self.assertEqual(again["status"], "cancelled")
        self.assertIn("不可撤单", again.get("note", ""))
        self.assertIsNone(T.cancel_order(store, "not-exist"))
        self.assertIsNone(T.cancel_order(store, None))

        filled = T.execute_orders(store, cfg,
                                  [buy_order(code="600519", qty=100, price=100.0)],
                                  {"600519": 100.0})
        fid = filled["orders"][0]["id"]
        self.assertEqual(filled["orders"][0]["status"], "filled")
        keep = T.cancel_order(store, fid)
        self.assertEqual(keep["status"], "filled", "已成交的单不得被撤单改写")
        self.assertEqual(store.get_trade_order(fid)["status"], "filled")
        self.json_ok(res, "cancel_order")

    def test_ack_order(self):
        """外部回执：pending → acked 并写入 extRef；终态单不接受回执。"""
        store = self.make_store()
        cfg = T.normalize_config({"mode": "dryrun", "enabled": True})
        plan = self.plan(store, cfg, [make_row(price=100.0)], {"600519": 100.0})
        oid = store.save_trade_order(plan["orders"][0])

        res = T.ack_order(store, oid, ext_ref="BRIDGE-1")
        self.assertEqual(res["status"], "acked")
        self.assertEqual(res["extRef"], "BRIDGE-1")
        self.assertEqual(store.get_trade_order(oid)["status"], "acked")
        res2 = T.ack_order(store, oid, ext_ref="BRIDGE-2", status="submitted")
        self.assertEqual(res2["status"], "acked", "acked 之后不再接受状态流转")
        self.assertEqual(res2["extRef"], "BRIDGE-1", "未被覆盖的 extRef 保持原值")
        self.assertIsNone(T.ack_order(store, "nope"))

        # 非法 status 回退 acked；submitted → acked 是合法流转
        plan2 = self.plan(store, cfg, [make_row(code="000001", price=10.0)], {"000001": 10.0})
        oid2 = store.save_trade_order(plan2["orders"][0])
        store.update_trade_order(oid2, {"status": "submitted"})
        res3 = T.ack_order(store, oid2, ext_ref="X", status="不是状态")
        self.assertEqual(res3["status"], "acked")
        self.json_ok(res3, "ack_order")

    def test_export_intents_only_pending_with_payload(self):
        """导出只给 pending（待执行意图），且带完整 payload 供外部复核。"""
        store = self.make_store()
        cfg = T.normalize_config({"mode": "dryrun", "enabled": True})
        plan = self.plan(store, cfg,
                         [make_row(code="600519", price=100.0),
                          make_row(code="000001", price=10.0)],
                         {"600519": 100.0, "000001": 10.0})
        for o in plan["orders"]:
            store.save_trade_order(o)
        store.update_trade_order(plan["orders"][1]["id"], {"status": "cancelled"})

        res = T.export_intents(store, limit=50)
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["total"], 1, "total 是「当前过滤条件下的总数」（= pending 笔数）")
        o = res["orders"][0]
        self.assertEqual(o["status"], "pending")
        self.assertEqual(set(o["payload"]), {"plan", "forecast", "gates", "account", "input"})
        self.assertEqual(res["confirmHeader"], T.CONFIRM_HEADER)
        token = T.get_config(store)["confirmToken"]
        self.assertNotIn(token, json.dumps(res, ensure_ascii=False),
                         "导出数据里绝不能出现口令值（口令只由服务端持有）")
        self.assertNotIn("confirmToken", o, "委托单上不该有口令字段")
        self.assertNotIn("confirmToken", o["payload"]["gates"])
        self.assertEqual(T.export_intents(store, limit=1)["count"], 1)
        self.assertEqual(T.export_intents(store, since=now_ms() + 60000)["count"], 0)
        self.json_ok(res, "export_intents")

    def test_close_position_manual_ignores_enabled(self):
        """手动平仓是用户显式意图：不受 enabled 限制，但仍按 paper 报价成交。"""
        store = self.make_store()
        cfg = paper_cfg(enabled=False, mode="paper")
        self.seed(store, cfg, qty=200, price=100.0)
        st = store.get_trade_state("paper:cn")
        cash_old = st["cash"]
        avg_old = st["positions"][0]["avgPrice"]

        res = T.close_position(store, cfg, "600519", {"600519": 100.0})
        self.assertTrue(res["ok"])
        self.assertEqual(res["filled"], 1)
        self.assertEqual(res["qty"], 200)
        o = res["order"]
        self.assertEqual(o["source"], "manual")
        self.assertEqual(o["intent"], "close")
        self.assertEqual(o["action"], "sell")
        self.assertEqual(o["status"], "filled")
        self.assertIn("手动平仓", o["reason"])
        self.assertIn("不受 enabled", o["reason"])
        self.assertEqual(res["account"]["positionCount"], 0)
        self.assertAlmostEqual(res["account"]["cash"], cash_old + sell_net_of(200, 100.0),
                               delta=1e-6)
        self.assertAlmostEqual(res["account"]["realizedPnl"],
                               sell_net_of(200, 100.0) - avg_old * 200, delta=1e-6)
        self.assertEqual(store.get_trade_order(o["id"])["status"], "filled")
        self.json_ok(res, "close_position")

    def test_close_position_partial_qty(self):
        """可以只平一部分（按整手向下取整）；剩余持仓保持均价不变。"""
        store = self.make_store()
        cfg = paper_cfg()
        self.seed(store, cfg, qty=300, price=100.0)
        res = T.close_position(store, cfg, "600519", {"600519": 100.0}, qty=100)
        self.assertTrue(res["ok"])
        self.assertEqual(res["qty"], 100)
        self.assertEqual(res["account"]["positions"][0]["qty"], 200)

    def test_close_position_dryrun_protects_account(self):
        """dryrun 下手动平仓也只出计划：账户状态与累计量一律不动。"""
        store = self.make_store()
        dry = T.normalize_config({"mode": "dryrun", "enabled": False})
        state = T.ensure_account(store, dry, "cn")
        state["positions"] = [{"code": "600519", "name": "600519", "market": "cn",
                               "qty": 100, "avgPrice": 100.0, "cost": 10000.0,
                               "openedAt": now_ms(), "lastPrice": 100.0,
                               "updatedAt": now_ms()}]
        store.save_trade_state(state)
        before = self.state_json(store, "dryrun:cn")

        res = T.close_position(store, dry, "600519", {"600519": 100.0})
        self.assertTrue(res["ok"])
        self.assertEqual(res["filled"], 0)
        self.assertEqual(res["order"]["status"], "pending")
        self.assertEqual(res["order"]["source"], "manual")
        self.assertIn("dryrun", res["note"])
        self.assertEqual(self.state_json(store, "dryrun:cn"), before,
                         "dryrun 的手动平仓不得改账户")

    def test_close_position_error_paths(self):
        """无持仓 / 无报价 / 代码为空 / 数量非法 → ok=False + 中文说明，且不建半截委托。"""
        store = self.make_store()
        cfg = paper_cfg()
        res = T.close_position(store, cfg, "600519", {"600519": 100.0})
        self.assertFalse(res["ok"])
        self.assertIn("无对应持仓", res["note"])
        self.assertIsNone(res["order"])

        self.seed(store, cfg, qty=200, price=100.0)
        res = T.close_position(store, cfg, "600519", {})
        self.assertFalse(res["ok"])
        self.assertIn("无有效价格", res["note"])

        res = T.close_position(store, cfg, "", {"600519": 100.0})
        self.assertFalse(res["ok"])
        self.assertIn("代码为空", res["note"])

        for bad in (0, -5, "x"):
            res = T.close_position(store, cfg, "600519", {"600519": 100.0}, qty=bad)
            self.assertFalse(res["ok"], "qty=%r" % (bad,))
            self.assertIn("非法", res["note"])
        res = T.close_position(store, cfg, "600519", {"600519": 100.0}, qty=50)
        self.assertFalse(res["ok"])
        self.assertIn("不足一手", res["note"])
        # 以上失败路径都不该改账户
        self.assertEqual(store.get_trade_state("paper:cn")["positions"][0]["qty"], 200)
        self.json_ok(res, "close_position(error)")


# --------------------------------------------------------------------------- #
# K. scan：一步到底
# --------------------------------------------------------------------------- #
class FakeRecommend(object):
    """假研判器：记录入参、可注入研报行或异常（签名与 recommend 一致）。"""

    def __init__(self, rows=None, fail=False, payload=None):
        self.calls = []
        self.rows = list(rows or [])
        self.fail = fail
        self.payload = payload

    def __call__(self, symbols, **kw):
        self.calls.append({"symbols": list(symbols), "kwargs": dict(kw)})
        if self.fail:
            raise RuntimeError("行情源不可用")
        if self.payload is not None:
            return self.payload
        return make_advice(self.rows, requested=len(symbols))


class TestScan(TraderCase):
    def test_scan_defaults_no_execute_and_kwargs(self):
        """scan 默认只出计划：构造 symbols、调研判、落库计划，但**不成交**。"""
        store = self.make_store()
        cfg = paper_cfg(universe=["600519", "000001"], whitelist=["600519"])
        fn = FakeRecommend([make_row(price=100.0, weight=0.2)])
        res = T.scan(store, cfg, fn)

        self.assertTrue(res["ok"])
        self.assertEqual(res["symbols"], ["600519", "000001"], "universe 优先于 whitelist 作为标的池")
        self.assertEqual(res["market"], "cn")
        self.assertEqual(fn.calls[0]["symbols"], ["600519", "000001"])
        kw = fn.calls[0]["kwargs"]
        self.assertEqual(kw["market"], "cn")
        self.assertEqual(kw["horizon"], T.SCAN_HORIZON)
        self.assertEqual(kw["capital"], CAP)
        self.assertEqual(kw["kelly_fraction"], T.SCAN_KELLY_FRACTION)
        self.assertEqual(kw["max_weight"], 0.25)
        self.assertFalse(res["executed"], "默认 execute=False：只出计划")
        self.assertEqual(res["filled"], 0)
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["adviceSummary"]["analyzed"], 1)
        self.assertEqual(res["adviceSummary"]["actions"]["buy"], 1)
        self.assertEqual(res["gates"]["enabled"], True)
        self.assertEqual(store.list_trade_orders(status="pending")["total"], 1)
        self.assertEqual(len(store.list_trade_equity("paper:cn")), 0,
                         "未成交不得追加权益点")
        self.assertEqual(res["account"]["cash"], CAP)
        self.json_ok(res, "scan(plan)")

    def test_scan_whitelist_fallback(self):
        """universe 为空时回退 whitelist（服务端只填白名单也能用）。"""
        store = self.make_store()
        cfg = paper_cfg(whitelist=["600858"])
        fn = FakeRecommend([make_row(code="600858", price=50.0)])
        res = T.scan(store, cfg, fn)
        self.assertEqual(res["symbols"], ["600858"])
        self.assertEqual(fn.calls[0]["symbols"], ["600858"])

    def test_scan_execute_paper_fills(self):
        """execute=True 且 paper：落库 → 成交 → 追加权益点 → 返回账户。"""
        store = self.make_store()
        cfg = paper_cfg(universe=["600519"])
        fn = FakeRecommend([make_row(price=100.0, weight=0.2)])
        res = T.scan(store, cfg, fn, execute=True)
        self.assertTrue(res["ok"])
        self.assertTrue(res["executed"])
        self.assertEqual(res["filled"], 1)
        self.assertEqual(res["orders"][0]["status"], "filled")
        self.assertAlmostEqual(res["account"]["cash"], CAP - buy_all_in(200, 100.0),
                               delta=1e-6)
        self.assertEqual(len(store.list_trade_equity("paper:cn")), 1)
        self.assertEqual(res["equityAt"], store.list_trade_equity("paper:cn")[0]["ts"])
        self.json_ok(res, "scan(execute)")

    def test_scan_execute_dryrun_keeps_money(self):
        """execute=True 但 mode=dryrun：只出计划，账户分文不动。"""
        store = self.make_store()
        cfg = T.normalize_config({"enabled": True, "mode": "dryrun",
                                  "universe": ["600519"]})
        fn = FakeRecommend([make_row(price=100.0, weight=0.25)])
        res = T.scan(store, cfg, fn, execute=True)
        self.assertEqual(res["filled"], 0)
        self.assertEqual(res["orders"][0]["status"], "pending")
        st = store.get_trade_state("dryrun:cn")
        self.assertEqual(st["cash"], CAP)
        self.assertEqual(st["positions"], [])
        self.assertEqual(len(store.list_trade_equity("dryrun:cn")), 0)

    def test_scan_empty_pool_and_failures(self):
        """标的池为空 / 研判抛异常 / 研判返回垃圾 → ok=False 或空计划，但绝不抛异常。"""
        store = self.make_store()
        cfg = paper_cfg()
        fn = FakeRecommend([make_row()])
        res = T.scan(store, cfg, fn)
        self.assertFalse(res["ok"])
        self.assertIn("标的池为空", res["error"])
        self.assertEqual(fn.calls, [], "没有标的就不该调研判")

        cfg2 = paper_cfg(universe=["600519"])
        bad = FakeRecommend(fail=True)
        res2 = T.scan(store, cfg2, bad)
        self.assertFalse(res2["ok"])
        self.assertIn("研判失败", res2["error"])
        self.json_ok(res2, "scan(failed)")

        for payload in (None, "x", 3, {"rows": "bad"}):
            res3 = T.scan(store, cfg2, FakeRecommend(payload=payload))
            self.assertTrue(res3["ok"])
            self.assertEqual(res3["count"], 0)
            self.json_ok(res3, "scan(garbage advice)")

        res4 = T.scan(store, cfg2, None)
        self.assertFalse(res4["ok"])
        self.assertIn("recommend_fn", res4["error"])

    def test_scan_market_override_and_partial(self):
        """market 参数可覆盖配置；研判函数用 partial 预绑参数时也不能炸。"""
        store = self.make_store()
        cfg = paper_cfg(universe=["AAPL"], market="cn")
        fn = FakeRecommend([make_row(code="AAPL", market="us", price=150.0, weight=0.1)])
        res = T.scan(store, cfg, fn, market="us")
        self.assertEqual(res["market"], "us")
        self.assertEqual(fn.calls[0]["kwargs"]["market"], "us")

        def rec(symbols, fetch_bars=None, market="cn", horizon=99, capital=0.0,
                kelly_fraction=0.5, max_weight=0.1):
            rec.seen = {"symbols": list(symbols), "horizon": horizon, "market": market}
            return make_advice([make_row(price=100.0, weight=0.1)])

        bound = functools.partial(rec, fetch_bars=lambda *a: [], horizon=10)
        res2 = T.scan(store, cfg, bound)
        self.assertTrue(res2["ok"], res2.get("error"))
        self.assertEqual(rec.seen["horizon"], 10, "partial 预绑的 horizon 必须保留")
        self.assertEqual(rec.seen["market"], "cn")


# --------------------------------------------------------------------------- #
# L. 快照与 JSON 安全
# --------------------------------------------------------------------------- #
class TestSnapshotJson(TraderCase):
    def test_trade_snapshot_structure(self):
        """总览含 config / account / gates / counts；counts 的 byStatus 覆盖全部状态。"""
        store = self.make_store()
        cfg = paper_cfg()
        self.seed(store, cfg, qty=200, price=100.0)
        snap = T.trade_snapshot(store, cfg, {"600519": 120.0})
        self.assertTrue({"config", "account", "gates", "counts"}.issubset(snap))
        self.assertEqual(snap["config"]["mode"], "paper")
        self.assertEqual(snap["account"]["accountId"], "paper:cn")
        self.assertEqual(snap["account"]["positionCount"], 1)
        self.assertEqual(snap["gates"]["positionsHeld"], 1)
        self.assertEqual(snap["gates"]["ordersToday"], 1)
        self.assertEqual(set(snap["counts"]["byStatus"]), set(T.ORDER_STATUS))
        self.assertEqual(snap["counts"]["byStatus"]["filled"], 1)
        self.assertGreaterEqual(snap["counts"]["trade_orders"], 1)
        self.assertNotIn("confirmToken", snap["gates"], "口令不进风控快照")
        self.assertIn("confirmToken", snap["config"], "配置里必须有口令（服务端校验用）")
        self.json_ok(snap, "trade_snapshot")

    def test_every_public_return_is_json_safe(self):
        """所有公开返回值都要能 json.dumps(allow_nan=False)：NaN 会让前端直接崩。"""
        store = self.make_store()
        cfg = paper_cfg(universe=["600519"])
        acc = T.ensure_account(store, cfg, "cn")
        plan = self.plan(store, cfg, [make_row(price=100.0, weight=0.2)],
                         {"600519": 100.0}, account=acc)
        self.json_ok(plan, "plan_orders")
        exe = T.execute_orders(store, cfg, plan["orders"], {"600519": 100.0})
        self.json_ok(exe, "execute_orders")
        self.json_ok(T.account_view(store, cfg, {"600519": 100.0}), "account_view")
        self.json_ok(T.account_view(store, cfg, None), "account_view(no quotes)")
        self.json_ok(T.trade_snapshot(store, cfg), "trade_snapshot")
        self.json_ok(T.export_intents(store), "export_intents")
        self.json_ok(T.scan(store, cfg, FakeRecommend([make_row(price=100.0)])), "scan")
        self.json_ok(T.close_position(store, cfg, "600519", {}), "close_position")
        self.json_ok(T.reset_account(store, cfg, "cn"), "reset_account")
        self.json_ok(T.cancel_order(store, "x"), "cancel_order(none)")


# --------------------------------------------------------------------------- #
# M. 脏输入
# --------------------------------------------------------------------------- #
class TestDirtyInput(TraderCase):
    def test_plan_orders_never_raises(self):
        """advice / account / config / quotes 全是垃圾时也不能抛异常。"""
        store = self.make_store()
        cfg = paper_cfg()
        bad_advices = [None, [], "x", 3, {"rows": None}, {"rows": "bad"},
                       {"rows": [None, 1, "a", {}]},
                       {"rows": [make_row(code=""), make_row(code="600519", ok=None)]}]
        bad_accounts = [None, [], "x", {"positions": None},
                        {"positions": [None, 1, {"code": ""}, {"code": "600519", "qty": "x"}]},
                        {"cash": "abc", "positions": [{"code": "600519", "qty": 100}]}]
        bad_quotes = [None, {}, "x", 3, {"600519": "abc"}, {"600519": {}},
                      {"600519": None}, {"cn:600519": {"price": "100"}}]
        for advice in bad_advices:
            for account in bad_accounts:
                for quotes in bad_quotes:
                    plan = T.plan_orders(advice, account, cfg, quotes, store)
                    self.assertTrue(plan["ok"])
                    self.json_ok(plan, "plan_orders(dirty)")
        # 配置本身是垃圾
        for bad_cfg in (None, "x", 3, {"mode": {"a": 1}}, {"universe": {"x": 1}}):
            plan = T.plan_orders(make_advice([make_row(price=100.0)]), None, bad_cfg,
                                 {"600519": 100.0}, store)
            self.json_ok(plan, "plan_orders(dirty cfg)")
        # store 缺失（只有 dryrun 计划不需要库）
        plan = T.plan_orders(make_advice([make_row(price=100.0)]), None, cfg,
                             {"600519": 100.0}, None)
        self.assertEqual(plan["count"], 1, "没有 store 也要能出计划（只是不落库）")
        self.assertEqual(plan["gates"]["ordersToday"], 0)

    def test_empty_code_row_is_skipped_not_ordered(self):
        """代码为空的行必须进 skipped（空代码的委托单是不可执行的垃圾数据）。"""
        store = self.make_store()
        cfg = paper_cfg()
        plan = self.plan(store, cfg, [make_row(code="", price=100.0)], {"": 100.0})
        self.assertEqual(plan["count"], 0)
        self.assertEqual(plan["skipped"][0]["gate"], "data")
        self.assertIn("标的代码为空", plan["skipped"][0]["reason"])

    def test_execute_orders_never_raises(self):
        """orders / quotes 是垃圾时也不能抛异常，且每条 dict 订单都要有状态。"""
        store = self.make_store()
        cfg = paper_cfg()
        bad_orders = [None, [], "x", 3, [None, 1, "a", {}],
                      [{"code": "", "side": "buy", "qty": 100}],
                      [{"code": "600519", "side": "?", "qty": 100, "limitPrice": 100}],
                      [{"code": "600519", "side": "sell", "intent": "reduce", "qty": 100}],
                      [{"code": "600519", "side": "buy", "qty": "x"}]]
        bad_quotes = [None, {}, "x", [], {"600519": "abc"}, {"600519": {"last": 100}}]
        for orders in bad_orders:
            for quotes in bad_quotes:
                res = T.execute_orders(store, cfg, orders, quotes)
                self.assertTrue(res["ok"])
                self.assertEqual(res["filled"], 0)
                self.json_ok(res, "execute_orders(dirty)")

    def test_store_missing_for_readers(self):
        """只读接口在没有 store 时返回可用的降级结果（服务端启动早期会走到）。"""
        cfg = paper_cfg()
        self.assertEqual(T.account_view(None, cfg)["cash"], CAP)
        self.assertEqual(T.ensure_account(None, cfg, "cn")["cash"], CAP)
        self.assertTrue(T.reset_account(None, cfg, "cn")["reset"])
        snap = T.trade_snapshot(None, cfg)
        self.assertEqual(snap["counts"]["byStatus"]["pending"], 0)
        self.json_ok(snap, "trade_snapshot(None)")
        self.assertEqual(T.export_intents(None)["count"], 0)

    def test_quotes_shapes(self):
        """报价的四种真实入参形态都要能取到价格（否则线上会「莫名其妙没报价」）。"""
        store = self.make_store()
        cfg = paper_cfg()
        for quotes in ({"600519": 100.0}, {"600519": {"price": 100.0}},
                       {"cn:600519": 100.0}, [{"code": "600519", "price": 100.0}],
                       {"600519": "100.0"}):
            plan = self.plan(store, cfg, [make_row(price=100.0, weight=0.2)], quotes)
            self.assertEqual(plan["count"], 1, "quotes=%r" % (quotes,))
            self.assertEqual(plan["orders"][0]["limitPrice"], 100.0)

    def test_position_without_quote_uses_cost(self):
        """账户有持仓但没报价：视图按成本估值并标 priced=false（不臆造）。"""
        store = self.make_store()
        cfg = paper_cfg()
        self.seed(store, cfg, qty=100, price=50.0)
        view = T.account_view(store, cfg, {"其他代码": 1.0})
        self.assertFalse(view["priced"])
        self.assertEqual(view["positions"][0]["priced"], False)
        self.assertAlmostEqual(view["marketValue"], 100 * view["positions"][0]["avgPrice"],
                               delta=1e-6)


# --------------------------------------------------------------------------- #
# N. 持久化往返
# --------------------------------------------------------------------------- #
class TestPersistence(TraderCase):
    def test_roundtrip_new_store_instance(self):
        """换 Store 实例读回：账户 / 累计量 / 委托单 / 配置全部一致（重启不丢状态）。"""
        store, path = self.file_store()
        cfg = T.save_config(store, {"enabled": True, "mode": "paper",
                                    "universe": ["600519"], "capital": CAP})
        self.seed(store, cfg, qty=300, price=100.0)
        T.execute_orders(store, cfg, [sell_order(qty=100, intent="reduce")],
                         {"600519": 100.0})
        state_before = store.get_trade_state("paper:cn")
        meta_before = store.meta_get("trade:meta:paper:cn", None)
        orders_before = store.list_trade_orders(limit=100)
        view_before = T.account_view(store, cfg, {"600519": 100.0})
        store.close()

        again = Store(path)
        self.addCleanup(again.close)
        state_after = again.get_trade_state("paper:cn")
        self.assertEqual(state_after["cash"], state_before["cash"])
        self.assertEqual(state_after["positions"], state_before["positions"])
        self.assertEqual(state_after["mode"], "paper")
        self.assertEqual(state_after["lot"], LOT)
        self.assertEqual(again.meta_get("trade:meta:paper:cn", None), meta_before)
        orders_after = again.list_trade_orders(limit=100)
        self.assertEqual(orders_after["total"], orders_before["total"])
        self.assertEqual([o["id"] for o in orders_after["rows"]],
                         [o["id"] for o in orders_before["rows"]])
        self.assertEqual(orders_after["rows"][0]["status"], "filled")
        view_after = T.account_view(again, T.get_config(again), {"600519": 100.0})
        self.assertAlmostEqual(view_after["cash"], view_before["cash"], delta=1e-9)
        self.assertAlmostEqual(view_after["realizedPnl"], view_before["realizedPnl"],
                               delta=1e-9)
        self.assertAlmostEqual(view_after["feeTotal"], view_before["feeTotal"], delta=1e-9)
        self.assertEqual(T.get_config(again)["universe"], ["600519"])
        self.assertEqual(T.get_config(again)["confirmToken"], cfg["confirmToken"])

    def test_pending_order_roundtrip_keeps_payload(self):
        """计划单往返后 payload 仍在（外部桥接系统靠它复核计划）。"""
        store, path = self.file_store()
        cfg = T.normalize_config({"mode": "dryrun", "enabled": True})
        plan = self.plan(store, cfg, [make_row(price=100.0)], {"600519": 100.0})
        store.save_trade_order(plan["orders"][0])
        store.close()
        again = Store(path)
        self.addCleanup(again.close)
        got = again.get_trade_order(plan["orders"][0]["id"])
        self.assertEqual(got["status"], "pending")
        self.assertEqual(got["qty"], plan["orders"][0]["qty"])
        self.assertEqual(got["reason"], plan["orders"][0]["reason"])
        self.assertEqual(got["payload"]["input"]["capital"], CAP)
        self.assertEqual(got["payload"]["plan"], plan["orders"][0]["payload"]["plan"])


# --------------------------------------------------------------------------- #
# O. 与 core/advisor.py 的契约联调（真研判输出 → 计划 → 成交）
# --------------------------------------------------------------------------- #
def synth_bars(n=160, base=50.0, start=(2024, 1, 2)):
    """确定性合成日线（无网络、无随机）：稳定上行 + 轻微正弦波动。"""
    out, d, px = [], date(*start), float(base)
    for i in range(n):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        px *= 1.0 + 0.002 + 0.001 * math.sin(i / 7.0)
        out.append({"t": d.isoformat(), "open": px * 0.999, "high": px * 1.01,
                    "low": px * 0.99, "close": px, "volume": 1000.0})
        d += timedelta(days=1)
    return out


class TestAdvisorIntegration(TraderCase):
    """不测 advisor 的算法，只用它的**真实返回结构**当夹具，验证两层契约咬得合。"""

    def make_recommend(self, bars):
        def recommend_fn(symbols, market="cn", horizon=20, capital=100000.0,
                         kelly_fraction=0.5, max_weight=0.25):
            return A.recommend(symbols, lambda m, c, p, l: list(bars),
                               lambda m, c: {"price": bars[-1]["close"]}, market=market,
                               horizon=horizon, capital=capital,
                               kelly_fraction=kelly_fraction, max_weight=max_weight)
        return recommend_fn

    def test_real_recommend_flows_into_plan(self):
        """真研判输出喂进 plan_orders：不静默丢弃、原因齐全、可 JSON 化。"""
        store = self.make_store()
        bars = synth_bars()
        cfg = paper_cfg(universe=["600519"], minConfidence=0.1)
        advice = A.recommend(["600519"], lambda m, c, p, l: list(bars),
                             lambda m, c: {"price": bars[-1]["close"]})
        rows = advice["rows"]
        self.assertTrue(rows and rows[0]["ok"], "合成数据必须能研判出结论（否则夹具失效）")
        plan = T.plan_orders(advice, T.ensure_account(store, cfg, "cn"), cfg,
                             {"600519": bars[-1]["close"]}, store)
        self.assertEqual(plan["count"] + plan["skippedCount"] + plan["neutral"], len(rows))
        for o in plan["orders"]:
            self.assertIn(o["code"], [r["code"] for r in rows])
            self.assertIsInstance(o["reviewId"], str,
                                  "reviewId 必须是字符串（真研判未必带记录 id）")
            self.assertAlmostEqual(o["limitPrice"], round(bars[-1]["close"], 6), delta=1e-6)
            self.assertEqual(o["payload"]["input"]["capital"], CAP)
        for s in plan["skipped"]:
            self.assertTrue(s["reason"])
        self.json_ok(plan, "plan_orders(real advice)")

    def test_real_recommend_scan_dryrun_keeps_money(self):
        """端到端 scan + execute：dryrun 下账户分文不动（真研判链路的安全属性）。"""
        store = self.make_store()
        bars = synth_bars()
        cfg = T.normalize_config({"enabled": True, "mode": "dryrun",
                                  "universe": ["600519"], "minConfidence": 0.1})
        res = T.scan(store, cfg, self.make_recommend(bars), execute=True)
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(res["filled"], 0)
        st = store.get_trade_state("dryrun:cn")
        self.assertEqual(st["cash"], CAP)
        self.assertEqual(st["positions"], [])
        self.assertEqual(len(store.list_trade_equity("dryrun:cn")), 0)
        self.assertEqual(res["account"]["equity"], CAP)
        self.json_ok(res, "scan(real advice)")

    def test_real_recommend_scan_paper_then_add(self):
        """端到端 scan + execute（paper）：第一轮建仓、第二轮按同一口径继续处理。"""
        store = self.make_store()
        bars = synth_bars()
        cfg = paper_cfg(universe=["600519"], minConfidence=0.1)
        fn = self.make_recommend(bars)
        first = T.scan(store, cfg, fn, execute=True)
        self.assertTrue(first["ok"], first.get("error"))
        second = T.scan(store, cfg, fn)
        self.assertTrue(second["ok"], second.get("error"))
        # 两轮都要给出可审计的委托单（状态合法 + 原因齐全）且不超标的池规模
        for res in (first, second):
            self.assertLessEqual(res["count"], len(res["symbols"]))
            for o in res["orders"]:
                self.assertIn(o["status"], T.ORDER_STATUS)
                self.assertTrue(o["reason"])
            self.json_ok(res, "scan(paper)")
        view = T.account_view(store, cfg, {"600519": bars[-1]["close"]})
        self.assertLessEqual(view["marketValue"] / CAP, 0.25 + 1e-6,
                             "单只权重不得超过 maxWeight（含整手取整误差）")
        self.assertGreaterEqual(view["cash"], 0.0, "模拟账户不允许透支")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
