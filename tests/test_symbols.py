# -*- coding: utf-8 -*-
"""core.symbols（标的名称识别层）的单元测试（仅标准库 unittest，可直接 python 运行）。

覆盖范围（需求的 4 组 20 条断言 → 本文件 18 个测试类）
-----------------------------------------------------
  A. TestNormalizeText / TestParseTokenCn / TestParseTokenUs / TestParseTokenNameSplit
     归一化（全角、噪声、大小写）、A股与美股代码形态、非代码必须返回 None、
     「代码:名称」两种语序；
  B. TestBuildIndex / TestMatchLocalTiers / TestMatchLocalGuards
     名录索引的跳过与统计、四个匹配档位、排序完全确定、单字与未知查询的噪声边界；
  C. TestResolveByCode / TestResolveUniqueName / TestResolveAmbiguous /
     TestResolveLetterInCn / TestResolveRemoteEcho / TestResolveRemoteMulti /
     TestResolveRemoteFailure / TestResolveDedupe / TestResolveLimits /
     TestResolveDirty / TestResolveSummaryJson
     resolve 全链路：代码直判 + 名录补名、名称唯一命中、**歧义绝不自动落定**、
     拼音串在 A 股语境优先按名称、远端回显只能算猜测、远端多候选的族判断、
     远端故障/脏返回不抛异常、去重、截断、脏输入、summary 自洽与严格 JSON；
  D. TestAdvisorNameBackfill
     行情名称回填到 row.name，且不覆盖调用方明确给出的名称；
  E. TestKnownDefects
     已发现但**不允许修改被测模块**的 5 处缺陷锚点（expectedFailure，见类内说明）。

为什么整个文件必须离线
--------------------
``resolve`` 的远端搜索是**依赖注入**的 ``search_fn``（与 ``core/advisor.recommend`` 的
``fetch_bars`` / ``fetch_quote`` 一样），因此本文件一律注入 ``FakeSearch`` / ``FakeFeed``：
前者按预设表回答并记录调用参数，后者只回内存里的合成K线。整个文件没有任何 socket、
DNS 或第三方接口依赖，也**不做任何 monkeypatch 真实网络函数**的事后补救 —— 靠注入契约
从源头保证「测试永远不会打真实接口、也不会消耗第三方配额」。

为什么每条断言都写理由
--------------------
本模块的核心风险不是「识别不出来」，而是**静默把分析对象换成另一只股票**：
「平安」同时命中 平安银行 与 中国平安，静默取第一个会让用户拿着 A 股的结论看 B 股。
因此每个用例的 docstring 都写清「这条锁的是什么风险、被放宽会怎样」，让后来改动的人
知道哪一条不能随便改成「取第一个」。

为什么用合成小型名录而不是真实全市场快照
--------------------------------------
``build_index`` / ``match_local`` 只关心 (code, name) 两个字符串，与行情源无关；用**人工
构造的小型名录**（名称借用真实股票名便于人工核对，但**不保证**与真实市场一致）可以让
每个档位（完全匹配 / 名称前缀 / 名称包含 / 代码前缀）都被精确断言，不受新股上市、改名、
快照缺失等外部因素影响。

运行方式（tests/ 下无 __init__.py，直接跑文件最稳）：
    python3 tests/test_symbols.py
    python3 -m unittest discover -s tests -p "test_*.py"
"""

import json
import math
import os
import sys
import unittest
from datetime import date, timedelta

# 让 tests/ 目录之外的包（core）可被导入，兼容任意工作目录运行
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import symbols as Y          # noqa: E402  被测模块
from core import advisor as A          # noqa: E402  名称回填所在模块（用例 19/20）

# --------------------------------------------------------------------------- #
# 契约常量（一律从被测模块读取，避免测试里再抄一份口径造成「双重标准」）
# --------------------------------------------------------------------------- #
#: item.kind 的取值域：四个分支缺任何一个都说明识别链少了一段
KINDS = (Y.KIND_CODE, Y.KIND_NAME, Y.KIND_AMBIGUOUS, Y.KIND_UNKNOWN)
#: 「必带代码」的两个 kind（不变量：这两类必带 code，另两类必不带 code）
CODED_KINDS = (Y.KIND_CODE, Y.KIND_NAME)
#: 未设置标记（用来区分「没传远端结果」与「显式传了 None」）
_UNSET = object()


def assert_consistent(case, res):
    """resolve 结果的全局自洽性（用例 16/17 共用，避免每个用例抄一遍）。

    三条恒等式缺一不可：``total`` 是 items 的长度（不是入参长度，去重会减少）、
    四个 kind 覆盖全部 items（``total == resolved + ambiguous + unknown``）、
    ``resolved`` 拆成 code + name 两类。summary 一旦不自洽，界面上的「已识别 N / 共 M」
    就会与实际渲染的行数对不上，用户会以为系统吃掉了输入。
    """
    s, items = res["summary"], res["items"]
    case.assertEqual(s["total"], len(items), "total 必须等于实际产出的条目数")
    case.assertEqual(s["total"], s["resolved"] + s["ambiguous"] + s["unknown"],
                     "四个 kind 必须覆盖所有条目：%r" % (s,))
    case.assertEqual(s["resolved"], s["code"] + s["name"], "resolved = code + name")
    case.assertEqual(s["named"], len([i for i in items if i.get("name")]),
                     "named 必须等于「有名称」的条数")
    case.assertEqual(s["guessed"], len([i for i in items if i.get("guess")]),
                     "guessed 必须等于 guess 为真的条数")
    for it in items:
        case.assertIn(it["kind"], KINDS, "kind 出现取值域外的值：%r" % (it,))


def assert_code_invariant(case, items):
    """kind 与 code 的联合不变量：只有 code/name 允许带代码，且它们必须带代码。

    这是「静默换股票」这类事故的结构性防线：一旦 ambiguous / unknown 也带上 code，
    下游（core/advisor、推送通道）就会把它当成一只确定的股票拿去分析。
    """
    for it in items:
        if it["kind"] in CODED_KINDS:
            case.assertTrue(it["code"], "kind=%s 必须带代码：%r" % (it["kind"], it))
        else:
            case.assertIsNone(it["code"], "kind=%s 不允许带代码：%r" % (it["kind"], it))


# --------------------------------------------------------------------------- #
# 测试替身（确定性 + 记录调用，绝不联网）
# --------------------------------------------------------------------------- #
class FakeSearch(object):
    """假远端搜索（东财 suggest 的替身）：按预设表回答并记录调用参数，绝不联网。

    三种回答方式正好覆盖 resolver 的三个远端分支：

    ============  ============================================================
    ``rows``      正常的 ``{"rows": [...]}`` 响应（列表按顺序给候选）；
    ``table``     按**原样查询串**分表回答；未收录的查询返回空 rows（用来验证
                  「远端确实被问了、也确实没命中」而不是「根本没问」）；
    ``result``    原样返回的脏响应（None / 42 / "oops" / 裸 list），用来验证远端
                  故障或接口格式变化不会让整批识别失败；
    ``raises``    直接抛异常，验证异常被吞掉而不是冒到调用方。
    ============  ============================================================
    """

    def __init__(self, rows=(), table=None, result=_UNSET, raises=None):
        self.rows = list(rows)          # 原样保留：脏行（None / 42 / "x"）必须真的传下去
        self.table = table
        self.result = result
        self.raises = raises
        self.calls = []

    @staticmethod
    def _copy(rows):
        """只对 dict 行做浅拷贝（避免用例之间互相污染），脏行原样返回。"""
        return [dict(r) if isinstance(r, dict) else r for r in rows]

    def __call__(self, query, market):
        self.calls.append((query, market))
        if self.raises is not None:
            raise self.raises
        if self.result is not _UNSET:
            return self.result
        if self.table is not None:
            return {"rows": self._copy(self.table.get(query, []))}
        return {"rows": self._copy(self.rows)}


# --------------------------------------------------------------------------- #
# 名录夹具（合成的小型市场快照）
# --------------------------------------------------------------------------- #
#: 通用名录：6 条，刚好能构造出四个档位与「平安」两义（前缀 + 包含）
#: 名称借用真实股票名便于人工核对，但**不保证**与真实市场一致，只用于匹配逻辑
INDEX_ROWS = [
    {"code": "600519", "name": "贵州茅台"},
    {"code": "600036", "name": "招商银行"},
    {"code": "000001", "name": "平安银行"},
    {"code": "601318", "name": "中国平安"},
    {"code": "300750", "name": "宁德时代"},
    {"code": "002415", "name": "海康威视"},
]
INDEX = Y.build_index(INDEX_ROWS)

#: 排序专用名录：三条都落在「名称前缀」同一档，用来断言同档位的二级排序
#: （名称短的在前 → 再按代码升序）。名字是明显的占位名，避免被当成真实标的
ORDER_ROWS = [
    {"code": "601318", "name": "平安乙"},
    {"code": "000001", "name": "平安银行"},
    {"code": "000002", "name": "平安丙"},
]
ORDER_INDEX = Y.build_index(ORDER_ROWS)


# --------------------------------------------------------------------------- #
# A. normalize_text / parse_token
# --------------------------------------------------------------------------- #
class TestNormalizeText(unittest.TestCase):
    """用例 1：全角转半角、去噪、大小写统一。

    为什么这三件事必须一起做：中文输入法下 ``６００５１９``、``600519：贵州茅台``、
    ``贵州·茅台`` 都是正常输入，任何一条没归一化，用户看到的就是「明明输对了却
    识别不出来」—— 这类失败无法从界面上解释，是最伤信任的一类 bug。
    """

    def test_fullwidth_digits_letters_and_colon(self):
        """全角数字 / 字母 / 冒号 / 空格一律归一成半角，且不破坏原有结构。"""
        self.assertEqual(Y.normalize_text("６００５１９"), "600519")
        self.assertEqual(Y.normalize_text("ａａｐｌ"), "AAPL")
        # 全角冒号归一成半角冒号：它是「代码:名称」的分隔符，必须保住
        self.assertEqual(Y.normalize_text("６００５１９：贵州茅台"), "600519:贵州茅台")
        # 全角空格（U+3000）属于空白噪声，归一后会被直接去掉
        self.assertEqual(Y.normalize_text("600519\u3000贵州茅台"), "600519贵州茅台")

    def test_noise_chars_and_case(self):
        """空格 / 全角空格 / · / - / _ 都是噪声，大小写统一为大写。

        ``-`` 与 ``·`` 出现在「600-519」「贵州·茅台」这类手写/复制输入里很常见，
        不清理就会出现「多了个点就识别不出来」。
        """
        self.assertEqual(Y.normalize_text(" ６００·５１９ "), "600519")
        self.assertEqual(Y.normalize_text("贵州·茅台"), "贵州茅台")
        self.assertEqual(Y.normalize_text("600-519"), "600519")
        self.assertEqual(Y.normalize_text(" sh600519 "), "SH600519")
        self.assertEqual(Y.normalize_text("brk-b"), "BRKB")

    def test_dirty_values_do_not_raise(self):
        """None / 数字等脏值不能抛异常（normalize 位于每个输入的第一跳）。

        它是所有入口的公共前置步骤，一旦抛异常会让整批识别失败；数字型代码
        （前端表单偶尔会传 json 数字）也要能正常转成字符串。
        """
        self.assertEqual(Y.normalize_text(None), "")
        self.assertEqual(Y.normalize_text(600519), "600519")


class TestParseTokenCn(unittest.TestCase):
    """用例 2：A 股代码形态与「非代码必须返回 None」。

    为什么「非代码返回 None」和「代码能识别」同等重要：把中文长串当代码去请求上游
    会得到一堆空结果或错误标的（``平安`` 会被当成 6 位代码的邻居去查询），
    而「1」「60051」这类残缺输入若被当成代码，用户会以为查的是一只真股票。
    """

    def test_cn_code_forms(self):
        """六位数字及其四种等价写法都应解析出同一个 A 股代码。"""
        cases = {
            "600519": "600519",
            "sh600519": "600519",
            "SH600519": "600519",
            "SZ000001": "000001",
            "bj430139": "430139",
            "000001.SZ": "000001",
            "600519.SH": "600519",
            "６００５１９": "600519",       # 全角数字
        }
        for raw, code in cases.items():
            with self.subTest(raw=raw):
                parsed = Y.parse_token(raw)
                self.assertIsNotNone(parsed, "应识别为代码：%r" % raw)
                self.assertEqual(parsed["code"], code)
                self.assertEqual(parsed["market"], Y.MARKET_CN)
                self.assertEqual(parsed["source"], "code")

    def test_non_code_returns_none(self):
        """中文全称 / 单字 / 位数不足 / 空值 / 纯数字残码都不算代码。

        「平安」「贵州茅台」这类输入必须落到名称识别；``1`` / ``60051`` / ``123``
        若被当成代码，会向上游请求一个不存在的标的并把「未找到」显示成「数据不足」。
        """
        for raw in ("平安", "贵州茅台", "1", "12", "60051", "6005199", "123",
                    "", "   ", None):
            with self.subTest(raw=repr(raw)):
                self.assertIsNone(Y.parse_token(raw), "不该被当成代码：%r" % (raw,))


class TestParseTokenUs(unittest.TestCase):
    """用例 3：美股形态，以及「中文串绝不能被当成代码」的回归。

    中文串误判成代码是本模块最典型的误判方向（大写 + 非 ASCII 会让正则的宽容度
    被误用）。这里用 ``贵州茅台`` 显式锁死：中文输入一律走名称识别。
    """

    def test_us_tickers(self):
        """纯字母 / 带类别后缀 / 带市场后缀的写法都应解析成美股代码。"""
        self.assertEqual(Y.parse_token("aapl"),
                         {"code": "AAPL", "market": Y.MARKET_US, "name": "", "source": "code"})
        self.assertEqual(Y.parse_token("BRK.B")["code"], "BRK.B")
        self.assertEqual(Y.parse_token("BRK.B")["market"], Y.MARKET_US)
        self.assertEqual(Y.parse_token("nvda:US")["code"], "NVDA")
        self.assertEqual(Y.parse_token("nvda:US")["market"], Y.MARKET_US)
        self.assertEqual(Y.parse_token("tsla")["market"], Y.MARKET_US)

    def test_cjk_is_never_a_code(self):
        """中文串必须返回 None —— 「任何中文都当代码」这类误判的回归锚点。

        一旦中文被判成代码，``平安`` 就会带着 market=us / code=平安 去打美股接口，
        用户看到的是「查无此股」而不是「命中 2 个候选，请选择」。
        """
        for raw in ("贵州茅台", "平安", "中国平安", "宁的时代"):
            with self.subTest(raw=raw):
                self.assertIsNone(Y.parse_token(raw))
                self.assertIsNone(Y.parse_token(raw, market=Y.MARKET_US))


class TestParseTokenNameSplit(unittest.TestCase):
    """用例 4：「代码 + 名称」两种语序都要给出 {code, market, name}，且 name 去空白。

    为什么两种语序都要支持：用户既可能先打代码再打名字（从别处复制代码前缀），
    也可能先打名字再打代码（中文输入法的自然顺序）。只支持一种时，另一种会整体
    落到名称识别并因为「名字 + 数字」这种怪串而识别失败。
    """

    def test_code_first_full_equality(self):
        """代码在前：完整断言四个字段（这是前端 advisor.js 的同一规则集）。"""
        self.assertEqual(
            Y.parse_token("600519 贵州茅台"),
            {"code": "600519", "market": Y.MARKET_CN, "name": "贵州茅台", "source": "code"})

    def test_name_first_and_separator_variants(self):
        """名称在前、全角冒号、冒号两侧带空格 —— 都要切出同一个结果且 name 去空白。"""
        cases = ["贵州茅台 600519", "贵州茅台:600519", "贵州茅台：600519",
                 "600519:贵州茅台", "600519：贵州茅台", " 600519 : 贵州茅台 ",
                 "贵州茅台:６００５１９"]
        for raw in cases:
            with self.subTest(raw=raw):
                parsed = Y.parse_token(raw)
                self.assertIsNotNone(parsed, "应切出代码：%r" % raw)
                self.assertEqual(parsed["code"], "600519")
                self.assertEqual(parsed["market"], Y.MARKET_CN)
                self.assertEqual(parsed["name"], "贵州茅台",
                                 "名称必须去空白且不带分隔符：%r" % raw)


# --------------------------------------------------------------------------- #
# B. build_index / match_local
# --------------------------------------------------------------------------- #
class TestBuildIndex(unittest.TestCase):
    """用例 5：建索引要跳过残缺行、统计 count、byCode/byName 正确、空输入不抛异常。

    名录来自服务端的全市场快照（5000+ 行，可能含停牌/退市/字段缺失的脏行），
    任何一行脏数据都不该让整张索引建不起来 —— 索引建不起来，所有名称输入都会
    退化成「未识别」。
    """

    def test_skips_incomplete_rows_and_counts(self):
        """残缺行只跳过、count 与实际条目数一致、byCode/byName 可直接反查。"""
        rows = [
            {"code": "600519", "name": "贵州茅台"},
            {"code": " 600036 ", "name": " 招商银行 "},     # 两侧空白：code 归一化、name strip
            {"code": "601318", "name": "中国平安"},
            {"code": "000001"},                            # 缺 name → 跳过
            {"name": "缺代码"},                             # 缺 code → 跳过
            {"code": "", "name": ""},                      # 空串 → 跳过
            {"code": None, "name": None},                  # None → 跳过
            "not-a-dict", None, 42, ["600000", "x"],       # 非 dict → 跳过
        ]
        index = Y.build_index(rows)
        self.assertEqual(index["count"], 3)
        self.assertEqual(len(index["items"]), 3)
        self.assertEqual(index["market"], Y.MARKET_CN)
        self.assertEqual(index["byCode"],
                         {"600519": "贵州茅台", "600036": "招商银行", "601318": "中国平安"})
        # byName 的值是**列表**（同名不同代码在快照里是可能的，例如 A/B 份额）
        self.assertEqual(index["byName"]["贵州茅台"], ["600519"])
        self.assertIsInstance(index["byName"]["招商银行"], list)

    def test_empty_input_is_safe(self):
        """空 / None / 非列表输入返回 count=0 的空索引，不抛异常。

        服务端首次启动、快照接口失败时都会走到这里；此时名称识别退化为「只能输代码」，
        但绝不能让整个识别接口 500。
        """
        for rows in ([], None, (), "not-a-list", 0):
            with self.subTest(rows=repr(rows)):
                index = Y.build_index(rows)
                self.assertEqual(index["count"], 0)
                self.assertEqual(index["items"], [])
                self.assertEqual(index["byCode"], {})
                self.assertEqual(index["byName"], {})


class TestMatchLocalTiers(unittest.TestCase):
    """用例 6：四个档位 + 排序完全确定。

    档位决定「谁能自动落定」：只有完全匹配（或唯一命中）才允许自动选定；同档并列
    即视为歧义。排序则是歧义候选列表的展示顺序 —— 必须完全确定，否则同一个查询在
    界面上每次刷新顺序都可能变，用户会点错。
    """

    def test_tier_scores_and_labels(self):
        """完全匹配 > 名称前缀 > 名称包含 > 代码前缀，且 tier 文案取自 TIER_LABEL。"""
        self.assertGreater(Y.TIER_EXACT, Y.TIER_PREFIX)
        self.assertGreater(Y.TIER_PREFIX, Y.TIER_SUBSTR)
        self.assertGreater(Y.TIER_SUBSTR, Y.TIER_CODE_PREFIX)
        cases = [("贵州茅台", Y.TIER_EXACT, "600519"),
                 ("贵州茅", Y.TIER_PREFIX, "600519"),
                 ("州茅", Y.TIER_SUBSTR, "600519"),
                 ("6005", Y.TIER_CODE_PREFIX, "600519")]
        for query, score, code in cases:
            with self.subTest(query=query):
                hits = Y.match_local(query, INDEX)
                self.assertEqual(len(hits), 1, "该查询在本名录里应唯一命中：%r" % query)
                self.assertEqual(hits[0]["code"], code)
                self.assertEqual(hits[0]["score"], score)
                self.assertEqual(hits[0]["tier"], Y.TIER_LABEL[score])
                self.assertEqual(hits[0]["market"], Y.MARKET_CN)
                self.assertEqual(hits[0]["key"], Y.normalize_text(hits[0]["name"]))

    def test_exact_code_query_hits_exact_tier(self):
        """代码完全相等也算 TIER_EXACT（与名称完全相等同档）。

        否则「600519」会落到「代码前缀」档，与其它以 600519 开头的输入混在一起，
        精确输入反而排不到第一。
        """
        hits = Y.match_local("600519", INDEX)
        self.assertEqual([h["code"] for h in hits], ["600519"])
        self.assertEqual(hits[0]["score"], Y.TIER_EXACT)

    def test_same_tier_order_is_deterministic(self):
        """同档位：名称短的在前，再按代码升序；连续两次调用、不同建索引顺序都一致。

        确定性是歧义候选列表能被用户「按位置点」的前提；只要顺序会漂，
        用户就可能在两次刷新之间点到不同的股票。
        """
        first = [h["code"] for h in Y.match_local("平安", ORDER_INDEX)]
        self.assertEqual(first, ["000002", "601318", "000001"],
                         "同档（均为名称前缀）时：名称短的在前，再按代码升序")
        second = [h["code"] for h in Y.match_local("平安", ORDER_INDEX)]
        self.assertEqual(first, second, "同一输入连续两次必须完全一致")
        # 建索引的输入顺序也不能影响结果（sort 必须构成全序）
        reversed_index = Y.build_index(list(reversed(ORDER_ROWS)))
        self.assertEqual([h["code"] for h in Y.match_local("平安", reversed_index)], first)

    def test_limit_is_clamped(self):
        """非法 limit 不能把结果切空或切出负数（上限至少 1 条）。

        limit 来自接口参数，``limit=0`` / 负数若直接落进切片会得到空列表或反向切片，
        表现为「命中却返回空」，与「未识别」无法区分。
        """
        self.assertEqual(len(Y.match_local("平安", INDEX, limit=1)), 1)
        self.assertEqual(len(Y.match_local("平安", INDEX, limit=-3)), 1)


class TestMatchLocalGuards(unittest.TestCase):
    """用例 7：单字符与未知查询的噪声边界。

    本地名录是 5000+ 条的全市场表，「名称包含」匹配对单字几乎必然命中上千条，
    返回这样一张清单对用户没有任何信息量（还白占一次候选渲染）。
    """

    def test_unknown_and_dirty_inputs_return_empty(self):
        """未知名称、空查询、脏名录一律返回空列表且不抛异常。"""
        self.assertEqual(Y.match_local("无此标的", INDEX), [])
        self.assertEqual(Y.match_local("", INDEX), [])
        self.assertEqual(Y.match_local(None, INDEX), [])
        self.assertEqual(Y.match_local("贵州茅台", None), [])
        self.assertEqual(Y.match_local("贵州茅台", {"items": None}), [])

    def test_single_char_matches_nothing(self):
        """单字一律不命中（子串与**前缀**都不允许），两字起才参与匹配。

        「贵州茅台」含「茅」、以「贵州」开头，但输入单个「茅」或「贵」时都不该算命中：
        单字在全市场上要么命中两千只（子串），要么只是「碰巧」排在最前（前缀）。
        回归点：前缀档位曾经只要求 ≥1 字，于是「中」这种单字会被**自动落定**成
        中国平安 —— 用户输了半个词，系统却替他选了股票。
        """
        self.assertEqual(Y.match_local("茅", INDEX), [])
        self.assertEqual(Y.match_local("茅", INDEX, limit=5), [])
        self.assertEqual(Y.match_local("中", INDEX), [], "单字前缀也必须不命中")
        hits = Y.match_local("贵州", INDEX)
        self.assertTrue(hits, "两字前缀应该命中")
        self.assertEqual(hits[0]["tier"], Y.TIER_LABEL[Y.TIER_PREFIX])


# --------------------------------------------------------------------------- #
# C. resolve（核心）
# --------------------------------------------------------------------------- #
class TestResolveByCode(unittest.TestCase):
    """用例 8：代码直判 + 本地名录补名；名录里没有的代码留空待回填。

    为什么代码路径要去名录里顺手查一下名字：界面与历史记录里全是「600519」这样的
    数字时，用户得自己背代码；而从全市场快照查名称是零成本、零配额的。
    为什么查不到就留空而不打远端：core/advisor 取行情时本来就会拿到名称，回填是免费的。
    """

    def test_code_gets_name_from_index(self):
        """代码直判成功 + 名称由本地名录补全，且不带 hits / 不是 guess。"""
        item = Y.resolve(["600519"], index=INDEX)["items"][0]
        self.assertEqual(item["kind"], Y.KIND_CODE)
        self.assertEqual(item["code"], "600519")
        self.assertEqual(item["market"], Y.MARKET_CN)
        self.assertEqual(item["name"], "贵州茅台", "名称应由本地名录补全")
        self.assertIn("名录补全", item["note"])
        self.assertFalse(item["guess"], "代码直判是确定结果，不是猜测")
        self.assertEqual(item["hits"], [])

    def test_code_forms_all_resolve_to_same_stock(self):
        """带前缀 / 后缀 / 名称的写法都要归一到同一个 code（否则同一只股票会被当成多只）。"""
        for raw in ("600519", "sh600519", "SH600519", "600519.SH",
                    "600519:贵州茅台", "贵州茅台 600519"):
            with self.subTest(raw=raw):
                items = Y.resolve([raw], index=INDEX)["items"]
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0]["kind"], Y.KIND_CODE)
                self.assertEqual(items[0]["code"], "600519")
                self.assertEqual(items[0]["name"], "贵州茅台")

    def test_unknown_code_keeps_name_empty_for_quote_fill(self):
        """名录里没有的代码：kind=code、name 为空、note 说明「名称待行情返回后回填」。

        这三件事缺一不可：name 若填成代码本身，界面就会把「600999」当成名称显示，
        上层也无法判断「这里还差一个名称」。
        """
        res = Y.resolve(["600999"], index=INDEX)
        item = res["items"][0]
        self.assertEqual(item["kind"], Y.KIND_CODE)
        self.assertEqual(item["code"], "600999")
        self.assertEqual(item["name"], "", "名录未收录时名称必须留空")
        self.assertIn("待行情返回后回填", item["note"])
        self.assertEqual(res["summary"]["named"], 0)
        assert_code_invariant(self, res["items"])

    def test_without_index_still_resolves_codes(self):
        """没有名录（快照未就绪）时代码直判仍然可用，只是没有名称。"""
        res = Y.resolve(["600519"], index=None)
        self.assertEqual(res["items"][0]["kind"], Y.KIND_CODE)
        self.assertEqual(res["items"][0]["name"], "")
        self.assertEqual(res["summary"]["indexed"], 0)
        self.assertEqual(res["index"], {"market": Y.MARKET_CN, "count": 0})


class TestResolveUniqueName(unittest.TestCase):
    """用例 9 前半：唯一名称命中（完全匹配 / 唯一前缀）→ kind='name'。

    只有唯一命中才允许自动落定 —— 这是模块最核心的设计取舍。
    """

    def test_exact_name_match(self):
        """名称完全相等 → 直接落定为这只股票（最高档，无歧义空间）。"""
        item = Y.resolve(["贵州茅台"], index=INDEX)["items"][0]
        self.assertEqual(item["kind"], Y.KIND_NAME)
        self.assertEqual(item["code"], "600519")
        self.assertEqual(item["name"], "贵州茅台")
        self.assertEqual(item["market"], Y.MARKET_CN)
        self.assertIn("完全匹配", item["note"])
        self.assertFalse(item["guess"])
        self.assertEqual(len(item["hits"]), 1, "唯一命中也要带回候选，界面可展示匹配档位")

    def test_unique_prefix_match(self):
        """前缀唯一命中同样自动落定（「招商」→ 招商银行），note 说明是名录唯一命中。"""
        item = Y.resolve(["招商"], index=INDEX)["items"][0]
        self.assertEqual(item["kind"], Y.KIND_NAME)
        self.assertEqual(item["code"], "600036")
        self.assertIn("唯一命中", item["note"])

    def test_unique_substring_match(self):
        """子串唯一命中（「茅台」→ 贵州茅台）；这是「随便输名称的一部分」的直观行为。"""
        item = Y.resolve(["茅台"], index=INDEX)["items"][0]
        self.assertEqual(item["kind"], Y.KIND_NAME)
        self.assertEqual(item["code"], "600519")
        self.assertEqual(item["name"], "贵州茅台")


class TestResolveAmbiguous(unittest.TestCase):
    """用例 9 后半：多命中 → ambiguous + code is None + hits 非空，且**绝不自动选定**。

    这是本文件最重要的一组断言。「平安」同时命中 平安银行(000001) 与 中国平安(601318)，
    静默取第一个会把分析对象换成另一只股票，而用户完全不会察觉 —— 这类静默改变
    对象的错误比「识别失败」危险得多（用户会拿着 B 股的走势说服自己 A 股在涨）。
    """

    def test_multi_hit_is_ambiguous_without_code(self):
        """「平安」两义 → ambiguous、code 为 None、hits 带两条候选（最关键的一条断言）。"""
        search = FakeSearch(rows=[{"code": "000001", "name": "平安银行"}])
        res = Y.resolve(["平安"], index=INDEX, search_fn=search)
        item = res["items"][0]
        self.assertEqual(item["kind"], Y.KIND_AMBIGUOUS)
        self.assertIsNone(item["code"], "歧义项绝不能带代码：带了就等于替用户选了一只")
        self.assertIsNone(item["name"], "歧义时没有确定名称")
        self.assertTrue(item["hits"], "必须给出候选清单，否则用户无从选择")
        self.assertEqual([h["code"] for h in item["hits"]], ["000001", "601318"],
                         "候选顺序必须确定（同档名称短优先，再代码升序）")
        self.assertEqual([h["source"] for h in item["hits"]], ["local", "local"])
        self.assertIn("请选择或改用代码", item["note"])
        self.assertEqual(res["summary"]["ambiguous"], 1)
        self.assertEqual(res["summary"]["resolved"], 0)
        assert_code_invariant(self, res["items"])

    def test_local_ambiguity_does_not_hit_remote(self):
        """本地已多命中时不再打远端搜索（省配额，也避免远端把候选搅进来）。

        本地名录是确定事实，远端候选的优先级不可能高于它；多打一轮只是白花配额。
        """
        search = FakeSearch(rows=[{"code": "000001", "name": "平安银行"}])
        Y.resolve(["平安"], index=INDEX, search_fn=search)
        self.assertEqual(search.calls, [])

    def test_ambiguous_kind_never_carries_code(self):
        """几个「像代码又像名字」的歧义输入一起扫：都不能带 code。"""
        res = Y.resolve(["平安", "600", "银行"], index=INDEX)
        self.assertEqual([i["kind"] for i in res["items"]],
                         [Y.KIND_AMBIGUOUS] * 3, "这三条在本名录里都是多命中")
        assert_code_invariant(self, res["items"])
        for item in res["items"]:
            self.assertIsNone(item["code"])


class TestResolveLetterInCn(unittest.TestCase):
    """用例 10：纯字母串在 A 股语境下**先按名称识别**，在美股语境下才走代码路径。

    ``gzmt``（拼音首字母）与美股代码长得完全一样。先当代码会让「输拼音查茅台」永远得到
    一只不存在的美股（GZMT）；先当名称、找不到再退回代码，两种意图都能满足 —— 但这条
    分流必须按**语境**走，美股语境下把一个真实代码当名字去搜同样是错的。
    """

    def test_pinyin_letters_in_cn_go_name_first(self):
        """A 股语境：gzmt 先当名称找 → 远端命中贵州茅台（kind='name'，不是 code）。"""
        search = FakeSearch(table={"gzmt": [{"code": "600519", "name": "贵州茅台"}]})
        res = Y.resolve(["gzmt"], index=INDEX, search_fn=search)
        item = res["items"][0]
        self.assertEqual(item["kind"], Y.KIND_NAME, "A 股语境下纯字母先按名称识别")
        self.assertEqual(item["code"], "600519")
        self.assertEqual(item["name"], "贵州茅台")
        self.assertEqual(item["market"], Y.MARKET_CN)
        self.assertFalse(item["guess"])
        self.assertEqual(search.calls, [("gzmt", Y.MARKET_CN)],
                         "应按原样查询串问远端，并带上 A 股语境")

    def test_same_letters_in_us_go_code_first(self):
        """美股语境：同样的 gzmt 走代码路径（kind='code'），且一次远端都不打。

        与上一条成对：这条差异正是「按语境分流」的全部意义 —— 少了任何一半，
        要么毁掉拼音查询，要么把真实美股代码当名字去搜。
        """
        search = FakeSearch(table={"gzmt": [{"code": "600519", "name": "贵州茅台"}]})
        item = Y.resolve(["gzmt"], market=Y.MARKET_US, index=INDEX,
                         search_fn=search)["items"][0]
        self.assertEqual(item["kind"], Y.KIND_CODE, "美股语境下纯字母是代码，不该当名称搜")
        self.assertEqual(item["code"], "GZMT")
        self.assertEqual(item["market"], Y.MARKET_US)
        self.assertEqual(search.calls, [], "走代码路径时一次远端都不该打")

    def test_pinyin_miss_falls_back_to_code_as_guess(self):
        """A 股语境下拼音没被远端命中时退回代码，但必须标 guess=True。

        这条路径上没有「找到股票」的证据（远端没收录这个拼音，也可能只是网络抖动），
        所以只能算猜测；不标 guess 的话用户会以为 GZMT 是一只真实存在的美股。
        """
        search = FakeSearch(table={"gzmt": []})
        item = Y.resolve(["gzmt"], index=INDEX, search_fn=search)["items"][0]
        self.assertEqual(item["kind"], Y.KIND_CODE)
        self.assertEqual(item["code"], "GZMT")
        self.assertEqual(item["market"], Y.MARKET_US, "按字母形态判为美股代码")
        self.assertTrue(item["guess"])
        self.assertIn("若为拼写错误请检查", item["note"])
        self.assertEqual(search.calls, [("gzmt", Y.MARKET_CN)], "远端确实被问过（只是没命中）")


class TestResolveRemoteEcho(unittest.TestCase):
    """用例 11：远端「回显」只能算猜测，不能算「找到了」。

    搜索接口对「像代码但没收录」的输入会原样回显（code 与 name 都是输入本身），
    它只说明「这个串可能是个代码」，不构成「这只股票存在」的证据。把回显当命中会让
    拼错的代码看起来完全有效，用户拿着空数据下的结论做决策。
    """

    def test_echo_row_is_guess_not_hit(self):
        """远端回显输入（code == name）→ kind='code' 且 guess=True、note 提示可能不存在。"""
        search = FakeSearch(rows=[{"code": "GZMT", "name": "gzmt"}])     # 输入原样回显
        item = Y.resolve(["gzmt"], index=INDEX, search_fn=search)["items"][0]
        self.assertEqual(item["kind"], Y.KIND_CODE)
        self.assertTrue(item["guess"], "回显必须打上「猜的」标记")
        self.assertIn("可能不存在", item["note"])
        self.assertEqual(item["name"], "", "回显不构成名称，名称仍留空待行情回填")
        self.assertEqual(item["market"], Y.MARKET_CN, "回显行未给 market，取调用语境")
        self.assertTrue(item["hits"] and all(h.get("echo") for h in item["hits"]),
                        "候选里必须标明这是回显，界面才能把「猜的」和「确定的」分开")

    def test_real_name_row_is_not_echo(self):
        """远端返回**真实名称**的行才是命中：kind='name' 且 guess=False。

        与上一条成对：同样是远端返回，有没有真实名称决定了「确定命中」还是「猜测」。
        """
        search = FakeSearch(rows=[{"code": "600519", "name": "贵州茅台"}])
        item = Y.resolve(["茅台酒"], index=INDEX, search_fn=search)["items"][0]
        self.assertEqual(item["kind"], Y.KIND_NAME)
        self.assertEqual(item["code"], "600519")
        self.assertEqual(item["name"], "贵州茅台")
        self.assertFalse(item["guess"])
        self.assertNotIn("可能不存在", item["note"])
        self.assertEqual(item["hits"][0]["echo"], False)
        self.assertEqual(item["hits"][0]["source"], "remote")


class TestResolveRemoteMulti(unittest.TestCase):
    """用例 12：远端多候选 —— 同族必须交回用户，异族才允许取首位（并提示可改选）。

    族判断看前两个字：``宁德时代`` / ``宁德新能源`` 这种「同族」候选看着都像，
    自动挑一个几乎必然是错的（用户想要的可能恰好是另一个）；而``微软`` / ``苹果``
    这种一眼能分的候选，取首位 + 在 note 里说明还有候选可改选，兼顾顺手与不静默。
    """

    def test_same_family_candidates_are_ambiguous(self):
        """同族候选（前两字相同）→ ambiguous、code 为 None、全部候选带回。

        查询用「宁德新能源股份」而不是「宁德」：后者会被本地名录前缀命中，
        就走不到远端这条分支了。
        """
        search = FakeSearch(rows=[{"code": "300750", "name": "宁德时代"},
                                  {"code": "300751", "name": "宁德新能源"}])
        item = Y.resolve(["宁德新能源股份"], index=INDEX, search_fn=search)["items"][0]
        self.assertEqual(item["kind"], Y.KIND_AMBIGUOUS)
        self.assertIsNone(item["code"])
        self.assertEqual([h["code"] for h in item["hits"]], ["300750", "300751"])
        self.assertIn("多个相近名称", item["note"])
        assert_code_invariant(self, [item])

    def test_different_family_takes_first_with_note(self):
        """异族候选 → 取首位落定，但 note 必须说明「另有 N 个候选可改选」。"""
        search = FakeSearch(rows=[{"code": "MSFT", "name": "微软", "market": "us"},
                                  {"code": "AAPL", "name": "苹果", "market": "us"}])
        item = Y.resolve(["眉"], index=INDEX, search_fn=search)["items"][0]
        self.assertEqual(item["kind"], Y.KIND_NAME)
        self.assertEqual(item["code"], "MSFT")
        self.assertEqual(item["name"], "微软")
        self.assertEqual(item["market"], Y.MARKET_US, "market 取远端行给的值")
        self.assertFalse(item["guess"])
        self.assertIn("另有 1 个候选可改选", item["note"],
                      "取了首位就必须在 note 里告知还有候选，用户才敢直接用")

    def test_single_remote_hit_is_not_ambiguous(self):
        """远端只回一条真实候选时是确定命中（用于对照上面两条的分界）。"""
        search = FakeSearch(rows=[{"code": "600519", "name": "贵州茅台"}])
        item = Y.resolve(["茅台酒"], index=INDEX, search_fn=search)["items"][0]
        self.assertEqual(item["kind"], Y.KIND_NAME)
        self.assertIn("远端搜索唯一命中", item["note"])


class TestResolveRemoteFailure(unittest.TestCase):
    """用例 13：远端缺失 / 脏返回 / 抛异常都不能让整批识别失败，且要落到 unknown 并说明。

    单条输入的识别失败只影响它自己；远端是弱依赖（配额、限流、字段变动都常见），
    它挂了不能把「输代码」这条零成本路径也一起拖垮，更不能让整个识别接口 500。
    """

    def test_missing_or_unusable_search_fn(self):
        """search_fn 为 None / 非可调用对象时，仍要给出 unknown + 说明试过哪些途径。"""
        for fn in (None, "not-callable", 0, []):
            with self.subTest(fn=repr(fn)):
                item = Y.resolve(["某名称"], index=INDEX, search_fn=fn)["items"][0]
                self.assertEqual(item["kind"], Y.KIND_UNKNOWN)
                self.assertIsNone(item["code"])
                self.assertIn("本地名录", item["note"])
                self.assertIn("远端", item["note"])

    def test_dirty_remote_results_are_tolerated(self):
        """远端返回 None / 空 dict / 非 dict 字符串 → unknown，不抛异常。

        这几个形态分别对应「接口挂了」、「接口返回空」、「接口返回了 HTML/文本」。
        """
        for result in (None, {}, "oops", b"", 0):
            with self.subTest(result=repr(result)):
                res = Y.resolve(["某名称"], index=INDEX, search_fn=FakeSearch(result=result))
                self.assertEqual(res["items"][0]["kind"], Y.KIND_UNKNOWN)
                assert_consistent(self, res)

    def test_bare_list_result_is_supported(self):
        """裸 list（不带 {"rows": ...} 外壳）也要能识别 —— 文档里明确支持这种形态。

        接口层将来直接返回 ``rows`` 列表时不应该变成「全部未识别」。
        """
        search = FakeSearch(result=[{"code": "600519", "name": "贵州茅台"}])
        item = Y.resolve(["茅台酒"], index=INDEX, search_fn=search)["items"][0]
        self.assertEqual(item["kind"], Y.KIND_NAME)
        self.assertEqual(item["code"], "600519")

    def test_exception_is_swallowed(self):
        """远端抛异常 → 吞掉并落到 unknown（其余标的照常识别）。"""
        search = FakeSearch(raises=RuntimeError("远端不可达"))
        res = Y.resolve(["某名称", "600519"], index=INDEX, search_fn=search)
        self.assertEqual([i["kind"] for i in res["items"]], [Y.KIND_UNKNOWN, Y.KIND_CODE])
        self.assertEqual(res["items"][1]["code"], "600519", "远端故障不能影响代码直判")
        assert_consistent(self, res)

    def test_dirty_rows_are_skipped(self):
        """远端行里的 None / 数字 / 空 dict / 缺 code 的行只被跳过，不影响有效行。"""
        search = FakeSearch(rows=[{"code": "600519", "name": "贵州茅台"},
                                  None, 42, {}, {"code": ""}, "x"])
        item = Y.resolve(["茅台酒"], index=INDEX, search_fn=search)["items"][0]
        self.assertEqual(item["kind"], Y.KIND_NAME)
        self.assertEqual(item["code"], "600519")
        self.assertEqual(len(item["hits"]), 1, "脏行不产出候选")

    def test_dirty_rows_do_not_consume_the_limit_budget(self):
        """脏行先过滤再计数：头部混入脏行不应把靠后的有效候选挤掉。

        曾经是「先按 limit 切片、再过滤脏行」，于是「5 条脏 + 第 6 条有效」在 limit=5
        时识别失败 —— 配额被脏行吃掉了。远端返回结构一旦变化（多包一层、混入占位行），
        这种失败表现为「有时识别不出来」，极难排查，因此改成先过滤再计数。
        """
        rows = [None, 42, {}, {"code": ""}, "x", {"code": "600519", "name": "贵州茅台"}]
        for cap in (1, 5, 6):
            got = Y.resolve(["茅台酒"], index=INDEX, search_fn=FakeSearch(rows=rows), limit=cap)
            self.assertEqual(got["items"][0]["kind"], Y.KIND_NAME, "limit=%d 时应仍能命中" % cap)
            self.assertEqual(got["items"][0]["code"], "600519")


class TestResolveDedupe(unittest.TestCase):
    """用例 14：按 market:code 去重（同一只股票只占一条），不同市场同代码不算重复。

    用户常见的输入是「600519 茅台 贵州茅台」这种重复罗列。若不去重，同一只股票会在
    结果里出现三行、并且**三次抢占组合资金**（core/advisor 的权重是按行分配的），
    仓位会被同一只股票重复吃掉。
    """

    def test_code_and_name_collapse_to_one_item(self):
        """「600519」+「茅台」是同一只 → 只产出一条 item（保留首次出现的那条）。"""
        res = Y.resolve(["600519", "茅台"], index=INDEX)
        self.assertEqual(len(res["items"]), 1, "同一只股票只产出一条 item")
        self.assertEqual(res["items"][0]["kind"], Y.KIND_CODE, "保留首次出现的那条")
        self.assertEqual(res["items"][0]["code"], "600519")
        self.assertEqual(res["summary"]["total"], 1)
        assert_consistent(self, res)

    def test_first_occurrence_wins(self):
        """先给名称再给代码时，保留名称那条（顺序优先，不做「代码优先」的重排）。

        保持输入顺序是前端「按用户输入顺序展示」的前提。
        """
        res = Y.resolve(["茅台", "600519"], index=INDEX)
        self.assertEqual(len(res["items"]), 1)
        self.assertEqual(res["items"][0]["kind"], Y.KIND_NAME)
        self.assertEqual(res["items"][0]["raw"], "茅台")

    def test_same_code_in_two_markets_is_kept(self):
        """不同市场 + 同代码是两只不同标的，不能去重成一条。

        去重键必须是 market:code 而不是 code —— 否则美股与 A 股的相同代码会被合并，
        其中一只被静默丢弃。
        """
        res = Y.resolve([{"code": "60051", "market": "cn"}, {"code": "60051", "market": "us"}],
                        index=INDEX)
        self.assertEqual(len(res["items"]), 2)
        self.assertEqual({i["market"] for i in res["items"]}, {Y.MARKET_CN, Y.MARKET_US})
        self.assertEqual([i["kind"] for i in res["items"]], [Y.KIND_CODE, Y.KIND_CODE])
        assert_consistent(self, res)

    def test_six_digit_code_is_always_cn(self):
        """6 位数字无论调用语境一律判 A 股，因此 cn/us 两种写法会归一到同一只。

        这条锁的是「数字不歧义」的规则：A 股代码形态本身就是市场信息，不该被
        调用方传进来的 market 改写（否则同一只股票会按两个市场各分析一次）。
        """
        for tokens in (["600519"], [{"code": "600519", "market": "us"}]):
            with self.subTest(tokens=repr(tokens)):
                res = Y.resolve(tokens, market=Y.MARKET_US, index=INDEX)
                self.assertEqual(len(res["items"]), 1)
                self.assertEqual(res["items"][0]["market"], Y.MARKET_CN)


class TestResolveLimits(unittest.TestCase):
    """用例 15：超过 max_tokens 时截断，且 summary.truncated 必须为真。

    上限与 AI 选股接口、推送通道同口径（MAX_TOKENS=60）：不截断会让一次输入打满上游
    配额并拖长响应；截断而不告知，用户会以为「全都识别了」。
    """

    def test_custom_cap_truncates(self):
        """自定义 max_tokens 生效：items 数等于上限且 summary.truncated 为真。"""
        res = Y.resolve(["600519", "600036", "000001", "601318", "300750"],
                        index=INDEX, max_tokens=3)
        self.assertTrue(res["summary"]["truncated"])
        self.assertEqual(len(res["items"]), 3, "items 数必须等于上限")
        self.assertEqual(res["summary"]["total"], 3)
        assert_consistent(self, res)

    def test_default_cap_is_max_tokens(self):
        """默认上限就是 MAX_TOKENS；恰好等于上限时不算截断（边界不差一）。"""
        self.assertEqual(Y.MAX_TOKENS, 60)
        tokens = ["600%03d" % i for i in range(Y.MAX_TOKENS + 5)]
        res = Y.resolve(tokens, index=INDEX)
        self.assertTrue(res["summary"]["truncated"])
        self.assertEqual(len(res["items"]), Y.MAX_TOKENS)
        edge = Y.resolve(tokens[:Y.MAX_TOKENS], index=INDEX)
        self.assertFalse(edge["summary"]["truncated"])
        self.assertEqual(len(edge["items"]), Y.MAX_TOKENS)

    def test_none_max_tokens_falls_back_to_default(self):
        """max_tokens=None（前端清空输入框）回退默认上限，而不是「不限」或 0 条。"""
        res = Y.resolve(["600%03d" % i for i in range(Y.MAX_TOKENS + 5)],
                        index=INDEX, max_tokens=None)
        self.assertTrue(res["summary"]["truncated"])
        self.assertEqual(len(res["items"]), Y.MAX_TOKENS)


class TestResolveDirty(unittest.TestCase):
    """用例 16：脏输入不抛异常，且 summary 各计数自洽。

    tokens 直接来自前端表单与 URL query，None / 数字 / 空串 / 超长串都可能出现。
    「不抛异常」在这里比「识别正确」更重要：一个脏 token 不能让整批已识别好的
    标的全部丢掉（那是用户肉眼可见的「白输了」）。
    """

    def test_dirty_token_list_never_raises(self):
        """一条脏 token 不能带走整批：None/数字/空串/空 dict/超长串只影响自己。"""
        tokens = [None, 123, "", "   ", "\t\n", {}, {"code": ""}, "x" * 300,
                  {"code": 600519}]
        res = Y.resolve(tokens, index=INDEX)
        assert_consistent(self, res)
        assert_code_invariant(self, res["items"])
        self.assertEqual([i["kind"] for i in res["items"]],
                         [Y.KIND_UNKNOWN, Y.KIND_UNKNOWN, Y.KIND_CODE])
        self.assertEqual(res["items"][0]["raw"], "123", "数字 token 当未知输入处理")
        self.assertEqual(len(res["items"][1]["raw"]), 300, "超长串原样带回，便于界面回显")
        self.assertEqual(res["items"][2]["code"], "600519", "数字型 code 也要能识别")

    def test_none_and_empty_token_containers(self):
        """tokens 为 None / 空列表 / 空元组 → 空结果，summary 全 0 且不自相矛盾。"""
        for tokens in (None, [], ()):
            with self.subTest(tokens=repr(tokens)):
                res = Y.resolve(tokens, index=INDEX)
                self.assertEqual(res["items"], [])
                self.assertEqual(res["summary"]["total"], 0)
                self.assertEqual(res["summary"]["indexed"], INDEX["count"])
                self.assertFalse(res["summary"]["truncated"])
                assert_consistent(self, res)

    def test_dict_without_code_is_skipped(self):
        """只给 ``name`` 的字典被静默跳过（当前口径：只认 code/raw/symbol 三键）。

        说明：给出的是「没有任何可识别键」的条目，因此不进结果；只给 ``raw``（名称）
        的情况另有缺陷，见 TestKnownDefects 的 D5。
        """
        res = Y.resolve([{"name": "贵州茅台"}], index=INDEX)
        self.assertEqual(res["summary"]["total"], 0)
        assert_consistent(self, res)

    def test_dict_key_aliases(self):
        """``code`` / ``symbol`` / ``raw`` 三种键都能定位到标的，market 缺省取调用语境。"""
        for token in ({"code": "600519"}, {"symbol": "600519"}, {"raw": "600519"}):
            with self.subTest(token=repr(token)):
                item = Y.resolve([token], index=INDEX)["items"][0]
                self.assertEqual(item["kind"], Y.KIND_CODE)
                self.assertEqual(item["code"], "600519")
                self.assertEqual(item["name"], "贵州茅台")


class TestResolveSummaryJson(unittest.TestCase):
    """用例 17-18：summary 自洽 + 结果可通过严格 JSON 序列化。

    项目的所有接口都用 ``json.dumps(..., allow_nan=False)`` 的严格口径（core/advisor
    与 server 一致）；识别结果里只要混进一个 NaN/inf，整包响应就会 500，
    前端连「哪一条识别失败」都看不到。
    """

    def mixed_batch(self):
        """混合批次：代码 / 名称 / 歧义 / 未知 / 拼音（远端命中后又与代码去重）。"""
        search = FakeSearch(table={"gzmt": [{"code": "600519", "name": "贵州茅台"}]})
        return Y.resolve(["600519", "茅台", "平安", "无此标的", "gzmt", "600999"],
                         index=INDEX, search_fn=search)

    def test_summary_identities(self):
        """混合批次逐项核对：去重、四类 kind 的数量、named/guessed 与 items 一致。"""
        res = self.mixed_batch()
        assert_consistent(self, res)
        assert_code_invariant(self, res["items"])
        # 逐项核对（去重与计数一次说清）：
        #   600519 → code（带名录名称）；茅台 → 同一只，被去重；
        #   平安 → ambiguous；无此标的 → unknown；gzmt → 远端命中 600519，去重；
        #   600999 → code（名录未收录，名称留空）
        self.assertEqual([i["raw"] for i in res["items"]],
                         ["600519", "平安", "无此标的", "600999"])
        self.assertEqual(res["summary"], {
            "total": 4, "resolved": 2, "code": 2, "name": 0, "ambiguous": 1,
            "unknown": 1, "named": 1, "guessed": 0, "indexed": 6, "truncated": False,
        })

    def test_strict_json_export(self):
        """allow_nan=False 下可序列化，且序列化后的 summary 与原对象一致。"""
        cases = {
            "mixed": self.mixed_batch(),
            "echo": Y.resolve(["gzmt"], index=INDEX,
                              search_fn=FakeSearch(rows=[{"code": "GZMT", "name": "gzmt"}])),
            "dirty": Y.resolve([None, 123, "", "x" * 300], index=INDEX),
            "empty": Y.resolve(None, index=INDEX),
            "remote_multi": Y.resolve(["宁德新能源股份"], index=INDEX,
                                      search_fn=FakeSearch(rows=[
                                          {"code": "300750", "name": "宁德时代"},
                                          {"code": "300751", "name": "宁德新能源"}])),
        }
        for name, res in cases.items():
            with self.subTest(case=name):
                text = json.dumps(res, allow_nan=False)   # 与项目严格 JSON 出口同一口径
                self.assertNotIn("NaN", text)
                self.assertNotIn("Infinity", text)
                back = json.loads(text)
                self.assertEqual(back["summary"], res["summary"])
                self.assertEqual(len(back["items"]), len(res["items"]))

    def test_result_shape(self):
        """顶层结构契约：items / summary / index / note 四段齐全，note 说明识别顺序。"""
        res = Y.resolve(["600519"], index=INDEX)
        self.assertEqual(sorted(res.keys()), ["index", "items", "note", "summary"])
        self.assertIn("代码直判", res["note"])
        self.assertIn("远端搜索", res["note"])
        self.assertEqual(res["index"], {"market": Y.MARKET_CN, "count": INDEX["count"]})


# --------------------------------------------------------------------------- #
# D. 与 AI 选股联动（名称回填）
# --------------------------------------------------------------------------- #
BEGIN = date(2024, 1, 2)
#: 合成K线根数：刚好越过 advisor.MIN_BARS=60。本组只验证**名称回填**，不复算策略，
#: K线越短 recommend 越快（80 根约 0.07 秒），整个文件才能压进 3 秒
BARS_N = 80


def make_bars(n=BARS_N, amp=0.02, period=7.0, slope=0.0015):
    """确定性合成日线：正弦包络 + 线性漂移，价格恒为正、日历严格递增。

    刻意不用 ``random``：默认种子依赖系统熵源，会让「同一份K线」在不同机器上不同，
    回填之外的断言（如价格）就不再可复现。
    """
    out = []
    for i in range(n):
        close = 100.0 * (1.0 + amp * math.sin(i / period)) * ((1.0 + slope) ** i)
        prev = out[-1]["close"] if out else close
        out.append({"t": (BEGIN + timedelta(days=i)).isoformat(), "open": prev,
                    "high": max(prev, close) * 1.002, "low": min(prev, close) * 0.998,
                    "close": close, "volume": 1000000 + i})
    return out


BARS = make_bars()


class FakeFeed(object):
    """假数据源：实现 recommend 的注入契约（fetch_bars(market, code, period, limit) /
    fetch_quote(market, code)），只回内存里的合成K线与预设行情，并记录调用参数。

    ``quote_fn`` 由用例提供，用来构造「行情带名称 / 只带价格 / 空 dict / 抛异常」四种形态。
    """

    def __init__(self, quote_fn=None):
        self.quote_fn = quote_fn
        self.calls = []

    def bars(self, market, code, period, limit):
        self.calls.append(("bars", market, code, period, limit))
        return list(BARS)[:limit]

    def quote(self, market, code):
        self.calls.append(("quote", market, code))
        return self.quote_fn(market, code) if self.quote_fn else None


def run_recommend(symbols, quote_fn):
    """统一入口：注入假数据源、单线程（保证 rows 顺序 = 输入顺序），不联网。"""
    feed = FakeFeed(quote_fn)
    res = A.recommend(symbols, fetch_bars=feed.bars, fetch_quote=feed.quote,
                      market="cn", max_workers=1)
    return res, feed


class TestAdvisorNameBackfill(unittest.TestCase):
    """用例 19-20：行情返回的名称回填到 row.name（core/advisor._analyze_one 那一段）。

    为什么需要回填：调用方（core/symbols 的识别层）对「只输代码」的输入只能给出代码
    （它刻意不去远端取名称，因为取报价时本来就会拿到）；界面与历史记录里若只有
    「600519」这样的数字，用户得自己背代码，而回填是零成本的。
    """

    def test_quote_name_fills_code_only_symbol(self):
        """只给代码时 row.name 变成行情返回的名称。"""
        res, feed = run_recommend([{"code": "600519", "market": "cn"}],
                                  lambda m, c: {"price": 1500.0, "changePct": 1.25,
                                                "name": "贵州茅台"})
        row = res["rows"][0]
        self.assertTrue(row["ok"], row.get("error"))
        self.assertEqual(row["code"], "600519")
        self.assertEqual(row["name"], "贵州茅台", "行情返回的名称应回填到 row.name")
        self.assertEqual(row["price"], 1500.0, "回填不影响行情价格")
        self.assertTrue(any(c[0] == "quote" for c in feed.calls), "确实取了实时行情")

    def test_caller_name_is_never_overwritten(self):
        """调用方明确给出的名称（用户备注）不能被行情名称覆盖。

        这是回填的唯一红线：界面上的「我的备注」是用户自己写的，被行情名悄悄换掉
        等于替用户改数据 —— 用户会以为备注丢了或系统在自作聪明。
        """
        res, _ = run_recommend([{"code": "600519", "market": "cn", "name": "我的备注"}],
                               lambda m, c: {"price": 1500.0, "changePct": 1.25,
                                             "name": "贵州茅台"})
        row = res["rows"][0]
        self.assertTrue(row["ok"], row.get("error"))
        self.assertEqual(row["name"], "我的备注")

    def test_backfill_replaces_name_that_equals_code(self):
        """名称恰好等于代码时（_normalize_symbols 对没给名称的输入就是这么填的）要替换。

        与上一条成对：判定条件是「没名称 **或** 名称就是代码」，否则「只输代码」
        这条最常见路径永远拿不到名称。
        """
        res, _ = run_recommend([{"code": "600519", "market": "cn", "name": "600519"}],
                               lambda m, c: {"price": 1500.0, "changePct": 1.25,
                                             "name": "贵州茅台"})
        self.assertEqual(res["rows"][0]["name"], "贵州茅台")

    def test_name_falls_back_to_code_without_quote_name(self):
        """行情没给名称（空 dict / 只有价格 / 抛异常 / 未收录）时回退为代码，不抛异常。

        行情源的字段完整度不由我们控制：只要它偶尔不带 name，就不能让整行失败，
        更不能把 name 变成 None（界面会渲染出「null」）。
        """
        def quote_fn(market, code):
            if code == "600519":
                return {}                                    # 空行情（falsy）
            if code == "600036":
                return {"price": 40.0}                       # 有价无名称
            if code == "000001":
                raise RuntimeError("quote down")             # 行情源故障
            return None                                      # 未收录

        symbols = [{"code": c, "market": "cn"}
                   for c in ("600519", "600036", "000001", "002415")]
        res, _ = run_recommend(symbols, quote_fn)
        self.assertEqual([r["ok"] for r in res["rows"]], [True] * 4,
                         "行情缺失不影响分析本身（用最后一根收盘价兜底）")
        self.assertEqual([r["name"] for r in res["rows"]],
                         ["600519", "600036", "000001", "002415"],
                         "没有行情名称时回退为代码")
        self.assertEqual([r["code"] for r in res["rows"]],
                         ["600519", "600036", "000001", "002415"], "顺序与输入一致")


# --------------------------------------------------------------------------- #
# E. 已发现缺陷的锚点（不允许修改 core/symbols.py，故用 expectedFailure）
# --------------------------------------------------------------------------- #
class TestFixedDefects(unittest.TestCase):
    """本文件编写过程中实测到、**已修复**的 5 处缺陷的回归测试。

    为什么保留：它们描述的是「正确行为」，一旦有人把修好的逻辑改回去，套件立刻变红 ——
    比在代码里留一句注释更难被绕过。五处依次是：
    ① 远端返回非 dict / 非列表的脏值会在切片时抛 TypeError；
    ② 只给 raw/name 的字典被当成「代码识别成功」，产出 code 为空串的已识别项；
    ③ 全角空格分隔的「名称　代码」无法识别（中文输入法下的常见写法）；
    ④ 市场后缀（`nvda:US`）被当成显示名称，界面上会一直显示「名称：US」；
    ⑤ 单字查询在前缀档位缺少长度保护，可被自动落定成「碰巧命中」的股票。
    """

    def test_d1_non_subscriptable_remote_result_must_not_raise(self):
        """D1：远端返回 42 这类非 dict、不可下标的脏响应时不应抛异常（应落到 unknown）。

        复现：``resolve(["某名称"], search_fn=lambda q, m: 42)`` →
        ``TypeError: 'int' object is not subscriptable``（异常从 ``_remote_hits`` 冒出来，
        因为它只把 ``search_fn(...)`` 一句包在 try 里，后面的 ``(rows or [])[:limit]``
        在切片前没做「可迭代」检查）。
        影响：远端被换成一个返回非标准结构的实现（或中间层返回了文本/数字）时，
        整个识别接口 500 —— 而这一步只是「本地没命中后的尽力而为」，绝不该是致命路径。
        建议修法：``rows = res.get("rows") if isinstance(res, dict) else res`` 之后补一句
        ``if not isinstance(rows, (list, tuple)): return []``。
        """
        try:
            res = Y.resolve(["某名称"], index=INDEX, search_fn=FakeSearch(result=42))
        except TypeError as exc:                      # pragma: no cover - 当前实现会走到这里
            self.fail("远端脏返回值不该让 resolve 抛异常：%s" % exc)
        self.assertEqual(res["items"][0]["kind"], Y.KIND_UNKNOWN)

    def test_d5_dict_with_only_raw_name_must_not_yield_empty_code(self):
        """D5：字典形态只给 ``raw``（名称）时应按名称识别，而不是产出 code='' 的 code 项。

        复现：``resolve([{"raw": "贵州茅台"}], index=INDEX)`` →
        ``kind='code', code='', name=''``，且 summary 里 ``resolved=1``。
        根因：``incoming`` 是一个**非空 dict**（即使它的 code 是空串），
        ``if code_first and (parsed or incoming)`` 因此成立，随后 ``code =
        (parsed or incoming)["code"]`` 拿到空串；而构造 items 时的去重守卫是
        ``if code:``（空串为假）→ 不报错、也没被过滤，直接产出空代码项。
        影响：前端会把它当成「已识别」的一行并带着空 code 去请求分析；
        如果上游按 code 找行情就会得到一只不存在的标的。
        建议修法：``incoming`` 只在 code 非空时才算「像代码」，例如
        ``src = parsed or (incoming if (incoming or {}).get("code") else None)``。
        """
        item = Y.resolve([{"raw": "贵州茅台"}], index=INDEX)["items"][0]
        self.assertNotEqual(item["code"], "", "不能产出空代码的已识别项")
        self.assertEqual(item["kind"], Y.KIND_NAME)
        self.assertEqual(item["code"], "600519")

    def test_d2_fullwidth_space_separated_code_and_name(self):
        """D2：全角空格（中文输入法的默认空格）分隔的「名称 代码」应与半角一致。

        复现：``parse_token("贵州茅台\\u3000600519")`` → ``None``。
        根因：``parse_token`` 用 ``text``（原样）里的 ``":" / "：" / " "`` 做切分，
        全角空格 U+3000 三种都不匹配；等轮到 ``normalize_text`` 时它先被转成半角空格、
        紧接着被 ``_NOISE`` 当作噪声删掉，于是变成 ``贵州茅台600519`` 这种既不匹配
        代码正则、又不可能命中名录名称的串，最终落到 unknown。
        影响：模块把「中文输入法下的全角字符」列为要解决的问题之一（normalize_text 的
        docstring 明确提到），却在最需要它的「名称+代码」写法上失效 —— 属于「明明输对了
        却识别不出来」。
        建议修法：把分隔符集合扩成 ``(":", "：", " ", "\\u3000")``，或先把全角空格
        替换成半角空格再做切分（一行即可）。
        """
        parsed = Y.parse_token("贵州茅台\u3000600519")
        self.assertIsNotNone(parsed, "全角空格分隔的「名称 代码」应能切出代码")
        self.assertEqual(parsed["code"], "600519")
        self.assertEqual(parsed["name"], "贵州茅台")

    def test_d3_single_char_query_must_not_auto_match(self):
        """D3：单字查询在前缀档位也要有最小长度保护，不能自动落定成一只股票。

        复现：``match_local("中", INDEX)`` → 命中「中国平安」（名称前缀档）；
        ``resolve(["中"], index=INDEX)`` → ``kind='name', code='601318'``。
        根因：子串档位有 ``len(q) >= 2`` 保护，前缀档位却是 ``len(q) >= 1``，
        而「唯一命中即自动落定」的规则没有区分这个命中是几个字换来的。
        影响：在全市场名录里，单字通常命中很多条（于是变成一堆噪声候选）；但对于
        「只有一个名称以该字开头」的字（如「茅」不在词首时会落空，而「中」会命中），
        一个手滑的单字输入就会被静默落定成一只股票 —— 与「只有确定命中才自动选定」
        的设计原则相冲突。
        建议修法：前缀档位也要求 ``len(q) >= 2``（单字一律返回空，提示用户补一个字）。
        """
        self.assertEqual(Y.match_local("中", INDEX), [], "单字不应命中任何本地候选")
        item = Y.resolve(["中"], index=INDEX)["items"][0]
        self.assertNotEqual(item["kind"], Y.KIND_NAME, "单字不应被自动落定成名称")
        self.assertNotEqual(item["code"], "601318")

    def test_d4_market_suffix_is_not_a_display_name(self):
        """D4：``nvda:US`` 的 ``US`` 是市场后缀，不应落进 name。

        复现：``parse_token("nvda:US")`` → ``{"code": "NVDA", "market": "us",
        "name": "US", ...}``。
        根因：切分后 ``US`` 被当成「名称侧」原样保留（它确实不是代码形态之外的噪声）。
        影响：market 靠字母形态已经判对，但 name='US' 会被上层当成「调用方给出的名称」，
        于是 core/advisor 的名称回填（只在无名称或名称==代码时替换）不再生效，
        界面与历史记录里会长期显示「名称：US」。
        建议修法：切分时若 tail 命中了市场后缀（US/O/N 或 SH/SZ/BJ），把它当市场标记
        而不是名称；至少不要把它写进 name。
        """
        self.assertEqual(Y.parse_token("nvda:US")["name"], "")
        self.assertEqual(Y.parse_token("600519:SH")["name"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
