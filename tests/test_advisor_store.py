#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 选股记录的「持久化 + 事后复盘」单元测试（core.storage v3 两表 + core.advisor.to_record / review）。

覆盖范围（与需求 19 条断言一一对应）
------------------------------------
A. Store 层
  1. TestAdvisorSchema         建库自检：schema v3、advisor_runs / advisor_items 已建并可读；
  2. 同上                      v2 → v3 迁移：返回 {from:2,to:3,applied:[3]}、老数据完好、可重复调用；
  3. TestAdvisorWrite          写入计数、同一 id 幂等重写、pinned 不被复位、note 不被 NULL 覆盖；
  4. 同上                      get_advisor_run / advisor_items 原样还原逐只结论
                               （含 kelly / forecast / plan / advisor.marks）、顺序 = 写入顺序、
                               不存在返回 None；
  5. TestAdvisorList           置顶优先 + 时间倒序、market / code / action / q 过滤、pinned=True、
                               limit / offset 与 total 自洽；
  6. TestAdvisorUpdateDelete   只改 note 不动 pinned（反之亦然）、不存在返回 None、
                               删除级联清空 advisor_items、删不存在返回 False；
  7. TestAdvisorPrune          只清最旧的非置顶、置顶永不清理、keep=0 清空、已达标返回 0、
                               累计清理数进 advisor_stats()["prunedTotal"]；
  8. TestAdvisorStats          records / items / buyTotal（只数 buy+add）/ avgTotalWeight /
                               latestAt / keep / prunedTotal（含空库口径）。
B. to_record（纯函数）
  9. TestToRecordId            记录 id 形态 ar-<13位毫秒>-<4位十六进制>、createdDate 对应 ts；
 10. TestToRecordSummary       七档计数之和 == 行数、buyCount == actionableCount == buy+add；
 11. 同上                      summary.topRows 最多 3 条且可操作性优先（buy/add 在 hold/watch 之前，
                               同档位按评分降序）；
 12. TestToRecordTrim          advisor.forecast.path 被裁空并带 trimmed=True，
                               但 advisor.marks / advisor.plan 保留（预测带锚在保存价上，事后无意义）；
 13. 同上                      run.payload 不含 rows（明细走 advisor_items，避免重复存储）、
                               summary.codes 覆盖全部行。
C. review（复盘）
 14. TestReviewReturns         sinceReturn / fwd[k].ret 与手工复算逐位一致（容差 1e-6），
                               含「参考价优先取记录里保存的价位」这条口径；
 15. TestReviewVerdict         hit / miss / neutral / pending / nodata 五类判定口径（含持平算 miss）；
 16. TestReviewBaseDate        基准日 = 「最后一个日期不晚于保存日」的K线（保存日可能是周末）；
 17. TestReviewSummary         命中率、看多/看空分组命中率、平均收益（百分数）、
                               positionReturn == accountReturn / totalWeight、
                               accountReturn == Σ(weight × sinceReturn)、totalWeight 只累计 weight>0；
 18. TestReviewVerdict/…       每行 fwd 永远含全部 horizon 键（nodata 行也是 {ret:null,ready:false}）；
 19. TestReviewRobustness      非法 horizons 回退默认 (5,20)、空记录 ok=False、
                               fetch_bars 抛异常/返回空不抛异常且判定 nodata、
                               多线程与单线程结果一致、结果可 json.dumps(allow_nan=False)。
 20. TestKnownIssues           实测到的两处不一致（@unittest.expectedFailure，说明见类 docstring）。

为什么这样造数据（可复现性优先）
--------------------------------
· 全部数据**确定性构造**：K 线由「连续交易日日期 + 显式收盘价」拼出，不用 random、不联网；
· 收益取**整百分比**（100 → 105 即 +5.0%），浮点比较因此可以收到 1e-6，不会变成对四舍五入的赌博；
· 记录一律经 `core.advisor.to_record()` 生成，再按两种真实入参形态喂给 review：
  `merged_record()`（= to_record 的 run 字段 + rows 摊平，与 get_advisor_run 同形）与
  `Store.get_advisor_run()`（端到端那条用例），而不是手写一个「恰好能被实现读懂的 dict」；
· 时间戳用 `ts_of(年,月,日)` 从本地日期反推，避免把毫秒值写死后随环境时区漂移；
· 每条断言的 docstring 说明「为什么断言这件事」，而不是复述代码。

与 tests/test_advisor.py 的分工
-------------------------------
test_advisor.py 锁定 recommend 的算法口径（分位键、凯利上界、组合分配、档位方向…）；
本文件**不重复**那些断言，只覆盖新增的持久化（advisor_runs / advisor_items）与事后复盘
（record_id / to_record / review）——这正是本次新增能力的边界。

运行方式::
    python3 tests/test_advisor_store.py
    python3 -m unittest discover -s tests -p "test_*.py"
"""

import json
import os
import re
import sys
import time
import unittest
from datetime import date, timedelta
from unittest import mock

# 让测试既能在 stock-terminal/ 下跑，也能在仓库根目录下跑
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import advisor as A                                   # noqa: E402
from core.storage import MIGRATIONS, SCHEMA_VERSION, Store      # noqa: E402

# --------------------------------------------------------------------------- #
# 常量：把「被测代码的契约」集中在这里，避免散落在断言里
# --------------------------------------------------------------------------- #
#: 本文件锁定**当前** schema 版本，不写死数字。
#: 这是实测踩过的坑：v4 加了自动交易四张表后，这里写死的 3 立刻让两个迁移用例变红，
#: 而失败原因与被测的选股记录逻辑毫无关系 —— 断言应该跟着 SCHEMA_VERSION 走。
EXPECTED_SCHEMA_VERSION = SCHEMA_VERSION
#: 基准K线在合成序列中的下标（=> 保存日 = 该根的日期）
BASE_INDEX = 10
#: 基准K线之后的根数（25 > 20：让 fwd[20] 与 sinceReturn 各自可控、互不遮挡）
TAIL = 25
#: 合成序列的起点（周一），工作日由 business_days 顺推
START = date(2026, 3, 2)
#: 默认复盘窗口（与 core.advisor.REVIEW_HORIZONS 同源，直接读被测常量）
HZ = A.REVIEW_HORIZONS
#: 默认 horizon 的字符串键（review 返回的 fwd 用字符串键）
HZ_KEYS = {str(int(k)) for k in HZ}
#: 未到期 / 无数据的 fwd 结构（nodata 行也必须是这个形状）
EMPTY_FWD = {"ret": None, "hit": None, "ready": False, "date": None, "price": None}


def ts_of(y, m, d, hour=15):
    """本地日期 → 毫秒时间戳（用 mktime 反推，避免时区导致 createdDate 漂移）。"""
    return int(time.mktime((y, m, d, hour, 0, 0, 0, 0, -1)) * 1000)


#: 三条记录的时间戳（本地 15:00）：TS1 与合成序列的基准K线同一天
TS1 = ts_of(2026, 3, 16)
TS2 = ts_of(2026, 3, 17)
TS3 = ts_of(2026, 3, 18)
#: 另一个基准日（用于 createdDate 与本地日期的对应关系）
TS_LATER = ts_of(2026, 4, 6, 12)


# --------------------------------------------------------------------------- #
# 合成数据与脚手架
# --------------------------------------------------------------------------- #
def business_days(start, n):
    """从 start 起顺推 n 个工作日（跳过周六周日）。

    周末用来验证「基准日回退到上一个交易日」，所以日历必须是真日历，不能自己编。
    """
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def bars_of(dates, closes):
    """显式日期 + 显式收盘价的日线序列（open/high/low 与收盘价同值，便于手算）。"""
    return [{"t": d.isoformat(), "open": float(c), "high": float(c), "low": float(c),
             "close": float(c), "volume": 1000.0}
            for d, c in zip(dates, closes)]


def series(base_close=100.0, changes=None, head=BASE_INDEX, tail=TAIL, start=START):
    """构造一段确定性日线：head+1 根横盘（第 head 根是基准K线）+ tail 根。

    ``changes`` 用「相对基准K线的偏移 → 收盘价」覆盖收盘价，例如
    ``{5: 105.0, 25: 103.0}`` 表示「第 5 根 +5%、第 25 根（末根）+3%」。
    返回 ``(bars, dates)``，两者一一对应，测试里的手算公式可以直接对齐实现里的公式。
    """
    closes = [float(base_close)] * (head + 1 + tail)
    for off, px in (changes or {}).items():
        closes[head + int(off)] = float(px)
    dates = business_days(start, len(closes))
    return bars_of(dates, closes), dates


def pct(a, b):
    """(b/a - 1) 的百分数保留 3 位——与 core.advisor._fwd_return 完全相同的取整链。"""
    return round((float(b) / float(a) - 1.0) * 100.0, 3)


class FakeBars(object):
    """假行情源：按 (market, code) 返回预置K线，并记录每次调用参数。

    签名与 recommend / review 期望的 ``fetch_bars(market, code, period, limit)`` 一致。
    ``fail`` 里的代码一律抛异常，用来验证「单只取数失败只影响它自己那一行」。
    """

    def __init__(self, book, fail=()):
        self.book = dict(book)
        self.fail = set(fail)
        self.calls = []

    def __call__(self, market, code, period, limit):
        self.calls.append({"market": market, "code": code,
                           "period": period, "limit": limit})
        if code in self.fail:
            raise RuntimeError("行情源不可用")
        return list(self.book.get((market, code)) or [])


def make_row(code, action="buy", price=100.0, weight=0.25, score=80.0, market="cn", **over):
    """构造一行 recommend 响应（字段与 web/js/views/advisor.js 的契约一致）。

    刻意保留 advisor.marks / advisor.forecast.path / advisor.plan 三个子结构：
    to_record 的裁剪行为（只裁预测带路径）正是靠它们之间的差异来验证的。
    """
    row = {
        "ok": True, "code": code, "name": code, "market": market,
        "price": price, "changePct": 1.0, "asOf": "2026-03-16", "bars": 37,
        "action": action, "actionText": A.ACTION_LABEL.get(action or "", "数据不足"),
        "score": score, "confidence": 0.6,
        "signals": [{"key": "ma", "label": "均线", "dir": "up", "brief": "MA5 在 MA20 上方"}],
        "ensemble": {"net": 3, "votes": {"maCross": 1, "macd": 1, "rsi": 1}},
        "edge": {"trades": 8, "winRate": 0.5, "payoff": 1.4},
        "kelly": {"fStar": 0.3, "kind": "discrete", "weight": weight,
                  "amount": 1000.0, "shares": 10, "lot": 100},
        "forecast": {"expectedReturn": 3.2, "upProb": 0.6, "sample": 40,
                     "quantiles": {"5": -3.0, "50": 1.2, "95": 8.0}},
        "plan": {"entry": price, "stop": 92.0, "target1": 112.0, "target2": 121.0,
                 "riskReward": 1.5},
        "risk": {"atrPct": 2.1, "maxDrawdown": 12.5},
        "advisor": {
            "marks": [{"t": "2026-03-13", "idx": 9, "dir": "buy", "kind": "trigger",
                       "strategy": "maCross", "label": "双均线交叉买"}],
            "forecast": {"path": [{"i": 0, "t": "2026-03-17", "mid": 101.0,
                                   "lo": 97.0, "hi": 105.0}],
                         "horizon": 20, "levels": {"p50": 1.2}},
            "plan": {"entry": price, "stop": 92.0, "target1": 112.0, "target2": 121.0},
        },
        "note": "测试行",
    }
    row.update(over)
    return row


def make_res(rows, **over):
    """构造一个与 recommend() 响应同形的结果（只保留 to_record 用到的键）。"""
    res = {
        "ok": True, "market": "cn", "horizon": 20, "capital": 100000.0,
        "kellyFraction": 0.5, "maxWeight": 0.25, "cashBuffer": 0.1,
        "requested": len(rows), "count": len(rows),
        "analyzed": len([r for r in rows if isinstance(r, dict) and r.get("action")]),
        "portfolio": {"totalWeight": 0.5, "cash": 50000.0},
        "disclaimer": "测试用免责声明", "source": "test-source", "updated": TS1,
        "rows": rows,
    }
    res.update(over)
    return res


def make_record(rows, ts=TS1, rid=None, **over):
    """``to_record`` 的薄封装：用例只关心「有多少行、什么档位」，不必重复构造响应壳。

    返回的是 to_record 的原始结果 ``{"run": {...}, "rows": [...]}``（落库用这个形状）。
    """
    trigger = over.pop("trigger", "list")
    note = over.pop("note", "")
    return A.to_record(make_res(rows, **over), trigger=trigger, note=note, rid=rid, ts=ts)


def merged_record(rows, ts=TS1, rid=None, **over):
    """把 to_record 的结果摊平成 review 期望的「合并体」（run 字段 + rows 同层）。

    review 的入参是 ``Store.get_advisor_run`` 的返回值，它把 run 的检索字段与 rows
    平铺在同一层；这里复刻那个形状，用来覆盖「合并体」这条入参路径
    （端到端那条用例另行用真的 get_advisor_run 走一遍真实链路）。
    """
    rec = make_record(rows, ts=ts, rid=rid, **over)
    out = dict(rec["run"])
    out["rows"] = rec["rows"]
    return out


class StoreCase(unittest.TestCase):
    """公共脚手架：每个用例一个独立内存库。

    为什么用 ``:memory:``：Store 的连接是 thread-local 的，而本文件的 Store 用例
    全部单线程执行，所以内存库不会被「另一个线程看不到表」的问题影响；
    同时省掉临时文件清理，失败时也不会在工作区留垃圾。
    """

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)


# --------------------------------------------------------------------------- #
# 1~2. 建库自检与结构迁移
# --------------------------------------------------------------------------- #
class TestAdvisorSchema(StoreCase):

    def test_schema_version_matches_constant_with_advisor_tables(self):
        """建库即最新版，且 advisor_runs / advisor_items 已在 counts() 里（v3 的核心交付）。

        版本号一律对着 ``SCHEMA_VERSION`` 断言，**不写死数字**：本文件原本写死 3，
        v4（自动交易四表）一落地这两个迁移用例立刻变红，而失败原因与选股记录毫无关系。
        """
        self.assertEqual(SCHEMA_VERSION, EXPECTED_SCHEMA_VERSION)
        self.assertEqual(self.store.schema_version(), SCHEMA_VERSION)

        counts = self.store.counts()
        # 断言「键存在且为 0」而不是「键存在与否」：后者在键不存在时也不会失败，
        # 会让「忘了把新表加进 counts()」这种回归被静默放过
        self.assertIn("advisor_runs", counts)
        self.assertIn("advisor_items", counts)
        self.assertEqual((counts["advisor_runs"], counts["advisor_items"]), (0, 0))

        tables = {r["name"] for r in self.store._conn().execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"advisor_runs", "advisor_items"} <= tables)
        # 级联删除依赖外键开关，删除语义是本模块的基础，必须真的是开的
        self.assertTrue(self.store.foreign_keys_enabled())

        # MIGRATIONS 是结构升级的唯一入口：版本号必须连续覆盖到最新，
        # 且**引入 advisor 表的那一步**（v3）必须存在、描述与实现齐备，
        # 否则 init_schema(version=2) → migrate() 的演练会静默跳过新表
        versions = [m["version"] for m in MIGRATIONS]
        self.assertEqual(versions, list(range(1, EXPECTED_SCHEMA_VERSION + 1)))
        step3 = [m for m in MIGRATIONS if m["version"] == 3][0]
        self.assertIn("advisor", step3["desc"])
        self.assertTrue(callable(step3["fn"]))

    def test_v2_migration_to_latest_keeps_data_and_is_idempotent(self):
        """v2 老库直升级到最新版：补出新表、老数据不动、重复调用是空操作。

        为什么用 init_schema(version=2) 而不是手写 DDL：迁移演练要的正是「真实的历史
        结构」；手写 DDL 一旦与 MIGRATIONS 里的 v1/v2 定义漂移，测的就不是真东西了。
        """
        store = Store(":memory:", init=False)
        self.addCleanup(store.close)
        self.assertEqual(store.init_schema(version=2), 2)
        self.assertEqual(store.schema_version(), 2)

        # 迁移前的对照：v2 库里确实还没有 v3 的两张表（否则「迁移成功」无从谈起）
        before = {r["name"] for r in store._conn().execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertNotIn("advisor_runs", before)
        self.assertNotIn("advisor_items", before)

        # 塞一条 v2 时代就存在的普通任务（含嵌套字段，验证 JSON 列没被迁移动过）
        legacy = {
            "id": "run-legacy", "code": "600519", "name": "贵州茅台", "market": "cn",
            "createdAt": 1775000000000, "createdDate": "2026-04-01",
            "params": {"fast": 5, "slow": 20},
            "monthly": {"2026-03": {"wins": 1, "trades": 2}},
        }
        store.upsert_run(legacy)
        store.append_trade("run-legacy", {"side": "buy", "qty": 100})

        result = store.migrate()
        self.assertEqual(result, {"from": 2, "to": SCHEMA_VERSION,
                                  "applied": list(range(3, SCHEMA_VERSION + 1))})
        self.assertEqual(store.schema_version(), SCHEMA_VERSION)

        # v3 两张表真的可用：能写能读（只判断「表存在」不够，列名/约束错了照样“存在”）
        rec = make_record([make_row("600519", action="buy")], ts=TS1)
        self.assertEqual(store.save_advisor_run(rec), rec["run"]["id"])
        self.assertEqual(store.counts()["advisor_runs"], 1)
        self.assertEqual(store.counts()["advisor_items"], 1)

        # 老数据完好：任务 payload（嵌套字段）与逐笔交易都不受影响
        got = store.get_run("run-legacy")
        self.assertEqual(got["params"], {"fast": 5, "slow": 20})
        self.assertEqual(got["monthly"]["2026-03"], {"wins": 1, "trades": 2})
        self.assertEqual(store.get_run_column("run-legacy", "code"), "600519")
        self.assertEqual(len(store.list_trades("run-legacy")), 1)

        # 幂等：已经是最新版，再迁移不应有步骤被应用，也不应丢数据
        again = store.migrate()
        self.assertEqual(again, {"from": SCHEMA_VERSION, "to": SCHEMA_VERSION, "applied": []})
        self.assertEqual(store.counts()["advisor_runs"], 1)
        self.assertEqual(store.get_run("run-legacy")["name"], "贵州茅台")
        self.assertIsNotNone(store.meta_get("schema_migrated_at"))


# --------------------------------------------------------------------------- #
# 3~4. 写入、幂等重写与原样还原
# --------------------------------------------------------------------------- #
class TestAdvisorWrite(StoreCase):

    def test_save_counts_and_idempotent_rewrite(self):
        """写入后计数正确；同一 id 重写不产生重复行（服务端重试/回填必须安全）。"""
        rows = [make_row("600519"), make_row("000001", action="hold"),
                make_row("600036", action="avoid")]
        rec = make_record(rows, ts=TS1, note="第一次")
        rid = self.store.save_advisor_run(rec)

        counts = self.store.counts()
        self.assertEqual(counts["advisor_runs"], 1)
        self.assertEqual(counts["advisor_items"], 3)

        # 幂等：同一条记录（同 id）重写后行数不变（save_advisor_run 先按 run_id 清明细）
        self.assertEqual(self.store.save_advisor_run(rec), rid)
        counts = self.store.counts()
        self.assertEqual((counts["advisor_runs"], counts["advisor_items"]), (1, 3))

        # 重写换成另一批明细时，旧明细必须被清掉（不是追加）
        self.store.save_advisor_run(make_record([make_row("000858", action="add")],
                                                ts=TS1, rid=rid))
        counts = self.store.counts()
        self.assertEqual((counts["advisor_runs"], counts["advisor_items"]), (1, 1))
        self.assertEqual([r["code"] for r in self.store.advisor_items(rid)], ["000858"])

    def test_rewrite_keeps_pinned_and_null_note(self):
        """重写不得复位用户手工状态（pinned），也不得用 NULL 把已有备注冲掉。

        pinned 与 note 是「用户产生的状态」，记录主体是「一次研判的快照」，两者生命周期
        不同：所以 ON CONFLICT 里 pinned 直接沿用旧值、note 走 COALESCE。
        """
        rid = self.store.save_advisor_run(make_record([make_row("600519")], ts=TS1))
        self.assertFalse(self.store.get_advisor_run(rid)["pinned"])

        self.store.update_advisor_run(rid, note="这条要留着", pinned=True)

        # 重试同一条记录（note 缺省 → to_record 传 None）：两者都不该被复位
        self.store.save_advisor_run(make_record([make_row("600519")], ts=TS1,
                                                rid=rid, note=None))
        got = self.store.get_advisor_run(rid)
        self.assertTrue(got["pinned"], "置顶是用户状态，重写记录不得把它复位")
        self.assertEqual(got["note"], "这条要留着", "NULL 备注不得覆盖已有备注")

        # 传入真实备注则照常更新（COALESCE 只对 NULL 生效，不能做成「永远写不进」）
        self.store.save_advisor_run(make_record([make_row("600519")], ts=TS1,
                                                rid=rid, note="换一条备注"))
        got = self.store.get_advisor_run(rid)
        self.assertEqual(got["note"], "换一条备注")
        self.assertTrue(got["pinned"])

        # 口径说明（锁行为，不是缺陷）：SQL 里空串 ≠ NULL，所以 note="" 会写进去。
        # 若产品口径是「空串也算空值」，应改成 COALESCE(NULLIF(excluded.note, ''), ...)
        self.store.save_advisor_run(make_record([make_row("600519")], ts=TS1,
                                                rid=rid, note=""))
        self.assertEqual(self.store.get_advisor_run(rid)["note"], "")

    def test_get_restores_rows_field_by_field(self):
        """get_advisor_run 原样还原逐只结论：逐字段一致 + 顺序 = 写入顺序。

        为什么逐字段断言：明细落库时一部分字段被提升成独立列（action / score /
        kelly_weight / plan_entry…），另一部分整体进 payload。只看整体相等会漏掉
        「独立列与 payload 不一致」这类真实故障，所以两层都要核。
        """
        rows = [
            make_row("600519", action="buy", price=1655.0, weight=0.25, score=86.5),
            make_row("000001", action="hold", price=11.32, weight=0.0, score=61.0,
                     changePct=-0.8),
            make_row("AAPL", action="sell", price=196.4, weight=0.15, score=33.0,
                     market="us"),
        ]
        rec = make_record(rows, ts=TS1)
        rid = self.store.save_advisor_run(rec)
        got = self.store.get_advisor_run(rid)
        self.assertIsNotNone(got)

        # 整体相等：JSON 往返（_dumps/_loads）对 JSON 原生类型必须无损。
        # 对照的是 to_record 之后的行（预测带已被裁剪），而不是裁剪前的原始响应行
        self.assertNotEqual(rec["rows"][0]["advisor"]["forecast"]["path"],
                            rows[0]["advisor"]["forecast"]["path"])
        self.assertEqual(got["rows"], rec["rows"])
        self.assertEqual([r["code"] for r in got["rows"]], ["600519", "000001", "AAPL"])
        self.assertEqual([r["name"] for r in got["rows"]], ["600519", "000001", "AAPL"])

        first = got["rows"][0]
        self.assertEqual(first["kelly"], {"fStar": 0.3, "kind": "discrete", "weight": 0.25,
                                         "amount": 1000.0, "shares": 10, "lot": 100})
        self.assertEqual(first["forecast"]["expectedReturn"], 3.2)
        self.assertEqual(first["forecast"]["quantiles"]["95"], 8.0)
        self.assertEqual(first["plan"]["target2"], 121.0)
        self.assertEqual(first["advisor"]["marks"][0]["strategy"], "maCross")
        self.assertEqual(first["advisor"]["plan"]["stop"], 92.0)
        self.assertEqual(got["rows"][1]["changePct"], -0.8)
        self.assertEqual(got["rows"][2]["market"], "us")

        # 独立列与 payload 同源：抽查列值与 payload 的对应字段一致（列写错会在这里露出来）
        raw = self.store._conn().execute(
            "SELECT code, action, score, kelly_weight, plan_entry, exp_return, seq"
            " FROM advisor_items WHERE run_id = ? ORDER BY seq ASC", (rid,)).fetchall()
        self.assertEqual([r["code"] for r in raw], ["600519", "000001", "AAPL"])
        self.assertEqual([r["action"] for r in raw], ["buy", "hold", "sell"])
        self.assertEqual([r["score"] for r in raw], [86.5, 61.0, 33.0])
        self.assertEqual([r["kelly_weight"] for r in raw], [0.25, 0.0, 0.15])
        self.assertEqual([r["plan_entry"] for r in raw], [1655.0, 11.32, 196.4])
        self.assertEqual([r["exp_return"] for r in raw], [3.2, 3.2, 3.2])
        self.assertEqual([r["seq"] for r in raw], [0, 1, 2])

        # advisor_items() 与 get_advisor_run()["rows"] 必须同源同序
        # （复盘读前者、载入回放读后者，两者不一致会导致「复盘的和看到的不一致」）
        self.assertEqual(self.store.advisor_items(rid), rec["rows"])
        self.assertEqual(self.store.advisor_items("ar-不存在"), [])
        self.assertIsNone(self.store.get_advisor_run("ar-不存在"))


# --------------------------------------------------------------------------- #
# 5. 列表：排序、过滤、分页
# --------------------------------------------------------------------------- #
class TestAdvisorList(StoreCase):

    def setUp(self):
        super().setUp()
        # 三条记录：TS1 最旧（cn / 600519 / buy）、TS2 最新（us / AAPL / hold）、
        # TS3 中间（cn / 000001 / add，名称「平安银行」）
        self.rid_of = {
            TS1: self.store.save_advisor_run(make_record(
                [make_row("600519", action="buy", market="cn")], ts=TS1, note="茅台那次")),
            TS2: self.store.save_advisor_run(make_record(
                [make_row("AAPL", action="hold", market="us", name="苹果")],
                ts=TS2, note="苹果那次", market="us")),
            TS3: self.store.save_advisor_run(make_record(
                [make_row("000001", action="add", market="cn", name="平安银行")],
                ts=TS3, note="平安那次")),
        }

    def test_order_pinned_first_then_time_desc(self):
        """默认时间倒序（最新在前）；置顶后该条跳到最前，其余仍按时间倒序。"""
        page = self.store.list_advisor_runs()
        self.assertEqual([r["createdAt"] for r in page["rows"]], [TS3, TS2, TS1])
        self.assertEqual(page["total"], 3)
        self.assertEqual(self.store.advisor_pinned_count(), 0)

        self.assertTrue(self.store.update_advisor_run(self.rid_of[TS1], pinned=True))
        page = self.store.list_advisor_runs()
        self.assertEqual(page["rows"][0]["id"], self.rid_of[TS1],
                         "置顶必须优先于时间排序，否则「置顶」在列表页毫无意义")
        self.assertEqual([r["createdAt"] for r in page["rows"]], [TS1, TS3, TS2])
        self.assertTrue(page["rows"][0]["pinned"])
        self.assertEqual(self.store.advisor_pinned_count(), 1)

    def test_filters_market_code_action_q(self):
        """四种过滤各自生效，并且可以叠加。

        code / action 走 advisor_items 的 EXISTS 子查询（一次研判里可能几十只标的，
        记录级字段根本表达不了「这次记录提到过某只票」），所以必须单独验证。
        """
        market_cn = self.store.list_advisor_runs(market="cn")
        self.assertEqual([r["createdAt"] for r in market_cn["rows"]], [TS3, TS1])
        self.assertEqual(market_cn["total"], 2)
        self.assertEqual(self.store.list_advisor_runs(market="us")["total"], 1)

        # code：查询侧会 .upper() 归一（服务端 _advisor_query 已 upper 过一次，
        # recommend 的 _normalize_symbols 也 upper）——链路两端口径一致
        self.assertEqual(self.store.list_advisor_runs(code="600519")["total"], 1)
        self.assertEqual(self.store.list_advisor_runs(code="AAPL")["total"], 1)
        self.assertEqual(self.store.list_advisor_runs(code="aapl")["total"], 1)
        self.assertEqual(self.store.list_advisor_runs(code="999999")["total"], 0)

        self.assertEqual(self.store.list_advisor_runs(action="add")["total"], 1)
        self.assertEqual(self.store.list_advisor_runs(action="buy")["total"], 1)
        self.assertEqual(self.store.list_advisor_runs(action="watch")["total"], 0)

        # q：备注、记录 id、标的代码、标的名称四条路都要能命中
        self.assertEqual(self.store.list_advisor_runs(q="茅台")["total"], 1)
        self.assertEqual(self.store.list_advisor_runs(q="苹果")["total"], 1)
        self.assertEqual(self.store.list_advisor_runs(q="平安")["total"], 1)
        self.assertEqual(self.store.list_advisor_runs(q="000001")["total"], 1)
        self.assertEqual(self.store.list_advisor_runs(q=self.rid_of[TS2])["total"], 1)
        self.assertEqual(self.store.list_advisor_runs(q="不存在的关键词")["total"], 0)

        # 叠加过滤：cn + buy 只有最旧那条
        both = self.store.list_advisor_runs(market="cn", action="buy")
        self.assertEqual([r["createdAt"] for r in both["rows"]], [TS1])
        self.assertEqual(both["total"], 1)

    def test_pinned_filter_only_true_is_honored(self):
        """pinned=True 只返回置顶；pinned=False 视为「不过滤」（当前实现如此）。

        锁这个行为是为了让调用方知道：想取「未置顶」必须自己按 pinned 字段再过滤，
        传 False 拿到的仍是全量——这是容易踩的坑，写成断言比写在注释里可靠。
        """
        self.store.update_advisor_run(self.rid_of[TS3], pinned=True)
        pinned = self.store.list_advisor_runs(pinned=True)
        self.assertEqual(pinned["total"], 1)
        self.assertEqual([r["id"] for r in pinned["rows"]], [self.rid_of[TS3]])
        self.assertEqual(self.store.list_advisor_runs(pinned=False)["total"], 3)
        self.assertEqual(self.store.advisor_pinned_count(), 1)

    def test_limit_offset_and_total(self):
        """limit / offset 只影响本页，total 始终是「过滤后的总数」。

        这组数字必须自洽，否则前端的「第 2 页 / 共 N 条」会算错。
        """
        page = self.store.list_advisor_runs(limit=2)
        self.assertEqual(len(page["rows"]), 2)
        self.assertEqual((page["limit"], page["offset"]), (2, 0))
        self.assertEqual(page["total"], 3)

        page2 = self.store.list_advisor_runs(limit=2, offset=2)
        self.assertEqual(len(page2["rows"]), 1)
        self.assertEqual((page2["limit"], page2["offset"]), (2, 2))
        self.assertEqual(page2["total"], 3, "total 不受 limit/offset 影响")

        # 两页拼起来 = 全量且不重不漏
        all_ids = [r["id"] for r in self.store.list_advisor_runs()["rows"]]
        self.assertEqual([r["id"] for r in page["rows"]] + [r["id"] for r in page2["rows"]],
                         all_ids)

        # 过滤后的 total 与分页也要自洽（把 total 算成「全表数」是最常见的分页 bug）
        flt = self.store.list_advisor_runs(market="cn", limit=1)
        self.assertEqual(flt["total"], 2)
        self.assertEqual(len(flt["rows"]), 1)

        # offset 超出范围：空页但不报错，total 不变
        empty = self.store.list_advisor_runs(offset=99)
        self.assertEqual((empty["rows"], empty["total"]), ([], 3))


# --------------------------------------------------------------------------- #
# 6. 更新（备注 / 置顶）与删除（级联）
# --------------------------------------------------------------------------- #
class TestAdvisorUpdateDelete(StoreCase):

    def setUp(self):
        super().setUp()
        self.rid = self.store.save_advisor_run(make_record(
            [make_row("600519"), make_row("000001", action="hold")],
            ts=TS1, note="原始备注"))
        self.other = self.store.save_advisor_run(make_record(
            [make_row("600036", action="avoid")], ts=TS2))

    def test_update_only_touches_given_fields(self):
        """只传 note 不动 pinned，只传 pinned 不动 note（否则「点星星丢备注」会上线）。"""
        self.store.update_advisor_run(self.rid, pinned=True)
        got = self.store.get_advisor_run(self.rid)
        self.assertTrue(got["pinned"])
        self.assertEqual(got["note"], "原始备注", "只改置顶时不得碰备注")

        view = self.store.update_advisor_run(self.rid, note="改过的备注")
        self.assertEqual(view["note"], "改过的备注")
        self.assertTrue(view["pinned"], "只改备注时不得碰置顶")

        # 取消置顶：False 是有效入参（≠「不传」），必须真的写成 0
        view = self.store.update_advisor_run(self.rid, pinned=False)
        self.assertFalse(view["pinned"])
        self.assertEqual(view["note"], "改过的备注")
        self.assertEqual(self.store.advisor_pinned_count(), 0)

        # 两个字段一起传
        view = self.store.update_advisor_run(self.rid, note="都要改", pinned=True)
        self.assertEqual((view["note"], view["pinned"]), ("都要改", True))

        # 什么都不传：返回当前视图、不写库
        view = self.store.update_advisor_run(self.rid)
        self.assertEqual((view["note"], view["pinned"]), ("都要改", True))

        self.assertIsNone(self.store.update_advisor_run("ar-不存在", note="x"),
                          "更新不存在的记录返回 None，调用方才能给出 404 而不是 200")
        self.assertIsNone(self.store.update_advisor_run("ar-不存在", pinned=True))

    def test_delete_cascades_items(self):
        """删除记录时逐只明细靠外键级联清空，且只影响这一条记录。"""
        self.assertEqual(self.store.counts()["advisor_items"], 3)
        self.assertTrue(self.store.delete_advisor_run(self.rid))
        self.assertFalse(self.store.delete_advisor_run(self.rid),
                         "删不存在的记录返回 False（幂等删除要能分辨「删没删到」）")
        self.assertIsNone(self.store.get_advisor_run(self.rid))
        self.assertEqual(self.store.advisor_items(self.rid), [])

        counts = self.store.counts()
        self.assertEqual(counts["advisor_runs"], 1)
        self.assertEqual(counts["advisor_items"], 1, "被删记录的明细必须级联清空")
        self.assertEqual([r["code"] for r in self.store.advisor_items(self.other)],
                         ["600036"])

        self.assertFalse(self.store.delete_advisor_run("ar-从未存在"))


# --------------------------------------------------------------------------- #
# 7. 保留上限（自动清理）
# --------------------------------------------------------------------------- #
class TestAdvisorPrune(StoreCase):

    def _seed(self, n, pinned=()):
        """写入 n 条记录（时间递增），pinned 里的序号置顶；返回按时间升序的 id 列表。"""
        ids = []
        for i in range(n):
            ids.append(self.store.save_advisor_run(make_record(
                [make_row("60000%d" % (i % 10))], ts=TS1 + i * 86400000)))
        for i in pinned:
            self.store.update_advisor_run(ids[i], pinned=True)
        return ids

    def test_prune_keeps_newest_free_records(self):
        """超出 keep 时只清理**最旧的非置顶**记录，并返回本次删除条数。"""
        ids = self._seed(5)
        self.assertEqual(self.store.advisor_prune(keep=3), 2)
        left = [r["id"] for r in self.store.list_advisor_runs()["rows"]]
        self.assertEqual(left, [ids[4], ids[3], ids[2]], "留下的必须是最新三条")
        self.assertEqual(self.store.counts()["advisor_items"], 3)

        # 已满足上限：再清理是空操作（幂等），否则「反复调用把记录清空」会成为线上事故
        self.assertEqual(self.store.advisor_prune(keep=3), 0)
        self.assertEqual(self.store.advisor_prune(keep=10), 0)
        self.assertEqual(self.store.counts()["advisor_runs"], 3)

    def test_pinned_records_never_pruned(self):
        """置顶记录永远不参与自动清理，哪怕它是最旧的。"""
        ids = self._seed(5, pinned=(0, 1))
        self.assertEqual(self.store.advisor_pinned_count(), 2)
        self.assertEqual(self.store.advisor_prune(keep=1), 2)   # 3 条非置顶里清掉最旧 2 条
        left = [r["id"] for r in self.store.list_advisor_runs()["rows"]]
        self.assertIn(ids[0], left, "最旧但已置顶，不能被清理")
        self.assertIn(ids[1], left)
        self.assertNotIn(ids[2], left)
        self.assertEqual(self.store.advisor_pinned_count(), 2)

        # keep=0 也不动置顶：清空的是「非置顶」，不是「全部」
        self.assertEqual(self.store.advisor_prune(keep=0), 1)
        left = [r["id"] for r in self.store.list_advisor_runs()["rows"]]
        self.assertEqual(sorted(left), sorted([ids[0], ids[1]]))
        self.assertEqual(self.store.advisor_prune(keep=0), 0,
                         "只剩置顶记录时清理 0 条（不报错、不误删）")

    def test_keep_zero_clears_all_free_records(self):
        """keep=0 = 前端「清空全部」：非置顶全清，明细级联一起走。"""
        ids = self._seed(4)
        self.assertEqual(self.store.advisor_prune(keep=0), 4)
        self.assertEqual(self.store.counts()["advisor_runs"], 0)
        self.assertEqual(self.store.counts()["advisor_items"], 0)
        self.assertIsNone(self.store.get_advisor_run(ids[0]))

    def test_pruned_total_accumulates_in_stats(self):
        """累计清理条数写进 meta，供前端如实展示保留策略（不是只报本次删除数）。"""
        self.assertEqual(self.store.advisor_stats()["prunedTotal"], 0)
        self._seed(5)
        self.store.advisor_prune(keep=3)                        # 清 2
        self.assertEqual(self.store.advisor_stats()["prunedTotal"], 2)
        self._seed(3)                                           # 又写 3 条 → 共 6 条非置顶
        self.assertEqual(self.store.advisor_prune(keep=1), 5)    # 清 5
        self.assertEqual(self.store.advisor_stats()["prunedTotal"], 7)
        self.assertEqual(self.store.advisor_stats()["records"], 1)

    def test_save_respects_keep_argument(self):
        """save_advisor_run(keep=N) 本身就会按上限清理——服务端不必额外调用 prune。"""
        for i in range(4):
            self.store.save_advisor_run(
                make_record([make_row("60000%d" % i)], ts=TS1 + i * 86400000), keep=2)
        self.assertEqual(self.store.counts()["advisor_runs"], 2)
        self.assertEqual(self.store.advisor_stats()["prunedTotal"], 2)

        # 默认 keep 是 ADVISOR_KEEP（很大），日常写入不会触发清理
        self.store.save_advisor_run(make_record([make_row("601888")], ts=TS3))
        self.assertEqual(self.store.counts()["advisor_runs"], 3)
        self.assertEqual(self.store.advisor_stats()["keep"], Store.ADVISOR_KEEP)


# --------------------------------------------------------------------------- #
# 8. 汇总统计
# --------------------------------------------------------------------------- #
class TestAdvisorStats(StoreCase):

    def test_counts_and_averages(self):
        """records / items / buyTotal / avgTotalWeight / latestAt / keep 的口径。"""
        early = self.store.save_advisor_run(make_record(
            [make_row("600519", action="buy"), make_row("000001", action="hold"),
             make_row("600036", action="add")], ts=TS1,
            portfolio={"totalWeight": 0.5}))
        late = self.store.save_advisor_run(make_record(
            [make_row("AAPL", action="sell", market="us"),
             make_row("MSFT", action="avoid", market="us")], ts=TS2,
            portfolio={"totalWeight": 0.5}))

        stats = self.store.advisor_stats()
        self.assertEqual(stats["records"], 2)
        self.assertEqual(stats["items"], 5)
        self.assertEqual(stats["buyTotal"], 2, "buyTotal 只数 buy + add（hold/sell/avoid 不算）")
        self.assertEqual(stats["latestAt"], TS2, "latestAt = 最新一条的 createdAt")
        self.assertEqual(stats["keep"], Store.ADVISOR_KEEP)
        self.assertEqual(stats["prunedTotal"], 0)
        self.assertEqual(stats["avgTotalWeight"], 0.5)
        self.assertEqual(late, self.store.list_advisor_runs()["rows"][0]["id"])

        # 换成两个不同的 totalWeight，均值必须跟着变（避免「恰好都为 0」式的假通过）
        self.assertTrue(self.store.delete_advisor_run(early))
        self.assertTrue(self.store.delete_advisor_run(late))
        self.store.save_advisor_run(make_record([make_row("601888")], ts=TS1,
                                                portfolio={"totalWeight": 0.2}))
        self.store.save_advisor_run(make_record([make_row("600036")], ts=TS2,
                                                portfolio={"totalWeight": 0.4}))
        stats = self.store.advisor_stats()
        self.assertEqual((stats["records"], stats["items"]), (2, 2))
        self.assertAlmostEqual(stats["avgTotalWeight"], 0.3, places=6)

    def test_stats_on_empty_store_is_well_formed(self):
        """空库的汇总也要返回完整字段（前端首屏不能拿到 KeyError 或 NaN）。"""
        stats = self.store.advisor_stats()
        self.assertEqual(stats["records"], 0)
        self.assertEqual(stats["items"], 0)
        self.assertEqual(stats["buyTotal"], 0)
        self.assertEqual(stats["avgTotalWeight"], 0.0)
        self.assertEqual(stats["prunedTotal"], 0)
        self.assertIsNone(stats["latestAt"], "没有记录时 latestAt 是 None，不是 0")
        self.assertEqual(self.store.advisor_pinned_count(), 0)


# --------------------------------------------------------------------------- #
# 9. to_record：记录 id 与 createdAt / createdDate
# --------------------------------------------------------------------------- #
class TestToRecordId(unittest.TestCase):

    def test_record_id_shape_and_timestamp(self):
        """id 形态 ar-<13位毫秒>-<4位十六进制>；毫秒段就是传入的 ts（可读、可排序）。"""
        rid = A.record_id(TS_LATER)
        self.assertRegex(rid, r"^ar-\d{13}-[0-9a-f]{4}$")
        self.assertEqual(rid.split("-")[1], str(TS_LATER))
        self.assertEqual(len(rid.split("-")[1]), 13, "毫秒时间戳当前固定 13 位")

        # 用打桩的 uuid 固定后缀：确认 id 确实由「ts + 随机后缀」两段拼成
        with mock.patch.object(A.uuid, "uuid4") as fake:
            fake.return_value.hex = "a1b2c3d4e5"
            self.assertEqual(A.record_id(TS_LATER), "ar-%d-a1b2" % TS_LATER)

        # 同一毫秒内两次调用不应撞 id（否则后写的记录会静默覆盖前一条）。
        # 4 位十六进制有 65536 种取值，单次碰撞概率约 1/65536，可接受
        self.assertNotEqual(A.record_id(TS_LATER), A.record_id(TS_LATER))

    def test_created_at_and_date_follow_ts(self):
        """createdAt 就是传入的 ts，createdDate 是它的**本地**日期（复盘基准日的来源）。"""
        run = make_record([make_row("600519")], ts=TS_LATER)["run"]
        self.assertEqual(run["createdAt"], TS_LATER)
        self.assertEqual(run["createdDate"], "2026-04-06")
        self.assertEqual(run["createdDate"],
                         time.strftime("%Y-%m-%d", time.localtime(TS_LATER / 1000.0)))
        self.assertRegex(run["id"], r"^ar-\d{13}-[0-9a-f]{4}$")
        self.assertEqual(run["id"].split("-")[1], str(run["createdAt"]))

        # rid 显式传入时以传入值为准（重试/回填要能保持同一条记录的身份）
        self.assertEqual(make_record([make_row("600519")], ts=TS_LATER,
                                     rid="ar-fixed-0001")["run"]["id"], "ar-fixed-0001")

        # 不传 ts 时走 now_ms()：createdAt 必须落在「调用前后」之间，而不是 0/None
        before = A.now_ms()
        run2 = A.to_record(make_res([make_row("600519")]))["run"]
        self.assertTrue(before <= run2["createdAt"] <= A.now_ms())
        self.assertTrue(run2["createdDate"])


# --------------------------------------------------------------------------- #
# 10~11. to_record：七档计数、buyCount/actionableCount、topRows
# --------------------------------------------------------------------------- #
class TestToRecordSummary(unittest.TestCase):

    def test_action_counts_and_buy_count(self):
        """七档计数之和 == 行数；buyCount == actionableCount == buy + add。"""
        actions = ["buy", "add", "hold", "reduce", "sell", "watch", "avoid"]
        rows = [make_row("60000%d" % i, action=act) for i, act in enumerate(actions)]
        run = make_record(rows, ts=TS1)["run"]

        counts = run["summary"]["actions"]
        self.assertEqual(set(counts.keys()), set(A.ACTION_LABEL.keys()),
                         "七档必须齐全：前端拿 actions 渲染分布，缺键会渲染成空白")
        self.assertEqual(counts, {act: 1 for act in actions})
        self.assertEqual(sum(counts.values()), len(rows))
        self.assertEqual(run["buyCount"], 2)
        self.assertEqual(run["actionableCount"], 2)
        self.assertEqual(run["buyCount"], counts["buy"] + counts["add"])
        self.assertEqual(run["actionableCount"], run["buyCount"])
        self.assertEqual(run["symbolCount"], len(rows))

        # 「数据不足」的行没有档位（action=None）：不计入七档与 buyCount，但占 symbolCount
        rows2 = rows + [make_row("600036", action=None)]
        run2 = make_record(rows2, ts=TS1)["run"]
        self.assertEqual(sum(run2["summary"]["actions"].values()), len(rows2) - 1)
        self.assertEqual(run2["buyCount"], 2)
        self.assertEqual(run2["symbolCount"], len(rows2))

    def test_top_rows_are_actionable_first(self):
        """topRows 最多 3 条，且可操作性优先、同档按评分降序。

        为什么不是「评分最高的 3 只」：评分高但档位是「持有/观望」的行拿不到钱，
        摘要里优先展示它们等于把用户往无效信息上引。
        """
        rows = [
            make_row("HOLD", action="hold", score=99.0),      # 评分最高但不可操作
            make_row("WATCH", action="watch", score=98.0),
            make_row("ADD", action="add", score=50.0),
            make_row("BUY1", action="buy", score=40.0),
            make_row("BUY2", action="buy", score=88.0),
        ]
        top = make_record(rows, ts=TS1)["run"]["summary"]["topRows"]
        self.assertEqual([r["code"] for r in top], ["BUY2", "BUY1", "ADD"],
                         "先按档位（buy < add < … < hold < watch），同档再按评分降序")
        self.assertLessEqual(len(top), 3)

        # 元素结构：前端摘要直接读这几个键
        self.assertEqual(set(top[0].keys()),
                         {"code", "name", "market", "action", "actionText", "score",
                          "kellyWeight"})
        self.assertEqual(top[0]["score"], 88.0)
        self.assertEqual(top[0]["kellyWeight"], 0.25)          # 取自 kelly.weight
        self.assertEqual(top[0]["actionText"], A.ACTION_LABEL["buy"])

        # 行数少于 3 时按实际条数返回；空行列表返回 []
        two = make_record([make_row("A", action="buy"), make_row("B", action="sell")],
                          ts=TS1)["run"]["summary"]["topRows"]
        self.assertEqual(len(two), 2)
        self.assertEqual(two[0]["code"], "A", "buy(0) 排在 sell(2) 之前")
        self.assertEqual(make_record([], ts=TS1)["run"]["summary"]["topRows"], [])

        # kelly 为 None 的行不能把摘要炸掉（真实数据里 error 行的 kelly 就是 None）
        broken = make_record([dict(make_row("X"), kelly=None)], ts=TS1)
        self.assertIsNone(broken["run"]["summary"]["topRows"][0]["kellyWeight"])


# --------------------------------------------------------------------------- #
# 12~13. to_record：裁剪预测带、payload 不含 rows、codes 覆盖全部行
# --------------------------------------------------------------------------- #
class TestToRecordTrim(unittest.TestCase):

    def test_forecast_path_trimmed_but_marks_and_plan_kept(self):
        """裁掉 advisor.forecast.path 并标记 trimmed=True；marks / plan 必须保留。

        这是刻意的体积取舍（预测带 20 个价格点锚在保存时的收盘价上，事后回看既不准也
        无意义，却占单行 JSON 近一半），但「买卖标记」与「交易计划」是当时的判断依据，
        必须留。trimmed 标记也不能少，否则前端会把「被裁掉」误读成「当时没有预测带」。
        """
        rows = [make_row("600519"), make_row("000001", action="hold")]
        original_path = rows[0]["advisor"]["forecast"]["path"]
        self.assertTrue(original_path, "前置条件：源数据的预测带非空，否则这条断言无从谈起")
        rec = make_record(rows, ts=TS1)

        for slim in rec["rows"]:
            adv = slim["advisor"]
            self.assertEqual(adv["forecast"]["path"], [], "预测带路径必须被裁空")
            self.assertIs(adv["forecast"]["trimmed"], True, "必须带 trimmed 标记")
            self.assertEqual(adv["forecast"]["horizon"], 20, "horizon 体积很小，保留")
            self.assertEqual(adv["forecast"]["levels"], {"p50": 1.2})
            self.assertEqual(adv["marks"], rows[0]["advisor"]["marks"],
                             "买卖标记必须原样保留（它是图表叠加层的数据源）")
            self.assertEqual(adv["plan"], {"entry": 100.0, "stop": 92.0,
                                          "target1": 112.0, "target2": 121.0},
                             "交易计划（当时的买卖价位）必须原样保留")

        # 行级顶层的 plan / forecast 不在裁剪范围内（详情页要靠它们做展示）
        self.assertEqual(rec["rows"][0]["plan"]["stop"], 92.0)
        self.assertEqual(rec["rows"][0]["forecast"]["expectedReturn"], 3.2)

        # 裁剪是「复制后改」，不能就地改掉调用方传进来的行（recommend 的响应还要回给前端）
        self.assertEqual(rows[0]["advisor"]["forecast"]["path"], original_path)
        self.assertNotIn("trimmed", rows[0]["advisor"]["forecast"])

        # 没有 advisor 字段的行（老记录 / 脏数据）不能被裁剪逻辑弄崩
        plain = A.to_record(make_res([{"code": "X", "action": "hold"}]), ts=TS1)
        self.assertEqual(plain["rows"][0]["code"], "X")

    def test_payload_excludes_rows_and_codes_cover_all(self):
        """run.payload 不含 rows（明细走 advisor_items，避免同一份数据存两遍）。"""
        rows = [make_row("600519", market="cn"),
                make_row("AAPL", action="sell", market="us")]
        res = make_res(rows, portfolio={"totalWeight": 0.4})
        rec = A.to_record(res, trigger="manual", note="手动那次", ts=TS1)
        run = rec["run"]

        self.assertNotIn("rows", run["payload"],
                         "明细已拆到 advisor_items，payload 再存一遍就是双份体积")
        self.assertEqual(run["payload"]["recordedAt"], TS1)
        self.assertEqual(run["payload"]["recordNote"], A.RECORD_NOTE)
        self.assertEqual(run["payload"]["portfolio"], {"totalWeight": 0.4})
        self.assertEqual(run["payload"]["disclaimer"], "测试用免责声明")
        self.assertEqual(run["payload"]["source"], "test-source")

        self.assertEqual(run["trigger"], "manual")
        self.assertEqual(run["note"], "手动那次")
        self.assertEqual(run["pinned"], False, "新记录默认未置顶")
        self.assertEqual(run["totalWeight"], 0.4)
        self.assertEqual(run["analyzed"], 2)

        codes = run["summary"]["codes"]
        self.assertEqual([c["code"] for c in codes], ["600519", "AAPL"],
                         "codes 必须覆盖全部行且保持顺序")
        self.assertEqual(codes[1], {"code": "AAPL", "market": "us", "name": "AAPL"})

        # 脏行（非 dict）不能进记录：to_record 的过滤与 store 的逐行跳过口径一致
        dirty = A.to_record(make_res([rows[0], None, "x", rows[1]]), ts=TS1)
        self.assertEqual([r["code"] for r in dirty["rows"]], ["600519", "AAPL"])
        self.assertEqual(dirty["run"]["symbolCount"], 2)

        # 无 portfolio 的响应不能让 totalWeight 变成 NaN（NaN 会让前端 JSON 解析直接失败）
        no_pf = A.to_record(make_res([rows[0]], portfolio=None), ts=TS1)["run"]
        self.assertIsNone(no_pf["totalWeight"])
        self.assertTrue(json.dumps(no_pf, allow_nan=False))


# --------------------------------------------------------------------------- #
# 14. review：收益口径（与手工复算逐位对齐）
# --------------------------------------------------------------------------- #
class TestReviewReturns(unittest.TestCase):

    def test_returns_match_manual_recalc(self):
        """sinceReturn / fwd[k].ret 必须与手算公式逐位一致（1e-6）。

        手算公式（也是口径说明里承诺的口径）：
        · fwd[k].ret = (bars[base+k].close / bars[base].close - 1) × 100，保留 3 位；
        · sinceReturn = (末根收盘 / 参考价 - 1) × 100，参考价优先取**记录里保存的价位**
          （= 当时给出的建议价），没有才回退到基准K线收盘价。
        为什么必须逐位对齐：这两个数字是复盘页最显眼的结论，差一个百分点都会被当成模型漂移。
        """
        bars, dates = series(100.0, changes={5: 105.0, 20: 96.0, TAIL: 103.0})
        rec = merged_record([make_row("600519", action="buy", price=100.0, weight=0.25)],
                            ts=TS1)
        res = A.review(rec, FakeBars({("cn", "600519"): bars}))
        self.assertTrue(res["ok"])
        row = res["rows"][0]

        idx0 = BASE_INDEX
        self.assertEqual(row["baseDate"], dates[idx0].isoformat())
        self.assertEqual(row["basePrice"], bars[idx0]["close"])
        self.assertEqual(row["refPrice"], 100.0)
        self.assertEqual(row["lastDate"], dates[-1].isoformat())
        self.assertEqual(row["lastPrice"], bars[-1]["close"])
        self.assertEqual(row["barsElapsed"], len(bars) - 1 - idx0)

        self.assertAlmostEqual(row["sinceReturn"], pct(row["refPrice"], bars[-1]["close"]),
                              delta=1e-6)
        self.assertEqual(row["sinceReturn"], 3.0)

        for k in HZ:
            j = idx0 + int(k)
            got = row["fwd"][str(k)]
            self.assertTrue(got["ready"])
            self.assertAlmostEqual(got["ret"], pct(bars[idx0]["close"], bars[j]["close"]),
                                   delta=1e-6)
            self.assertEqual(got["date"], dates[j].isoformat())
            self.assertEqual(got["price"], bars[j]["close"])
        self.assertEqual(row["fwd"]["5"]["ret"], 5.0)
        self.assertEqual(row["fwd"]["20"]["ret"], -4.0)

        # 权重 × 收益 = 账户口径贡献（两者都是百分数，乘积仍是百分数）
        self.assertAlmostEqual(row["contribution"], round(0.25 * 3.0, 4), delta=1e-6)

    def test_saved_price_is_the_reference_when_present(self):
        """参考价优先取「记录里保存的价位」，而不是基准K线收盘价。

        两者通常相等（保存当天的收盘价），但一旦不等（记录里存的是盘中价 / 手工价），
        用错基准会让复盘结论整体偏移，所以单独锁一条。
        """
        bars, _ = series(100.0, changes={TAIL: 110.0})

        rec = merged_record([make_row("600519", action="buy", price=100.0)], ts=TS1)
        self.assertEqual(A.review(rec, FakeBars({("cn", "600519"): bars}))
                         ["rows"][0]["sinceReturn"], 10.0)

        rec2 = merged_record([make_row("600519", action="buy", price=110.0)], ts=TS1)
        row2 = A.review(rec2, FakeBars({("cn", "600519"): bars}))["rows"][0]
        self.assertEqual(row2["basePrice"], 100.0, "基准价始终是基准K线的收盘价")
        self.assertEqual(row2["refPrice"], 110.0)
        self.assertAlmostEqual(row2["sinceReturn"], pct(110.0, 110.0), delta=1e-6)
        self.assertEqual(row2["sinceReturn"], 0.0)
        self.assertAlmostEqual(
            row2["fwd"]["5"]["ret"],
            pct(bars[BASE_INDEX]["close"], bars[BASE_INDEX + 5]["close"]), delta=1e-6)

        # 保存价缺失 / 为 0 / 是垃圾字符串时回退到基准K线收盘价（老记录可能没有 price）
        for bad in (None, 0, 0.0, "abc"):
            rec3 = merged_record([make_row("600519", action="buy", price=100.0)], ts=TS1)
            rec3["rows"][0]["price"] = bad
            row3 = A.review(rec3, FakeBars({("cn", "600519"): bars}))["rows"][0]
            self.assertEqual(row3["refPrice"], 100.0)
            self.assertEqual(row3["sinceReturn"], 10.0)

    def test_review_reads_persisted_record(self):
        """端到端：to_record → save_advisor_run → get_advisor_run → review。

        为什么还要端到端来一遍：复盘页拿到的是**数据库里**的记录，只有 createdDate 与
        rows 两个字段真正参与复盘；任何一处字段名/单位在落库时被改坏，都会在这里暴露。
        """
        store = Store(":memory:")
        self.addCleanup(store.close)
        bars, _ = series(100.0, changes={5: 105.0, TAIL: 103.0})
        rid = store.save_advisor_run(make_record(
            [make_row("600519", action="buy", price=100.0, weight=0.25)], ts=TS1))
        rec = store.get_advisor_run(rid)

        res = A.review(rec, FakeBars({("cn", "600519"): bars}))
        self.assertTrue(res["ok"])
        self.assertEqual(res["id"], rid, "复盘结果要回带记录 id，前端才能把结果贴回那条记录")
        self.assertEqual(res["baseDate"], rec["createdDate"])
        row = res["rows"][0]
        self.assertEqual((row["code"], row["action"]), ("600519", "buy"))
        self.assertEqual(row["sinceReturn"], 3.0)
        self.assertEqual(row["verdict"], "hit")
        self.assertEqual(row["savedPrice"], 100.0)

        # 取数只应取日线，且 limit 不低于 MIN_BARS（否则指标不足会把复盘变成噪声）
        feed = FakeBars({("cn", "600519"): bars})
        A.review(rec, feed, limit=1)
        self.assertEqual(feed.calls[0], {"market": "cn", "code": "600519",
                                        "period": "day", "limit": A.MIN_BARS})


# --------------------------------------------------------------------------- #
# 15~16. review：判定口径与基准日选择
# --------------------------------------------------------------------------- #
class TestReviewVerdict(unittest.TestCase):

    def _book(self):
        """构造「代码 → 行情」表：每只票只改自己需要的偏移，便于逐类核对判定。"""
        up, _ = series(100.0, changes={5: 105.0})
        down, _ = series(100.0, changes={5: 94.0})
        flat, _ = series(100.0, changes={5: 100.0})
        short, _ = series(100.0, changes={}, tail=3)      # 基准之后只有 3 根 → 未走满 5 根
        return {
            ("cn", "UP1"): up, ("cn", "UP2"): up, ("cn", "UP3"): up,
            ("cn", "DN1"): down, ("cn", "DN2"): down, ("cn", "DN3"): down,
            ("cn", "DN4"): down,
            ("cn", "FLAT"): flat, ("cn", "HOLD"): up, ("cn", "WATCH"): up,
            ("cn", "PEND"): short, ("cn", "NOACT"): up,
        }

    def test_verdict_matrix(self):
        """五类判定：看多涨=hit、看多跌=miss、看空跌=hit、中性=neutral、
        未满窗口=pending、无数据/无档位=nodata。"""
        rows = [
            make_row("UP1", action="buy", price=100.0),        # 涨 → hit
            make_row("UP2", action="add", price=100.0),        # 涨 → hit（增持也是看多）
            make_row("UP3", action="avoid", price=100.0),      # 涨 → miss（回避却涨）
            make_row("DN1", action="buy", price=100.0),        # 跌 → miss
            make_row("DN2", action="reduce", price=100.0),     # 跌 → hit
            make_row("DN3", action="sell", price=100.0),       # 跌 → hit
            make_row("DN4", action="avoid", price=100.0),      # 跌 → hit
            make_row("FLAT", action="buy", price=100.0),       # 持平 → 不算命中
            make_row("HOLD", action="hold", price=100.0),      # 中性，不计命中率
            make_row("WATCH", action="watch", price=100.0),    # 中性
            make_row("PEND", action="buy", price=100.0),       # 未走满 5 根 → pending
            make_row("NOACT", action=None, price=100.0),       # 无档位 → nodata
            make_row("", action="buy", price=100.0),           # 缺代码 → nodata
        ]
        rec = merged_record(rows, ts=TS1)
        rec["rows"] = rows + [
            make_row("UP2X", action="buy", price=100.0),       # 取数抛异常 → nodata
            make_row("NODATA", action="buy", price=100.0),     # 行情为空 → nodata
        ]
        feed = FakeBars(self._book(), fail=("UP2X",))
        got = {r["code"]: r for r in A.review(rec, feed, max_workers=1)["rows"]}

        expect = {
            "UP1": "hit", "UP2": "hit", "UP3": "miss",
            "DN1": "miss", "DN2": "hit", "DN3": "hit", "DN4": "hit",
            "FLAT": "miss", "HOLD": "neutral", "WATCH": "neutral",
            "PEND": "pending", "NOACT": "nodata", "": "nodata",
            "UP2X": "nodata", "NODATA": "nodata",
        }
        self.assertEqual({c: got[c]["verdict"] for c in expect}, expect)

        # 命中判定一律用**最短的已到期窗口**（5 根），保证同一条记录内不同标的口径一致
        for code in ("UP1", "UP2", "UP3", "DN1", "DN2", "DN3", "DN4", "FLAT"):
            row = got[code]
            self.assertEqual(row["verdictHorizon"], 5)
            self.assertEqual(row["verdictReturn"], row["fwd"]["5"]["ret"])
            self.assertIs(row["fwd"]["5"]["hit"], row["verdict"] == "hit",
                          "被判定的窗口要留下 hit 标记，前端才能高亮「哪一档判定命中」")
        # 未参与判定的窗口 hit 保持 None（不能因为整条判定为 hit 就把所有窗口都染成 hit）
        self.assertIsNone(got["UP1"]["fwd"]["20"]["hit"])

        # 中性档位：不计入命中率，但要如实记录涨跌
        self.assertIsNone(got["HOLD"]["fwd"]["5"]["hit"])
        self.assertIn("不计入命中率", got["HOLD"]["note"])
        self.assertIsNotNone(got["HOLD"]["sinceReturn"])

        # pending 行的 note 要写清「差多少根」，否则用户无法判断什么时候再看
        self.assertIn("5", got["PEND"]["note"])
        self.assertIn("3", got["PEND"]["note"])
        self.assertIsNone(got["PEND"]["verdictHorizon"])
        self.assertIs(got["PEND"]["fwd"]["5"]["ready"], False)
        self.assertIs(got["PEND"]["fwd"]["20"]["ready"], False)

        # nodata 的四种原因要能区分（否则排查时只能猜）
        self.assertEqual(got[""]["note"], "缺少标的代码")
        self.assertIn("行情获取失败", got["UP2X"]["note"])
        self.assertEqual(got["NODATA"]["note"], "无可用日线数据")
        self.assertIn("未给出可执行档位", got["NOACT"]["note"])

        # 18) 每行 fwd 永远含全部 horizon 键；nodata 行也是 {ret:null,ready:false} 形状
        for code, row in got.items():
            self.assertEqual(set(row["fwd"].keys()), HZ_KEYS, "缺键会让前端读到 undefined")
        for code in ("", "UP2X", "NODATA"):
            self.assertEqual(got[code]["fwd"]["5"], EMPTY_FWD)
            self.assertEqual(got[code]["fwd"]["20"], EMPTY_FWD)
            self.assertIsNone(got[code]["sinceReturn"])
            self.assertIsNone(got[code]["contribution"])

    def test_base_date_falls_back_to_previous_trading_day(self):
        """保存日不是交易日（周末）时，基准取「最后一个日期不晚于保存日」的K线。

        为什么单独测：记录常在盘中/周末保存，而K线只有交易日；若基准日取成「之后的
        第一根」，复盘的起点会晚一天，收益口径整体偏移。
        """
        # 先把日历事实钉死：2026-04-03 是周五、2026-04-05 是周日
        self.assertEqual(date(2026, 4, 3).weekday(), 4)
        self.assertEqual(date(2026, 4, 5).weekday(), 6)
        dates = business_days(date(2026, 3, 30), 30)
        self.assertEqual([d.isoformat() for d in dates[:6]],
                         ["2026-03-30", "2026-03-31", "2026-04-01", "2026-04-02",
                          "2026-04-03", "2026-04-06"])
        bars = bars_of(dates, [100.0] * len(dates))
        feed = FakeBars({("cn", "600519"): bars})

        # 周日保存 → 基准回退到上周五（第 4 根）
        weekend = merged_record([make_row("600519", action="buy", price=100.0)],
                                ts=ts_of(2026, 4, 5))
        self.assertEqual(weekend["createdDate"], "2026-04-05")
        row = A.review(weekend, feed)["rows"][0]
        self.assertEqual(row["baseDate"], "2026-04-03", "基准必须回退到上一个交易日")
        self.assertEqual(row["basePrice"], 100.0)
        self.assertEqual(row["barsElapsed"], len(dates) - 1 - 4)
        self.assertEqual(row["fwd"]["5"]["date"], dates[9].isoformat())

        # 保存日早于整个序列（记录里的日期被写坏）：退回第一根，不抛异常
        old = merged_record([make_row("600519", action="buy", price=100.0)], ts=TS1)
        old["createdDate"] = "1999-01-01"
        row_old = A.review(old, feed)["rows"][0]
        self.assertEqual(row_old["baseDate"], dates[0].isoformat())
        self.assertEqual(row_old["barsElapsed"], len(dates) - 1)

        # 保存日正好是交易日：基准就是当天，不回退
        same = merged_record([make_row("600519", action="buy", price=100.0)],
                             ts=ts_of(2026, 4, 6))
        row_same = A.review(same, feed)["rows"][0]
        self.assertEqual(row_same["baseDate"], "2026-04-06")
        self.assertEqual(row_same["barsElapsed"], len(dates) - 1 - 5)


# --------------------------------------------------------------------------- #
# 17. review：汇总聚合
# --------------------------------------------------------------------------- #
class TestReviewSummary(unittest.TestCase):

    #: 手工设计的组合：每只票只改「第 5 根」与「末根」两个点，收益都是整百分比
    #:  代码,  档位,    权重,  第5根收盘, 末根收盘, 期望判定
    CASES = (
        ("AAA", "buy",   0.25, 105.0, 105.0, "hit"),      # 看多 +5% → 命中，since +5
        ("BBB", "buy",   0.50, 98.0,   98.0, "miss"),     # 看多 -2% → 未命中，since -2
        ("CCC", "sell",  0.25, 94.0,   94.0, "hit"),      # 看空 -6% → 命中，since -6
        ("DDD", "hold",  0.25, 101.0, 101.0, "neutral"),  # 中性，since +1
        ("FFF", "hold",  0.00, 100.0, 100.0, "neutral"),  # 权重 0：进平均收益，不进仓位收益
        ("EEE", "avoid", 0.20, 100.0, 100.0, "nodata"),   # 取数失败
    )

    def _fixture(self):
        """按 CASES 表造记录 + 行情；EEE 走「取数失败」分支。"""
        book, rows = {}, []
        for code, action, weight, px5, px_last, _ in self.CASES:
            book[("cn", code)] = series(100.0, changes={5: px5, TAIL: px_last})[0]
            rows.append(make_row(code, action=action, price=100.0, weight=weight))
        return merged_record(rows, ts=TS1), FakeBars(book, fail=("EEE",))

    def test_summary_aggregates_match_hand_calc(self):
        """命中率、分组命中率、平均收益、仓位收益全部与手算一致（容差 1e-6）。

        手算过程（见 CASES 表）：
          判定组 = AAA(hit) / BBB(miss) / CCC(hit) → hitRate = 2/3 = 0.6667
          看多组 = AAA、BBB（只看已判定行）→ bullHitRate = 1/2 = 0.5
          看空组 = CCC → bearHitRate = 1/1 = 1.0
          sinceReturn：+5、-2、-6、+1、0（EEE 无数据）→ avgReturn = (-2)/5 = -0.4
          avgHitReturn = (5 + -6)/2 = -0.5；avgMissReturn = -2.0
          totalWeight = 0.25+0.5+0.25+0.25 = 1.25（权重 0 的 FFF 不计入）
          accountReturn = 0.25×5 + 0.5×(-2) + 0.25×(-6) + 0.25×1 = -1.0
          positionReturn = -1.0 / 1.25 = -0.8
        """
        rec, feed = self._fixture()
        res = A.review(rec, feed)
        summary = res["summary"]

        self.assertEqual((summary["total"], summary["ready"]), (6, 3))
        self.assertEqual((summary["hits"], summary["misses"]), (2, 1))
        self.assertEqual((summary["neutral"], summary["pending"], summary["nodata"]),
                         (2, 0, 1))
        self.assertEqual(summary["horizon"], min(HZ))

        self.assertEqual(summary["hitRate"], round(2 / 3, 4))
        # 恒等式核对时容差取 1e-4：hitRate 出口按 4 位取整，半单位误差就是 5e-5
        self.assertAlmostEqual(summary["hitRate"],
                               summary["hits"] / (summary["hits"] + summary["misses"]),
                               delta=1e-4)
        self.assertEqual(summary["bullHitRate"], 0.5)
        self.assertEqual(summary["bearHitRate"], 1.0)
        self.assertEqual((summary["bullCount"], summary["bearCount"]), (2, 1))

        # 平均收益一律是**百分数**（不是小数）：与 sinceReturn 同单位，前端直接加 % 号
        self.assertEqual(summary["avgReturn"], -0.4)
        self.assertEqual(summary["avgHitReturn"], -0.5)
        self.assertEqual(summary["avgMissReturn"], -2.0)

        # 仓位口径：只用 weight > 0 的行
        self.assertEqual(summary["totalWeight"], 1.25)
        self.assertEqual(summary["accountReturn"], -1.0)
        self.assertEqual(summary["positionReturn"], -0.8)
        self.assertAlmostEqual(summary["positionReturn"],
                               summary["accountReturn"] / summary["totalWeight"],
                               delta=1e-6)

        # 逐行 + 汇总的手工复算（不依赖 summary 的中间量）
        weight = {c: w for c, _, w, _, _, _ in self.CASES}
        valid = [c for c, _, _, _, _, v in self.CASES if v != "nodata"]   # 拿得到行情的行
        since = {c: pct(100.0, last) for c, _, _, _, last, v in self.CASES
                 if v != "nodata"}
        graded = [c for c, _, _, _, _, v in self.CASES if v in ("hit", "miss")]
        # 仓位口径 = 「拿得到行情 且 weight > 0」的行（权重 0 与取不到行情都不参与）
        position = [c for c in valid if weight[c] > 0]
        manual_account = sum(weight[c] * since[c] for c in position)
        manual_weight = sum(weight[c] for c in position)
        # 平均收益覆盖所有拿得到行情的行（含权重 0 的中性行）
        manual_avg = sum(since.values()) / len(since)
        manual_hits = [c for c in graded if self._verdict(c, since) == "hit"]
        self.assertAlmostEqual(summary["accountReturn"], manual_account, delta=1e-6)
        self.assertAlmostEqual(summary["totalWeight"], manual_weight, delta=1e-6)
        self.assertAlmostEqual(summary["avgReturn"], round(manual_avg, 3), delta=1e-6)
        self.assertEqual(summary["hits"], len(manual_hits))
        self.assertAlmostEqual(summary["hitRate"], round(len(manual_hits) / len(graded), 4),
                               delta=1e-6)

        # 逐行明细：verdict / sinceReturn / contribution 也要与 CASES 表对上
        # （避免出现「汇总对、明细错」这种最难排查的情况）
        for row, (code, action, w, px5, px_last, verdict) in zip(res["rows"], self.CASES):
            self.assertEqual((row["code"], row["action"], row["weight"]), (code, action, w))
            self.assertEqual(row["verdict"], verdict)
            if verdict == "nodata":
                self.assertIsNone(row["sinceReturn"])
                self.assertIsNone(row["contribution"])
            else:
                self.assertAlmostEqual(row["sinceReturn"], pct(100.0, px_last), delta=1e-6)
                self.assertAlmostEqual(row["fwd"]["5"]["ret"], pct(100.0, px5), delta=1e-6)
                if w > 0:
                    self.assertAlmostEqual(row["contribution"],
                                           round(w * pct(100.0, px_last), 4), delta=1e-6)
                else:
                    self.assertIsNone(row["contribution"],
                                      "权重 0 的行不进账户口径，贡献记为 None 而不是 0")

        self.assertEqual(res["asOf"], res["rows"][0]["lastDate"],
                         "asOf 取所有标的中最新的行情日期")
        self.assertEqual(res["baseDate"], "2026-03-16")
        self.assertEqual(res["note"], A.REVIEW_NOTE)
        self.assertEqual(res["horizons"], [5, 20])

    @staticmethod
    def _verdict(code, since):
        """按 CASES 的档位判断命中（用于手工复算命中率，不读被测代码的判定结果）。"""
        action = {c: a for c, a, _, _, _, _ in TestReviewSummary.CASES}[code]
        ret = since[code]
        if action in ("buy", "add"):
            return "hit" if ret > 0 else "miss"
        return "hit" if ret < 0 else "miss"

    def test_rates_are_none_when_nothing_is_gradeable(self):
        """全中性 / 全无数据时命中率返回 None 而不是 0——否则会显示成「0% 命中」误导用户。"""
        bars = {("cn", "H1"): series(100.0, changes={5: 105.0})[0],
                ("cn", "H2"): series(100.0, changes={5: 94.0})[0]}
        neutral = merged_record([make_row("H1", action="hold", price=100.0, weight=0.1),
                                make_row("H2", action="watch", price=100.0, weight=0.2)],
                                ts=TS1)
        res = A.review(neutral, FakeBars(bars))
        self.assertEqual(res["summary"]["ready"], 0)
        self.assertIsNone(res["summary"]["hitRate"])
        self.assertIsNone(res["summary"]["bullHitRate"])
        self.assertIsNone(res["summary"]["bearHitRate"])
        self.assertEqual((res["summary"]["neutral"], res["summary"]["hits"]), (2, 0))
        # 中性行仍计入平均收益与仓位收益（它们是真金白银的持仓）
        self.assertIsNotNone(res["summary"]["avgReturn"])
        self.assertAlmostEqual(res["summary"]["totalWeight"], 0.3, delta=1e-6)

        nothing = merged_record([make_row("X", action="avoid", price=100.0)], ts=TS1)
        res2 = A.review(nothing, FakeBars({}))
        self.assertEqual(res2["summary"]["nodata"], 1)
        self.assertIsNone(res2["summary"]["hitRate"])
        self.assertIsNone(res2["summary"]["avgReturn"])
        self.assertEqual(res2["summary"]["accountReturn"], 0.0)
        self.assertIsNone(res2["summary"]["positionReturn"],
                          "一只都拿不到行情时仓位收益无意义 → None（不是 0.0）")
        self.assertIsNone(res2["asOf"])


# --------------------------------------------------------------------------- #
# 19. review：边界与健壮性
# --------------------------------------------------------------------------- #
class TestReviewRobustness(unittest.TestCase):

    def test_illegal_horizons_fall_back_to_default(self):
        """horizons 非法（0 / 非数字 / 空 / None）一律回退默认 (5,20)，绝不抛异常。

        为什么口径要这么宽松：horizons 来自前端查询串，用户可能传任何垃圾；复盘宁可
        退回默认窗口，也不能 500，或者返回一个缺 fwd 键的半成品。
        """
        bars, _ = series(100.0, changes={5: 105.0, 20: 96.0})
        feed = FakeBars({("cn", "600519"): bars})
        rec = merged_record([make_row("600519", action="buy", price=100.0)], ts=TS1)

        for bad in ((0,), ("x",), (), None, (-3,), ("abc", 0), (0, 0), ["", None]):
            res = A.review(rec, feed, horizons=bad)
            self.assertEqual(res["horizons"], [5, 20], "非法 horizons 必须回退默认窗口")
            self.assertEqual(set(res["rows"][0]["fwd"].keys()), HZ_KEYS)

        # 合法值要真的被采纳：排序 + 去重，单值/标量也接受
        res = A.review(rec, feed, horizons=(20, 5, 5))
        self.assertEqual(res["horizons"], [5, 20])
        self.assertEqual(set(res["rows"][0]["fwd"].keys()), {"5", "20"})
        res = A.review(rec, feed, horizons=(10,))
        self.assertEqual(res["horizons"], [10])
        self.assertEqual(set(res["rows"][0]["fwd"].keys()), {"10"})
        self.assertEqual(A.review(rec, feed, horizons=7)["horizons"], [7])

    def test_empty_record_returns_not_ok(self):
        """空记录 / rows 为空：返回 ok=False 且结构完整，不抛异常。"""
        feed = FakeBars({})
        for rec in ({}, None, [], {"rows": []}, {"createdDate": "2026-03-16"},
                    {"rows": [None, "x", 3]}):
            res = A.review(rec, feed)
            self.assertFalse(res["ok"], "没有可复盘标的时 ok=False，前端据此给空态")
            self.assertEqual(res["rows"], [])
            self.assertEqual(res["summary"], {})
            self.assertEqual(res["horizons"], [5, 20])
            self.assertIn("没有可复盘的标的", res["message"])
            self.assertTrue(json.dumps(res, allow_nan=False))

    def test_fetch_failures_are_isolated(self):
        """取数抛异常 / 返回空列表都不能让整条复盘失败，只把该只标成 nodata。"""
        bars, _ = series(100.0, changes={5: 105.0})
        rec = merged_record([make_row("600519", action="buy", price=100.0),
                             make_row("000001", action="add", price=10.0)], ts=TS1)

        def boom(market, code, period, limit):
            raise RuntimeError("网络超时")

        res = A.review(rec, boom)
        self.assertTrue(res["ok"], "单只失败不能拖垮整条记录")
        self.assertEqual([r["verdict"] for r in res["rows"]], ["nodata", "nodata"])
        self.assertTrue(all("行情获取失败" in r["note"] for r in res["rows"]))
        self.assertEqual(res["summary"]["nodata"], 2)

        # 返回 None / 空列表：同样只影响自己那一行
        def empty(market, code, period, limit):
            return None if code == "600519" else []

        res2 = A.review(rec, empty)
        self.assertEqual([r["verdict"] for r in res2["rows"]], ["nodata", "nodata"])

        # 全是脏K线（缺 close / 非 dict / 价格为 0）：清洗后无有效K线 → nodata
        dirty = FakeBars({("cn", "600519"): [{"t": "2026-03-16", "close": 0},
                                            {"t": "2026-03-17"},
                                            "not-a-bar"]})
        res3 = A.review(rec, dirty)
        self.assertEqual([r["verdict"] for r in res3["rows"]], ["nodata", "nodata"])

        # 混入可用行情：失败的那只 nodata，可用的那只照常判定
        mixed = FakeBars({("cn", "000001"): bars}, fail=("600519",))
        res4 = A.review(rec, mixed)
        self.assertEqual([r["verdict"] for r in res4["rows"]], ["nodata", "hit"])
        self.assertEqual((res4["summary"]["hits"], res4["summary"]["nodata"]), (1, 1))

    def test_parallel_and_serial_agree(self):
        """多线程复盘（默认 max_workers=4）与单线程结果完全一致，顺序仍是 rows 顺序。"""
        book, rows = {}, []
        for i in range(6):
            code = "60000%d" % i
            book[("cn", code)] = series(100.0, changes={5: 100.0 + i})[0]
            rows.append(make_row(code, action="buy" if i % 2 == 0 else "sell",
                                 price=100.0))
        rec = merged_record(rows, ts=TS1)

        serial = A.review(rec, FakeBars(book), max_workers=1)
        parallel = A.review(rec, FakeBars(book), max_workers=4)
        self.assertEqual([r["code"] for r in serial["rows"]], [r["code"] for r in rows])
        self.assertEqual([r["code"] for r in parallel["rows"]], [r["code"] for r in rows])
        self.assertEqual(serial["rows"], parallel["rows"])
        self.assertEqual(serial["summary"], parallel["summary"])

    def test_result_is_json_safe(self):
        """复盘结果必须能 json.dumps(allow_nan=False)：任何 NaN/inf 都会让前端解析直接失败。"""
        bars, _ = series(100.0, changes={5: 105.0, TAIL: 103.0})
        rec = merged_record([make_row("600519", action="buy", price=100.0, weight=0.25),
                             make_row("000001", action="hold", price=11.32, weight=None)],
                            ts=TS1)
        res = A.review(rec, FakeBars({("cn", "600519"): bars}))
        text = json.dumps(res, ensure_ascii=False, allow_nan=False)
        self.assertIn("hitRate", text)
        self.assertIn('"verdict"', text)
        self.assertNotIn("NaN", text)
        self.assertNotIn("Infinity", text)

        # 权重为 None 的行不能让仓位收益变成 NaN
        res2 = A.review(rec, FakeBars({("cn", "600519"): bars, ("cn", "000001"): bars}))
        json.dumps(res2, allow_nan=False)
        self.assertIsNone(res2["rows"][1]["weight"])
        self.assertAlmostEqual(res2["summary"]["totalWeight"], 0.25, delta=1e-6)


# --------------------------------------------------------------------------- #
# 20. 回归：本文件编写过程中实测到、已修复的两处不一致
# --------------------------------------------------------------------------- #
class TestFixedRegressions(unittest.TestCase):
    """两处「文案与实现互相矛盾」的缺陷，现已修复，用回归测试锁住。

    这类用例描述的是「期望的正确行为」：一旦有人把修好的逻辑改回去，套件立刻变红。
    """

    def test_record_note_number_matches_actual_keep(self):
        """保留条数的文案必须与真实生效的上限一致（文案即口径）。

        曾经 ``RECORD_NOTE`` 硬编码「保留最近 300 条未置顶记录」，而
        ``storage.ADVISOR_KEEP`` 与 ``server.advisor_keep()`` 实际都是 500 ——
        这段文案会随 ``/api/advisor/history``、``/api/advisor/review`` 返回给前端展示，
        等于对用户谎报保留策略。现改为由 ``record_note(keep)`` 拼装生效值，
        基础文案里不再出现任何数字。
        """
        self.assertIsNone(re.search(r"\d+\s*条未置顶记录", A.RECORD_NOTE),
                          "基础文案不应再写死条数：%s" % A.RECORD_NOTE)
        self.assertIn(str(Store.ADVISOR_KEEP), A.record_note(Store.ADVISOR_KEEP))
        self.assertIn("500", A.record_note(500))
        self.assertIn("120", A.record_note(120))
        # 非法 keep 不应抛异常，退回不带数字的说明
        self.assertEqual(A.record_note("x"), A.RECORD_NOTE)
        self.assertEqual(A.record_note(None), A.RECORD_NOTE)

    def test_resave_does_not_change_created_at(self):
        """同一 id 重写记录不应改变 createdAt / createdDate。

        曾经 advisor_runs 的 ON CONFLICT 写的是 ``created_at = excluded.created_at``，
        而 runs 用的是 ``COALESCE`` 并明确注释「创建时间只在首次写入时确定」。后果：
        · record_id() 把毫秒时间戳编进 id，重写后 id 里的时间戳与 createdAt 互相矛盾；
        · review 的基准日取自 createdDate，重写会把「保存日」悄悄挪到重写当天，
          复盘的起点与收益口径随之漂移（记录内容却不一定变）；
        · 列表按 created_at 倒序，记录会莫名跳位。
        """
        store = Store(":memory:")
        self.addCleanup(store.close)
        rows = [make_row("600519", action="buy", price=100.0)]
        rid = store.save_advisor_run(make_record(rows, ts=TS1, note="第一次"))
        # 同一 id 重写（服务端重试 / 回填会走到这条路径），时间戳不同
        store.save_advisor_run(make_record(rows, ts=TS2, rid=rid, note="第一次"))

        got = store.get_advisor_run(rid)
        self.assertEqual(got["createdAt"], TS1, "重写不得改变记录快照的保存时间")
        self.assertEqual(got["createdDate"], "2026-03-16")
        self.assertEqual(rid.split("-")[1], str(got["createdAt"]),
                         "id 内嵌的时间戳必须与 createdAt 一致，否则排序与复盘基准日会互相矛盾")
        # 重写仍应刷新内容字段（只是不动创建时间）
        self.assertEqual(got["rows"][0]["action"], "buy")
        self.assertEqual(got["note"], "第一次")


if __name__ == "__main__":
    unittest.main(verbosity=2)
