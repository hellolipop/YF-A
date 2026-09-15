#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core.storage / core.logs 的单元测试。

覆盖范围
--------
1. 建库自检：WAL、foreign_keys、六张表、schema 版本写入 meta；
2. meta 读写与默认值；
3. 任务增删查改（runs）：嵌套字段 JSON 往返、列表排序、更新不覆盖创建时间、级联删除；
4. 交易（trades）：phase / signal_price / fill_price / fee / slippage 落库、字段别名兼容、
   limit 与 phase 过滤、replace_trades 整体替换、外键约束；
5. 权益曲线（equity）与信号（signals）追加与查询顺序；
6. 结构迁移：v1 库 → migrate() 升到 v2（补出 fee / slippage 列、已有数据不丢、可重复执行）；
7. 数据迁移：strategy_runs.json → migrate_from_json()（明细拆分入库、信号顺序归一）；
8. 并发读写：多线程同时写任务/交易/权益/信号 + 多线程同时查询，校验无异常且数量正确；
9. 结构化日志（logs）：JSONL 落盘、内存环形缓冲 500 条上限、ring(limit, level) 过滤、
   SQLite logs 表 sink 双写。

运行方式::

    python3 -m unittest discover -s tests -v
    # 或直接
    python3 tests/test_storage.py
"""

import json
import os
import sys
import tempfile
import threading
import unittest

# 让测试既能在 stock-terminal/ 下跑，也能在仓库根目录下跑
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import logs as logs_mod                        # noqa: E402
from core.logs import Logger, RingBuffer, read_jsonl     # noqa: E402
from core.storage import SCHEMA_VERSION, Store, now_ms   # noqa: E402

try:      # 与同目录的绩效指标库联动（该模块由另一个任务维护，缺失时跳过相关断言）
    from core.metrics import summarize as metrics_summarize
except Exception:  # noqa: BLE001
    metrics_summarize = None

RUN_ID = "s1712345678"


def sample_run(rid=RUN_ID, **over):
    """构造一个与 engine.py 结构一致的任务（含各种嵌套字段）"""
    run = {
        "id": rid,
        "createdAt": 1712345678000,
        "createdDate": "2026-04-06",
        "market": "cn",
        "code": "600519",
        "name": "贵州茅台",
        "strategy": "maCross",
        "strategyName": "双均线交叉",
        "params": {"fast": 5.0, "slow": 20.0},       # 嵌套 → JSON 列
        "period": "day",
        "fq": 1,
        "initial": 100000.0,
        "lot": 100,
        "fee": 0.0003,
        "slippage": 0.001,
        "stopLoss": 8.0,
        "takeProfit": 0.0,
        "targetDays": 90,
        "startDate": "2026-01-05",
        "status": "running",
        "mode": "paper",
        "barProcessed": 62,
        "pending": {"side": "buy", "t": "2026-04-03"},   # 嵌套 → JSON 列
        "cash": 12345.6,
        "qty": 0.0,
        "monthly": {"2026-03": {"realized": 812.5, "trades": 2, "wins": 1}},
        "revisions": [{"ts": 1712300000000, "reset": False, "fields": {"name": {"from": "600519", "to": "贵州茅台"}}}],
        "benchmarkStart": 1600.0,
        "lastPrice": 1655.0,
        "note": "观察期测试",
    }
    run.update(over)
    return run


def sample_trade(**over):
    """engine.py 风格的逐笔交易（驼峰命名），可覆盖任意字段"""
    trade = {
        "inDate": "2026-03-12", "inPrice": 1500.0, "qty": 100,
        "outDate": "2026-03-20", "outPrice": 1560.0, "pnl": 5700.0,
        "pnlPct": 3.4, "bars": 6, "reason": "信号", "phase": "backfill",
    }
    trade.update(over)
    return trade


class StoreTestBase(unittest.TestCase):
    """公共脚手架：每个用例一个独立临时目录 + 独立库文件"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "strategy.db")

    def tearDown(self):
        try:
            self._tmp.cleanup()
        except OSError:
            pass

    def open_store(self, path=None, **kw):
        store = Store(path or self.db, **kw)
        self.addCleanup(store.close)
        return store


# --------------------------------------------------------------------------- #
# 1. 建库自检
# --------------------------------------------------------------------------- #

class TestSchema(StoreTestBase):

    def test_wal_and_foreign_keys(self):
        """建库即开启 WAL 与 foreign_keys"""
        store = self.open_store()
        self.assertEqual(store.journal_mode(), "wal")
        self.assertTrue(store.foreign_keys_enabled())

    def test_tables_created(self):
        """六张表齐备，且 schema 版本写进 meta"""
        store = self.open_store()
        rows = store._conn().execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        names = {r["name"] for r in rows}
        for table in ("runs", "trades", "equity", "signals", "logs", "meta"):
            self.assertIn(table, names)
        self.assertEqual(store.schema_version(), SCHEMA_VERSION)
        self.assertEqual(store.meta_get("schema_version"), SCHEMA_VERSION)

    def test_trades_required_columns(self):
        """trades 表必须包含 phase / signal_price / fill_price / fee / slippage"""
        store = self.open_store()
        cols = {r["name"] for r in store._conn().execute("PRAGMA table_info(trades)")}
        for col in ("phase", "signal_price", "fill_price", "fee", "slippage"):
            self.assertIn(col, cols)

    def test_init_schema_idempotent(self):
        """重复建表不报错、不改变版本"""
        store = self.open_store()
        self.assertEqual(store.init_schema(), SCHEMA_VERSION)
        self.assertEqual(store.init_schema(), SCHEMA_VERSION)
        self.assertEqual(store.schema_version(), SCHEMA_VERSION)

    def test_meta_get_set(self):
        """meta 读写：任意 JSON 值往返，缺失走默认值"""
        store = self.open_store()
        self.assertEqual(store.meta_get("nothing", "fallback"), "fallback")
        store.meta_set("engine_interval", 60)
        store.meta_set("note", {"hello": "世界"})
        self.assertEqual(store.meta_get("engine_interval"), 60)
        self.assertEqual(store.meta_get("note"), {"hello": "世界"})
        self.assertEqual(store.meta_all()["engine_interval"], 60)

    def test_context_manager(self):
        """支持 with 语法（退出即关闭连接）"""
        with Store(self.db) as store:
            store.upsert_run(sample_run())
        self.assertEqual(Store(self.db).get_run(RUN_ID)["code"], "600519")


# --------------------------------------------------------------------------- #
# 2. 任务增删查改
# --------------------------------------------------------------------------- #

class TestRuns(StoreTestBase):

    def test_upsert_get_roundtrip(self):
        """嵌套字段（params / pending / monthly / revisions）JSON 往返无损"""
        store = self.open_store()
        run = sample_run()
        self.assertEqual(store.upsert_run(run), RUN_ID)
        got = store.get_run(RUN_ID)
        self.assertEqual(got, run)
        self.assertEqual(got["params"], {"fast": 5.0, "slow": 20.0})
        self.assertEqual(got["monthly"]["2026-03"]["wins"], 1)
        self.assertEqual(got["revisions"][0]["fields"]["name"]["to"], "贵州茅台")

    def test_get_missing_returns_none(self):
        """查不到的任务返回 None"""
        store = self.open_store()
        self.assertIsNone(store.get_run("nope"))

    def test_upsert_requires_id(self):
        """缺少 id 的任务直接拒绝"""
        store = self.open_store()
        with self.assertRaises(ValueError):
            store.upsert_run({"code": "600519"})

    def test_upsert_update_keeps_created_at(self):
        """更新任务：字段被覆盖，创建时间保持不变"""
        store = self.open_store()
        store.upsert_run(sample_run())
        store.upsert_run(sample_run(status="paused", name="改名了"))
        got = store.get_run(RUN_ID)
        self.assertEqual(got["status"], "paused")
        self.assertEqual(got["name"], "改名了")
        self.assertEqual(got["createdAt"], 1712345678000)
        self.assertEqual(store.get_run_column(RUN_ID, "status"), "paused")
        self.assertEqual(store.counts()["runs"], 1)

    def test_list_runs_order_and_filter(self):
        """列表按创建时间倒序，支持 status / market 过滤与 limit"""
        store = self.open_store()
        store.upsert_run(sample_run("r1", createdAt=1000, market="cn", status="running"))
        store.upsert_run(sample_run("r2", createdAt=3000, market="us", status="paused"))
        store.upsert_run(sample_run("r3", createdAt=2000, market="cn", status="running"))
        self.assertEqual([r["id"] for r in store.list_runs()], ["r2", "r3", "r1"])
        self.assertEqual([r["id"] for r in store.list_runs(limit=2)], ["r2", "r3"])
        self.assertEqual([r["id"] for r in store.list_runs(status="running")], ["r3", "r1"])
        self.assertEqual([r["id"] for r in store.list_runs(market="us")], ["r2"])

    def test_delete_run_cascade(self):
        """删除任务时交易 / 权益 / 信号级联删除"""
        store = self.open_store()
        store.upsert_run(sample_run())
        store.append_trade(RUN_ID, sample_trade())
        store.append_equity(RUN_ID, {"t": "2026-03-20", "v": 105700.0})
        store.append_signal(RUN_ID, {"t": "2026-03-12", "side": "buy", "price": 1500.0})
        self.assertTrue(store.delete_run(RUN_ID))
        self.assertFalse(store.delete_run(RUN_ID))
        self.assertIsNone(store.get_run(RUN_ID))
        counts = store.counts()
        self.assertEqual((counts["runs"], counts["trades"], counts["equity"], counts["signals"]),
                         (0, 0, 0, 0))


# --------------------------------------------------------------------------- #
# 3. 交易明细
# --------------------------------------------------------------------------- #

class TestTrades(StoreTestBase):

    def setUp(self):
        super().setUp()
        self.store = self.open_store()
        self.store.upsert_run(sample_run())

    def test_append_and_required_fields(self):
        """落库后 phase / signal_price / fill_price / fee / slippage 均可读"""
        self.store.append_trade(RUN_ID, {
            "side": "buy", "phase": "live", "t": "2026-04-08",
            "signal_price": 1655.0, "fill_price": 1656.66, "fee": 496.998,
            "slippage": 0.001, "qty": 100, "reason": "信号",
        })
        trade = self.store.list_trades(RUN_ID)[0]
        self.assertEqual(trade["phase"], "live")
        self.assertEqual(trade["signal_price"], 1655.0)
        self.assertAlmostEqual(trade["fill_price"], 1656.66)
        self.assertAlmostEqual(trade["fee"], 496.998)
        self.assertEqual(trade["slippage"], 0.001)

    def test_engine_camel_case_aliases(self):
        """engine.py 的驼峰字段能被正确映射（并推导成交价 / 方向）"""
        self.store.append_trade(RUN_ID, sample_trade())
        trade = self.store.list_trades(RUN_ID)[0]
        self.assertEqual(trade["side"], "sell")
        self.assertEqual(trade["in_date"], "2026-03-12")
        self.assertEqual(trade["out_date"], "2026-03-20")
        self.assertAlmostEqual(trade["in_price"], 1500.0)
        self.assertAlmostEqual(trade["fill_price"], 1560.0)   # 卖出 → 取 outPrice
        self.assertAlmostEqual(trade["pnl"], 5700.0)
        self.assertEqual(trade["bars"], 6)
        self.assertEqual(trade["phase"], "backfill")
        self.assertEqual(trade["fee"], 0.0)                   # 未给成本字段时按 0 处理
        self.assertEqual(trade["slippage"], 0.0)

    def test_extra_fields_kept(self):
        """未映射的自定义字段进 extra，查询时原样带回"""
        self.store.append_trade(RUN_ID, sample_trade(tag="回测", score=91))
        trade = self.store.list_trades(RUN_ID)[0]
        self.assertEqual(trade["tag"], "回测")
        self.assertEqual(trade["score"], 91)

    def test_order_limit_and_phase_filter(self):
        """写入顺序即查询顺序；limit 取最近 N 笔；phase 可过滤"""
        for i in range(5):
            self.store.append_trade(RUN_ID, sample_trade(
                outDate="2026-03-%02d" % (10 + i), qty=100 + i,
                phase="backfill" if i < 3 else "live"))
        trades = self.store.list_trades(RUN_ID)
        self.assertEqual([t["qty"] for t in trades], [100.0, 101.0, 102.0, 103.0, 104.0])
        self.assertEqual([t["seq"] for t in trades], [0, 1, 2, 3, 4])
        recent = self.store.list_trades(RUN_ID, limit=2)
        self.assertEqual([t["qty"] for t in recent], [103.0, 104.0])
        live = self.store.list_trades(RUN_ID, phase="live")
        self.assertEqual([t["qty"] for t in live], [103.0, 104.0])

    def test_replace_trades(self):
        """整体替换：旧明细清空、序号重排"""
        self.store.append_trade(RUN_ID, sample_trade())
        self.store.append_trade(RUN_ID, sample_trade())
        n = self.store.replace_trades(RUN_ID, [
            sample_trade(qty=200, phase="live"),
            sample_trade(qty=300, phase="live"),
            sample_trade(qty=400, phase="live"),
        ])
        self.assertEqual(n, 3)
        trades = self.store.list_trades(RUN_ID)
        self.assertEqual([t["seq"] for t in trades], [0, 1, 2])
        self.assertEqual([t["qty"] for t in trades], [200.0, 300.0, 400.0])
        self.assertEqual(self.store.replace_trades(RUN_ID, []), 0)
        self.assertEqual(self.store.list_trades(RUN_ID), [])

    def test_legacy_view_feeds_metrics(self):
        """legacy=True 的驼峰投影可被 core.metrics.summarize 直接消费（存储 → 指标链路）"""
        self.store.append_trade(RUN_ID, sample_trade(
            fee=93.6, slippage=15.0, qty=100, outPrice=1560.0, pnl=5700.0))
        self.store.append_trade(RUN_ID, sample_trade(
            outDate="2026-03-25", outPrice=1488.0, pnl=-1200.0, phase="live",
            fee=89.3, slippage=14.9))
        row = self.store.list_trades(RUN_ID, legacy=True)[0]
        for key in ("inDate", "inPrice", "outDate", "outPrice", "pnlPct"):
            self.assertIn(key, row)
        self.assertEqual(row["inDate"], "2026-03-12")

        if metrics_summarize is None:      # core.metrics 不可用时跳过联动校验
            self.skipTest("core.metrics 不可用")
        self.store.append_equity(RUN_ID, {"t": "2026-03-19", "v": 100000.0,
                                          "close": 1500.0, "position": True})
        self.store.append_equity(RUN_ID, {"t": "2026-03-20", "v": 105700.0,
                                          "close": 1560.0, "position": False})
        stats = metrics_summarize(
            self.store.list_equity(RUN_ID),
            self.store.list_trades(RUN_ID, legacy=True),
            initial=100000.0,
        )
        self.assertEqual(stats["trades"], 2)                 # 两笔已实现成交
        self.assertAlmostEqual(stats["win_rate"], 0.5)        # 一胜一负
        self.assertAlmostEqual(stats["fee_total"], 93.6 + 89.3, places=6)

    def test_foreign_key_guard(self):
        """往不存在的任务里写交易会被拦截（错误信息可读）"""
        with self.assertRaises(ValueError):
            self.store.append_trade("ghost", sample_trade())
        # 直接绕过 `Store` 的校验也会被数据库外键挡住
        with self.assertRaises(Exception):
            with self.store._tx() as conn:
                conn.execute("INSERT INTO trades (run_id, seq) VALUES ('ghost', 0)")


# --------------------------------------------------------------------------- #
# 4. 权益曲线与信号
# --------------------------------------------------------------------------- #

class TestEquityAndSignals(StoreTestBase):

    def setUp(self):
        super().setUp()
        self.store = self.open_store()
        self.store.upsert_run(sample_run())

    def test_equity_append_list(self):
        """权益点按时序追加；limit 取最近 N 个，仍按升序返回"""
        for i in range(6):
            self.store.append_equity(RUN_ID, {
                "t": "2026-03-%02d" % (10 + i),
                "v": 100000.0 + i * 100,
                "close": 1600.0 + i,
                "position": i % 2 == 0,
            })
        curve = self.store.list_equity(RUN_ID)
        self.assertEqual(len(curve), 6)
        self.assertEqual([p["t"] for p in curve], sorted(p["t"] for p in curve))
        self.assertTrue(curve[0]["position"])
        self.assertFalse(curve[1]["position"])
        recent = self.store.list_equity(RUN_ID, limit=2)
        self.assertEqual([p["t"] for p in recent], ["2026-03-14", "2026-03-15"])
        self.assertEqual(self.store.list_equity("ghost"), [])

    def test_signal_append_list_newest_first(self):
        """信号默认最新在前，limit 取最近 N 条"""
        for i in range(4):
            self.store.append_signal(RUN_ID, {
                "t": "2026-04-%02d" % (10 + i), "side": "buy" if i % 2 == 0 else "sell",
                "price": 1650.0 + i, "phase": "live", "hadPosition": False,
            })
        sigs = self.store.list_signals(RUN_ID, None)
        self.assertEqual([s["t"] for s in sigs],
                         ["2026-04-13", "2026-04-12", "2026-04-11", "2026-04-10"])
        self.assertEqual(len(self.store.list_signals(RUN_ID, 2)), 2)
        self.assertEqual(self.store.list_signals(RUN_ID, 2)[0]["t"], "2026-04-13")

    def test_signal_skipped_flag(self):
        """因资金不足被跳过的信号带 skipped 标记与 note"""
        self.store.append_signal(RUN_ID, {
            "t": "2026-04-14", "side": "buy", "price": 1655.0, "phase": "live",
            "hadPosition": False, "skipped": True, "note": "资金不足（需≥165500）",
        })
        sig = self.store.list_signals(RUN_ID, 1)[0]
        self.assertTrue(sig["skipped"])
        self.assertIn("资金不足", sig["note"])


# --------------------------------------------------------------------------- #
# 5. 结构迁移
# --------------------------------------------------------------------------- #

class TestSchemaMigration(StoreTestBase):

    def _build_v1(self):
        """造一个 v1 老库（trades 还没有 fee / slippage 列）并塞入历史数据"""
        store = Store(self.db, init=False)
        self.addCleanup(store.close)
        self.assertEqual(store.init_schema(version=1), 1)
        self.assertEqual(store.schema_version(), 1)
        store.upsert_run(sample_run())
        with store._tx() as conn:
            conn.execute(
                "INSERT INTO trades (run_id, seq, side, phase, signal_price, fill_price, "
                "in_date, in_price, out_date, out_price, qty, pnl, pnl_pct, bars, reason) "
                "VALUES (?, 0, 'sell', 'backfill', 1500.0, 1560.0, '2026-03-12', 1500.0, "
                "'2026-03-20', 1560.0, 100, 5700.0, 3.4, 6, '信号')", (RUN_ID,))
            conn.execute(
                "INSERT INTO equity (run_id, seq, t, v, close, position) "
                "VALUES (?, 0, '2026-03-20', 105700.0, 1560.0, 0)", (RUN_ID,))
        return store

    def test_v1_has_no_cost_columns(self):
        """v1 结构里确实没有 fee / slippage（迁移前的对照）"""
        store = self._build_v1()
        cols = {r["name"] for r in store._conn().execute("PRAGMA table_info(trades)")}
        self.assertNotIn("fee", cols)
        self.assertNotIn("slippage", cols)

    def test_migrate_adds_columns_and_keeps_data(self):
        """migrate() 补出成本列，历史数据不受影响，版本号前进到最新"""
        store = self._build_v1()
        result = store.migrate()
        self.assertEqual(result["from"], 1)
        self.assertEqual(result["to"], SCHEMA_VERSION)
        self.assertEqual(result["applied"], [2])
        self.assertEqual(store.schema_version(), SCHEMA_VERSION)

        cols = {r["name"] for r in store._conn().execute("PRAGMA table_info(trades)")}
        for col in ("phase", "signal_price", "fill_price", "fee", "slippage"):
            self.assertIn(col, cols)

        trade = store.list_trades(RUN_ID)[0]
        self.assertEqual(trade["phase"], "backfill")
        self.assertAlmostEqual(trade["signal_price"], 1500.0)
        self.assertAlmostEqual(trade["out_price"], 1560.0)
        self.assertEqual(trade["fee"], 0.0)        # 老数据默认补 0
        self.assertEqual(trade["slippage"], 0.0)
        self.assertEqual(store.get_run(RUN_ID)["code"], "600519")
        self.assertEqual(len(store.list_equity(RUN_ID)), 1)

    def test_migrate_is_idempotent(self):
        """已是新版时再迁移是空操作，可以安全重复调用"""
        store = self._build_v1()
        store.migrate()
        again = store.migrate()
        self.assertEqual(again["applied"], [])
        self.assertEqual(again["to"], SCHEMA_VERSION)
        self.assertIsNotNone(store.meta_get("schema_migrated_at"))

    def test_fresh_store_migrate_is_noop(self):
        """全新库建库即最新版，migrate() 无步骤可做"""
        store = self.open_store()
        self.assertEqual(store.migrate(), {"from": SCHEMA_VERSION, "to": SCHEMA_VERSION, "applied": []})

    def test_unknown_version_rejected(self):
        """未知版本号会被拒绝（避免误建结构）"""
        store = self.open_store()
        with self.assertRaises(ValueError):
            store.init_schema(version=99)
        with self.assertRaises(ValueError):
            store.migrate(target=99)


# --------------------------------------------------------------------------- #
# 6. 从 strategy_runs.json 迁移
# --------------------------------------------------------------------------- #

class TestJsonMigration(StoreTestBase):

    def _write_legacy_json(self):
        """按 engine.py 的落盘格式造一份旧数据（信号按「最新在前」存）"""
        legacy = {
            "version": 1,
            "savedAt": 1712345999.0,
            "runs": [
                dict(sample_run("legacy1"), trades=[
                    sample_trade(), sample_trade(outDate="2026-03-25", pnl=-1200.0, phase="live"),
                ], equity=[
                    {"t": "2026-03-19", "v": 100000.0, "close": 1500.0, "position": True},
                    {"t": "2026-03-20", "v": 105700.0, "close": 1560.0, "position": False},
                ], signals=[
                    {"t": "2026-03-20", "side": "sell", "price": 1560.0, "phase": "live", "hadPosition": True},
                    {"t": "2026-03-12", "side": "buy", "price": 1500.0, "phase": "backfill", "hadPosition": False},
                ]),
                dict(sample_run("legacy2", market="us", code="AAPL", createdAt=1712345000000),
                     trades=[], equity=[], signals=[]),
                {"code": "no-id-会被跳过"},
            ],
        }
        path = os.path.join(self._tmp.name, "strategy_runs.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(legacy, fh, ensure_ascii=False)
        return path

    def test_migrate_from_json(self):
        """旧 JSON 整体入库：任务/交易/权益/信号分别落到各自的表"""
        store = self.open_store()
        stat = store.migrate_from_json(self._write_legacy_json())
        self.assertEqual(stat["runs"], 2)
        self.assertEqual(stat["trades"], 2)
        self.assertEqual(stat["equity"], 2)
        self.assertEqual(stat["signals"], 2)
        self.assertEqual(stat["skipped"], 1)

        counts = store.counts()
        self.assertEqual(counts["runs"], 2)
        self.assertEqual(counts["trades"], 2)
        self.assertEqual(counts["equity"], 2)
        self.assertEqual(counts["signals"], 2)

        # 任务主体（嵌套字段）完整保留，明细已拆到各自的表
        run = store.get_run("legacy1")
        self.assertEqual(run["params"], {"fast": 5.0, "slow": 20.0})
        self.assertEqual(run["monthly"]["2026-03"]["realized"], 812.5)
        self.assertNotIn("trades", run)
        self.assertEqual(store.get_run("legacy2")["code"], "AAPL")

        # 交易：顺序保持（旧 → 新），成本字段按 0 补齐
        trades = store.list_trades("legacy1")
        self.assertEqual([t["out_date"] for t in trades], ["2026-03-20", "2026-03-25"])
        self.assertEqual(trades[1]["phase"], "live")

        # 权益：按时间升序
        self.assertEqual([p["t"] for p in store.list_equity("legacy1")],
                         ["2026-03-19", "2026-03-20"])

        # 信号：源文件是「最新在前」，入库后 list_signals 仍是最新在前
        sigs = store.list_signals("legacy1", None)
        self.assertEqual([s["t"] for s in sigs], ["2026-03-20", "2026-03-12"])
        self.assertEqual([s["seq"] for s in sigs], [1, 0])

        # 迁移元信息
        self.assertTrue(store.meta_get("migrated_from").endswith("strategy_runs.json"))
        self.assertEqual(store.meta_get("migrated_source_version"), 1)

    def test_migrate_from_json_idempotent(self):
        """重复导入不会产生重复明细（replace 语义）"""
        path = self._write_legacy_json()
        store = self.open_store()
        store.migrate_from_json(path)
        store.migrate_from_json(path)
        counts = store.counts()
        self.assertEqual((counts["runs"], counts["trades"], counts["equity"], counts["signals"]),
                         (2, 2, 2, 2))

    def test_migrate_from_json_no_trades_key(self):
        """没有明细字段的任务也能导入（明细表为空）"""
        path = os.path.join(self._tmp.name, "plain.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(sample_run("plain1"), fh, ensure_ascii=False)
        store = self.open_store()
        self.assertEqual(store.migrate_from_json(path)["runs"], 1)
        self.assertEqual(store.list_trades("plain1"), [])


# --------------------------------------------------------------------------- #
# 7. 并发读写
# --------------------------------------------------------------------------- #

class TestConcurrency(StoreTestBase):

    def test_concurrent_read_write(self):
        """多线程并发写（任务 / 交易 / 权益 / 信号）与并发查询互不干扰"""
        store = self.open_store()
        store.upsert_run(sample_run())
        reader_store = self.open_store()          # 模拟另一个进程/实例同时读
        writers, readers = 4, 3
        per_writer = 40
        errors = []
        barrier = threading.Barrier(writers + readers)

        def write_worker(idx):
            try:
                barrier.wait(timeout=10)
                for i in range(per_writer):
                    store.append_trade(RUN_ID, sample_trade(
                        qty=100 + i, outDate="2026-05-%02d" % (1 + i % 28),
                        phase="live" if idx % 2 else "backfill"))
                    store.append_equity(RUN_ID, {
                        "t": "2026-05-%02d" % (1 + i % 28), "v": 100000.0 + idx * 10 + i,
                        "close": 1600.0 + i, "position": bool(i % 3 == 0),
                    })
                    store.append_signal(RUN_ID, {
                        "t": "2026-05-%02d" % (1 + i % 28), "side": "buy",
                        "price": 1600.0 + i, "phase": "live", "hadPosition": False,
                    })
                # 顺带验证「任务更新」与明细写入并发
                store.upsert_run(sample_run(barProcessed=100 + idx, lastPrice=1650.0 + idx))
            except Exception as exc:  # noqa: BLE001
                errors.append("writer%d: %r" % (idx, exc))

        def read_worker(idx):
            try:
                barrier.wait(timeout=10)
                for _ in range(60):
                    reader_store.list_trades(RUN_ID, limit=20)
                    reader_store.list_equity(RUN_ID, limit=20)
                    reader_store.list_signals(RUN_ID, 10)
                    reader_store.list_runs()
            except Exception as exc:  # noqa: BLE001
                errors.append("reader%d: %r" % (idx, exc))

        threads = ([threading.Thread(target=write_worker, args=(i,)) for i in range(writers)]
                   + [threading.Thread(target=read_worker, args=(i,)) for i in range(readers)])
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertTrue(all(not th.is_alive() for th in threads))

        expected = writers * per_writer
        self.assertEqual(len(store.list_trades(RUN_ID)), expected)
        self.assertEqual(len(store.list_equity(RUN_ID)), expected)
        self.assertEqual(len(store.list_signals(RUN_ID, None)), expected)
        # 并发写入的 seq 连续无重复
        self.assertEqual([t["seq"] for t in store.list_trades(RUN_ID)], list(range(expected)))
        # 任务仍在且能读回
        self.assertIsNotNone(store.get_run(RUN_ID))
        self.assertEqual(store.counts()["runs"], 1)

    def test_concurrent_distinct_runs(self):
        """不同任务并行写入互不影响（外键各自成立）"""
        store = self.open_store()
        errors = []

        def worker(idx):
            rid = "run-%d" % idx
            try:
                store.upsert_run(sample_run(rid, code="60000%d" % idx))
                for i in range(25):
                    store.append_trade(rid, sample_trade(qty=100 + i))
                    store.append_equity(rid, {"t": "2026-06-%02d" % (1 + i % 28), "v": 100000.0 + i})
            except Exception as exc:  # noqa: BLE001
                errors.append("worker%d: %r" % (idx, exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)
        self.assertEqual(errors, [])
        for i in range(5):
            self.assertEqual(len(store.list_trades("run-%d" % i)), 25)
        self.assertEqual(store.counts()["trades"], 125)


# --------------------------------------------------------------------------- #
# 8. 结构化日志
# --------------------------------------------------------------------------- #

class TestLogs(StoreTestBase):

    def test_jsonl_append_and_ring(self):
        """JSONL 追加落盘 + 内存环形缓冲查询（最新在前）"""
        path = os.path.join(self._tmp.name, "logs", "strategy.jsonl")
        logger = Logger(path, capacity=500)
        self.addCleanup(logger.close)
        logger.log("info", "run.created", run_id=RUN_ID, code="600519")
        logger.info("engine.tick", ticks=3)
        logger.warn("engine.slow", cost_ms=1800)
        logger.error("quote.failed", code="AAPL", err="timeout")

        ring = logger.ring(10)
        self.assertEqual([r["event"] for r in ring],
                         ["quote.failed", "engine.slow", "engine.tick", "run.created"])
        self.assertEqual(ring[0]["level"], "error")
        self.assertEqual(ring[0]["code"], "AAPL")
        self.assertTrue(ring[0]["ts"] > 0 and ring[0]["time"])

        # 磁盘上的 JSONL 与内存缓冲一致（每行一个 JSON 对象）
        lines = read_jsonl(path)
        self.assertEqual([r["event"] for r in lines], [r["event"] for r in ring])
        with open(path, encoding="utf-8") as fh:
            raw = [json.loads(l) for l in fh.read().strip().splitlines()]
        self.assertEqual(len(raw), 4)
        self.assertEqual(raw[0]["event"], "run.created")

    def test_ring_filters(self):
        """ring(limit) 取最近 N 条；ring(limit, level) 按最低级别过滤"""
        logger = Logger(None, capacity=500)
        for i in range(10):
            logger.log("info" if i % 2 == 0 else "warn", "evt.%d" % i, i=i)
        self.assertEqual(len(logger.ring(3)), 3)
        self.assertEqual([r["i"] for r in logger.ring(3)], [9, 8, 7])
        self.assertEqual(len(logger.ring(None, level="warn")), 5)
        self.assertTrue(all(r["level"] == "warn" for r in logger.ring(None, "warn")))
        self.assertEqual(logger.ring(2, "error"), [])

    def test_ring_capacity_500(self):
        """环形缓冲写满后覆盖最旧记录（默认 500 条）"""
        logger = Logger(None)
        self.assertEqual(logger.buffer.capacity, 500)
        for i in range(620):
            logger.log("info", "evt", i=i)
        ring = logger.ring()
        self.assertEqual(len(ring), 500)
        self.assertEqual(ring[0]["i"], 619)      # 最新在前
        self.assertEqual(ring[-1]["i"], 120)     # 最旧的 120 条已被覆盖
        self.assertEqual(len(logger.buffer), 500)

        small = Logger(None, capacity=3)
        for i in range(5):
            small.log("info", "evt", i=i)
        self.assertEqual([r["i"] for r in small.ring()], [4, 3, 2])

    def test_level_threshold(self):
        """低于最低级别的日志被丢弃（不落盘、不入缓冲）"""
        path = os.path.join(self._tmp.name, "app.jsonl")
        logger = Logger(path, level="warn")
        self.addCleanup(logger.close)
        logger.debug("noisy.debug")
        logger.info("noisy.info")
        logger.warn("kept.warn")
        self.assertEqual([r["event"] for r in logger.ring()], ["kept.warn"])
        self.assertEqual(logger.dropped, 2)
        self.assertEqual([r["event"] for r in read_jsonl(path)], ["kept.warn"])

    def test_reserved_keys_protected(self):
        """业务字段不能覆盖 ts / time（level / event 由位置参数给出）"""
        logger = Logger(None)
        rec = logger.log("info", "evt", ts=1, time="x", extra=2)
        self.assertEqual((rec["level"], rec["event"], rec["extra"]), ("info", "evt", 2))
        self.assertNotEqual(rec["ts"], 1)
        self.assertNotEqual(rec["time"], "x")

    def test_dirty_field_serialized(self):
        """不可 JSON 序列化的字段不会让日志写失败"""
        path = os.path.join(self._tmp.name, "app.jsonl")
        logger = Logger(path)
        self.addCleanup(logger.close)
        logger.log("info", "evt", obj=object(), tag="ok")
        self.assertIsNone(logger.last_error)
        rec = read_jsonl(path)[0]
        self.assertEqual(rec["tag"], "ok")
        self.assertIn("object", rec["obj"])

    def test_ring_buffer_unit(self):
        """RingBuffer 自身：容量上限与快照顺序"""
        buf = RingBuffer(3)
        for i in range(4):
            buf.append({"i": i})
        self.assertEqual([r["i"] for r in buf.snapshot()], [1, 2, 3])
        self.assertEqual(len(buf), 3)
        buf.clear()
        self.assertEqual(len(buf), 0)

    def test_default_logger_and_module_helpers(self):
        """模块级 log / ring 走全局默认 logger"""
        path = os.path.join(self._tmp.name, "default.jsonl")
        logger = logs_mod.configure(path, level="debug")
        self.addCleanup(logger.close)
        logs_mod.log("info", "default.path", k=1)
        self.assertIs(logs_mod.get_logger(), logger)
        self.assertEqual(logs_mod.ring(5)[0]["event"], "default.path")

    def test_sqlite_sink(self):
        """sink 双写：日志同时进 JSONL 与 SQLite 的 logs 表"""
        store = self.open_store()
        store.upsert_run(sample_run())
        path = os.path.join(self._tmp.name, "app.jsonl")
        logger = Logger(path, sink=logs_mod.sqlite_sink(store))
        self.addCleanup(logger.close)
        logger.log("info", "run.tick", run_id=RUN_ID, ticks=7)
        logger.log("error", "run.failed", run_id=RUN_ID, err="行情超时")

        rows = store.list_logs(limit=10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["level"], "error")
        self.assertEqual(rows[0]["err"], "行情超时")
        self.assertEqual(rows[0]["run_id"], RUN_ID)
        self.assertEqual(store.list_logs(limit=10, min_level="error")[0]["event"], "run.failed")
        self.assertEqual(len(store.list_logs(limit=10, level="info")), 1)
        self.assertEqual(len(store.list_logs(limit=10, run_id=RUN_ID)), 2)
        self.assertEqual([r["event"] for r in read_jsonl(path)], ["run.failed", "run.tick"])

    def test_logs_table_via_store(self):
        """Store.append_log 直接使用（不依赖 Logger）"""
        store = self.open_store()
        store.append_log("warn", "engine.slow", ticks=11)
        store.append_log("info", "engine.ok", ticks=12)
        self.assertEqual(store.counts()["logs"], 2)
        self.assertEqual(store.list_logs(1)[0]["ticks"], 12)

    def test_logger_thread_safety(self):
        """多线程并发写日志：不丢条数、缓冲不超容量"""
        logger = Logger(None, capacity=500)
        errors = []

        def worker(idx):
            try:
                for i in range(200):
                    logger.log("info" if i % 2 else "warn", "evt", w=idx, i=i)
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=20)
        self.assertEqual(errors, [])
        self.assertEqual(logger.count, 800)
        self.assertEqual(len(logger.ring()), 500)          # 只保留最近 500 条
        self.assertTrue(all(r["ts"] <= now_ms() for r in logger.ring()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
