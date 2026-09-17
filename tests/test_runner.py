# -*- coding: utf-8 -*-
"""策略跟踪引擎 · 游标推进的回归测试（tests/test_runner.py）

为什么单独为「游标」写测试
--------------------------
这是一个**静默冻结**类缺陷的锚点，不写测试就一定会复发。2026-09-17 的实际事故：
四个跟踪任务全部显示空仓，而引擎 `tickCount` 涨到 3162、`lastBarTime` 也一直在更新，
界面上完全看不出异常。根因是推进游标用了**位置索引**：

    引擎每次固定取 800 根（`fetch_bars(..., 800)`）→ 拿到的是滑动窗口；
    首次取到的根数比 800 多 1（801）→ `barProcessed` 被推到 801；
    此后窗口只有 800 根 → `start_i(801) >= complete_n(≤800)` 永远成立 →
    **replay 分支再也不进入**，任务从创建起就冻结在当时的持仓状态（恰好都是空仓）。

修复方向是把游标换成**日期口径**（最后一根已处理的已收盘K线），并给旧任务做一次迁移。
本文件把三条关键性质钉住：
1. 滑动窗口下（含「首次根数多于窗口」这一触发条件）游标必须继续推进；
2. 旧任务（只有位置索引、已冻结）必须能恢复，且不重复计数；
3. 没有更新的K线时不得越界推进（否则会重复处理同一根K线、凭空造出交易）。
"""

import datetime
import sys
import unittest

sys.path.insert(0, ".")

import core.indicators as I  # noqa: E402
from core.runner import Runner, _lag_days  # noqa: E402


def _trading_days(end="2026-08-31", count=806):
    """向前取 count 个交易日（跳过周六周日），返回升序的 YYYY-MM-DD 列表。

    不用真实交易日历：本文件只验证「按日期推进」的机制，节假日不影响结论，
    而且写死日期才能让用例完全确定性（不依赖今天是什么日子）。
    """
    out = []
    day = datetime.date.fromisoformat(end)
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day -= datetime.timedelta(days=1)
    return list(reversed(out))


def _bars(dates, closes):
    return [{"t": d, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1000000}
            for d, c in zip(dates, closes)]


def _closes(n, trend="flat"):
    """平盘 + 尾部 V 型：足以在尾部制造一次均线金叉（用于验证「新K线被真的消化」）。"""
    out = [100.0] * n
    tail = min(25, n)
    for k in range(tail):
        i = n - tail + k
        if k < 15:
            out[i] = 100.0 - 0.8 * (k + 1)      # 先跌 15 根 → MA5 落到 MA20 下方
        else:
            out[i] = 88.0 + 1.6 * (k - 14)      # 再涨 10 根 → 制造金叉
    return out


class _Feed:
    """可控行情注入：**固定长度滑动窗口**，并刻意让首次返回的根数比窗口多 1。

    这正是事故的触发条件（801 > 800）。把它做成默认行为，是为了让「滑动窗口 + 首次多一根」
    这个组合一直被测试覆盖，而不是只在生产环境偶发。
    """

    def __init__(self, bars, window=800, first_full=True):
        self.all = bars
        self.window = window
        self.first_full = first_full
        self.calls = 0

    def __call__(self, market, code, period, limit):
        self.calls += 1
        want = self.window
        if self.first_full and self.calls == 1:
            want = min(len(self.all), self.window + 1)   # 首次多一根：801
        return self.all[-want:]


class CursorCase(unittest.TestCase):
    def setUp(self):
        self.dates = _trading_days()
        self.closes = _closes(len(self.dates))
        self.bars = _bars(self.dates, self.closes)
        self.feed = _Feed(self.bars)
        self.r = Runner(":memory:", fetch_bars=self.feed, fetch_quote=lambda *a: {},
                        fetch_orderbook=lambda *a: {})
        self.run = self.r.create_run({
            "market": "cn", "code": "600667", "name": "测试标的", "strategy": "maCross",
            "params": {"fast": 5, "slow": 20}, "period": "day", "fq": 1,
            "initial": 10000.0, "lot": 100, "startDate": self.dates[730],
            "stopLoss": 8.0, "takeProfit": 10.0, "targetDays": 90,
        })


class TestCursorAdvances(CursorCase):
    def test_sliding_window_keeps_advancing(self):
        """核心回归：滑动窗口 + 首次多一根，游标仍必须随新K线推进。

        修复前的行为：第一次 tick 后 `barProcessed = 801`，此后
        `start_i >= complete_n` 恒成立 → 第二个 tick 起一根K线都不消化，
        `lastBarDate` 永远停在首次的最后一根（生产事故中就是「全天空仓」）。
        """
        self.r.tick_run(self.run)
        first_cursor = self.run.get("lastBarDate")
        self.assertEqual(first_cursor, self.dates[-1], "首次 tick 应把游标推到最后一根K线")

        # 追加两根新K线，窗口整体后移（长度仍为 800 —— 滑动窗口的本质）
        extra_dates = _trading_days(end="2026-09-02", count=2)
        self.dates = self.dates + extra_dates
        self.closes = self.closes + [self.closes[-1] * 1.01, self.closes[-1] * 1.02]
        self.bars = _bars(self.dates, self.closes)
        self.feed.all = self.bars

        self.r.tick_run(self.run)
        self.assertEqual(self.run.get("lastBarDate"), self.dates[-1],
                         "新增K线后游标必须推进到最新一根（修复前会停在原地）")
        self.assertGreater(self.feed.calls, 1)
        # 位置索引不得越界：它现在只是「窗口内的位置」，不能大于窗口长度
        self.assertLessEqual(int(self.run.get("barProcessed") or 0), len(self.bars))

    def test_cursor_is_a_date_not_an_index(self):
        """游标必须是日期：窗口滑动后位置索引会指到别的K线，日期不会。"""
        self.r.tick_run(self.run)
        self.assertRegex(str(self.run.get("lastBarDate") or ""), r"^\d{4}-\d{2}-\d{2}$")


class TestCursorStart(CursorCase):
    """`_cursor_start` 的三条性质（直接测，不依赖网络与真实行情）"""

    def test_legacy_frozen_run_recovers(self):
        """旧任务迁移：只有位置索引且已冻结（barProcessed 大于窗口长度）时必须能恢复。

        迁移口径 = max(创建日, 最后执行日, 各笔交易的进出场日)，该日期之后的K线在
        冻结事故中确实从未被处理过；而重放「无事件区间」是幂等的，所以不会重复计数。
        """
        legacy = {
            "barProcessed": 801, "createdDate": self.dates[-8], "executedOnDate": self.dates[-20],
            "trades": [{"in_date": self.dates[-30], "out_date": self.dates[-25]}],
        }
        start = self.r._cursor_start(legacy, self.bars, len(self.bars))
        self.assertTrue(legacy.get("cursorMigrated"), "应标记为已迁移，便于排查")
        self.assertEqual(legacy.get("lastBarDate"), self.dates[-8], "游标取创建日")
        self.assertEqual(self.bars[start]["t"], self.dates[-7], "应从创建日的下一根开始重放")

    def test_no_new_bars_does_not_advance(self):
        """没有更新的K线时返回 complete_n（不得越界重放，否则会重复造出交易）"""
        run = {"lastBarDate": self.dates[-1]}
        start = self.r._cursor_start(run, self.bars, len(self.bars))
        self.assertEqual(start, len(self.bars))

    def test_date_cursor_picks_first_newer_bar(self):
        run = {"lastBarDate": self.dates[-3]}
        start = self.r._cursor_start(run, self.bars, len(self.bars))
        self.assertEqual(self.bars[start]["t"], self.dates[-2])

    def test_positional_fallback_never_exceeds_window(self):
        """全新任务（无日期游标也无创建日）时位置索引必须夹到窗口内，不得越界"""
        run = {"barProcessed": 99999, "trades": []}
        start = self.r._cursor_start(run, self.bars, len(self.bars))
        self.assertLessEqual(start, len(self.bars))


class TestVisibility(CursorCase):
    """把「推进到哪一天」做成界面上看得见的字段（事故中完全看不出来）"""

    def test_overview_exposes_cursor_and_lag(self):
        self.r.tick_run(self.run)
        rows = self.r.overview()["rows"]
        self.assertTrue(rows)
        row = rows[0]
        for key in ("lastBarDate", "availableTo", "lagDays", "stalled"):
            self.assertIn(key, row, "概览必须暴露推进健康度字段：%s" % key)
        self.assertEqual(row["lastBarDate"], self.dates[-1])
        self.assertEqual(row["lagDays"], 0, "刚推进完不应显示滞后")
        self.assertFalse(row["stalled"])

    def test_stalled_flag_triggers_on_large_lag(self):
        """滞后 ≥5 天才报警：盘中「正在形成的那根」天然差 1 天，不能天天误报"""
        self.assertEqual(_lag_days("2026-09-01", "2026-09-02"), 1)
        self.assertGreaterEqual(_lag_days("2026-09-01", "2026-09-10"), 5)
        self.assertIsNone(_lag_days(None, "2026-09-10"))
        self.assertIsNone(_lag_days("坏数据", "2026-09-10"))


class TestNoDuplicateTrades(CursorCase):
    def test_repeat_tick_does_not_duplicate(self):
        """同一根K线不得被处理两次：连续两次 tick 不应产生重复交易/信号。

        这是「游标退回」类修复最容易引入的新问题（宁可冻结也不能重复造交易）。
        """
        self.r.tick_run(self.run)
        before = len(self.r.detail(self.run["id"]).get("trades") or [])
        self.r.tick_run(self.run)
        after = len(self.r.detail(self.run["id"]).get("trades") or [])
        self.assertEqual(before, after, "重复 tick 不得新增交易")


if __name__ == "__main__":
    unittest.main(verbosity=2)
