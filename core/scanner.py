# -*- coding: utf-8 -*-
"""AlphaDesk · 全市场「值得买入」候选扫描器（core/scanner.py）

定位
----
回答一个问题：**「把全市场快照丢进来，现在哪几只值得买入、为什么、风险多大」**。
与 `core/advisor.py` 的分工（避免重复实现，这是本模块的第一条设计约束）：

======================================  ==================================================
`core/advisor.py`（已有）                单标的深挖：7 策略共识票 / 统计优势回放 / 凯利仓位 /
                                        历史条件分布预测（forecast）/ 组合分配
`core/scanner.py`（本模块）              全市场粗筛：三道硬闸门 → 复合评分 → 可解释等级，
                                        只回答「进不进初选名单」，不回答「买多少 / 目标价」
======================================  ==================================================

· 技术指标**全部复用** `core.indicators`（SMA / RSI / ATR），本模块不重新实现任何指标；
· 仓位与资金分配**不做**，直接复用 `core/kelly.py`（`allocate` / `fractional`），
  本模块只在输出里给出 `score` / `grade` 供上层决定要不要送去 advisor 深算；
· 预测（`forecast`）与 AI 建议（`recommend`）**不重算**，避免同一标的出现两套口径；
· 因此本模块只有 4 个公开入口：`hard_filters` / `score_candidate` / `risk_score` /
  `scan_candidates`（外加 `composite_score` / `grade_of` 两个纯函数便于测试与复用）。

设计依据与出处（本轮联网调研结论，逐条落实；引用为调研所引资料，未做逐条原始文献复核）
--------------------------------------------------------------------------------
1. **三道硬闸门先过滤、再打分排序**（InStock 全市场筛选流程）：
   ①流动性下限（成交额 ≥ 2 亿元，可配）；②反追高（当日涨幅上限，默认 ≤ 7%，避免当天
   已经冲高）；③量比 / 资金门槛。硬闸门不通过的直接进 `rejected` 并给出中文原因，
   **不参与打分**（见 `hard_filters`）。
2. **抗单日异动的量能口径**（DuckDB 三层过滤方案：先粗筛流动性、再看量能结构、最后
   技术形态）：量比（当日量 / 近 5 日均量）≥ 1.2 **或** 5 日均额 / 20 日均额 ≥ 1.3，
   后者不会被单日异常成交额带偏，二者满足其一即放行。
3. **反「已经涨上天」**（InStock「无大幅回撤」策略 + Penny 的反拉抬分）：
   ①近 60 日累计涨幅 > 80% 扣分、≥ 150% 直接剔除；②近 60 日出现单日跌幅 > 7%
   或两日累计跌幅 > 10% 记为回撤风险；③Penny 式「极端放量 + 极端涨幅 + 过热」合成
   **0–10 风险分**：5–6 分 = HIGH / AVOID，7 分及以上 = CRITICAL / AVOID，并支持按
   阈值整只剔除（`params["reject_risk_score"]`，默认 7，见 `risk_score`）。
4. **A 股短期（< 4 个月）偏反转、不是动量**（《中国A股市场动量效应和反转效应》等
   实证：A 股中短期表现为反转效应，中长期才可能动量）。本工具持有 5–20 个交易日，
   正落在短期区间，因此 `momentum` 因子**不把「近 5 日涨幅越高」当正分**，而按
   「温和走强 + 不过热」给分：RSI 50–68 满分、RSI > 75 反而扣分、近 5 日涨幅 > 12%
   归零。同时 **低换手更优**（6 个月平均换手率分组单调性 0.74，高换手组持续跑输），
   故换手率过高扣分（计入 `volume` 因子）。
5. **单指标不可独立成信号**（Sullivan–Timmermann–White（1999, Journal of Finance
   54(5): 1647–1691）用 7,846 条技术规则 + White's Reality Check 做数据窥探校正后，
   最优规则不再显著）：RSI / ADX 等只能作为**过滤器或加权项之一**，不得作为唯一
   买入理由；`reasons` 中不出现「因为 RSI 超卖所以买入」这类单因子结论，每条 reason
   都同时带上权重与其它证据。
6. **复合评分与可解释等级**（Penny 的 Composite）：
   技术（趋势 + 位置）0.40 + 量能 0.25 + 风险（反向）0.25 + 趋势强度/相对强弱 0.10，
   映射 **A(80–100) / B(65–79) / C(50–64) / D(35–49) / F(0–34)**；`verdict` 是
   「一句话说清为什么」的中文结论（前端只展示等级 + 这句话，分数与拆解放二级）。
7. **突破打分参考**（Penny：RVOL ≥ 2、RSI > 50、价 > MA20、距阻力位、价 > 20 日高点）：
   这些要素**没有照抄它的 25/20/20/20/15 分值**，而是折进本模块的五个因子里
   （RVOL 与均额比 → `volume`，RSI 与涨幅 → `momentum`，价 vs 均线 → `trend`，
   距阻力位 / 是否新高 → `position`，见「因子实现」小节），这样每个因子仍是 0–100 的
   同尺度量，可直接套复合权重。唯一**方向反转**的是「距阻力位 3% 以内」：Penny 把它
   当突破前的加分项，本模块按调研结论的落地要求（「太近 = 空间不足扣分、适度距离 =
   满分」）反向处理 —— 因为本工具的持有期只有 5–20 个交易日，贴着阻力位买入一旦突破
   失败就没有缓冲，属于追高；已突破（上方无阻力）另给 60 分中性偏正。

诚实性约束（必须）
------------------
· **数据缺失一律降级，不臆造**：`bars_map` 里没有 K 线的标的 → 直接 `rejected`，
  原因「无K线数据」，绝不用快照数据编造技术面得分；快照字段缺失（如 `amount` /
  `turnover` / `changePct`）→ 跳过对应闸门并在 `warnings` 记一条，同时在
  `stats["missingFields"]` 统计「因字段缺失被跳过的闸门数」；
· 若某因子的底层字段取不到，该子项从分子分母中同时剔除（按可用子项归一化），
  并在 `warnings` 说明降级，不做任何插值猜测；
· 出口统一清洗：`NaN` / `±inf` 一律转 `None`，保证 `json.dumps(..., allow_nan=False)`
  一定通过；
· 任何脏输入（`rows=None`、非 dict 行、缺字段、字符串数值、空 `bars_map`）都不抛异常。

口径与单位（前端按此消费，勿混用）
----------------------------------
· `score` 0–100 分整数；`risk.score` 0–10；`factors.*` 每个 0–100（`risk` 已反向：
  越高越安全）；`grade` ∈ A/B/C/D/F；`verdict` 一句话中文；`verdict3` 是三档口径
  （值得买入 / 观察 / 回避，按 `VERDICT_BUCKETS` 聚合），供只展示三档的接口使用；
· 百分比字段（`changePct` / `pct60d` / `maxDrop1d` / `ma20Dev` / `distToResistance` /
  `ret5` / `ret20` / `atrPct` / `turnover`）都是**百分数**（`7.0` 表示 7%）；
· `amount` 按各自市场本币（A 股元 / 美股美元），不做汇率换算，`volumeRatio` /
  `amountRatio` 是无量纲倍数；`spark` 为最近不超过 30 根收盘价（供前端画迷你走势图）。

快照与 K 线的字段优先级（每个指标的取值来源都在 `metrics.*Source` 里标出）
------------------------------------------------------------------------
· `volumeRatio`（量比）：**快照优先** —— 供应商的量比是盘中时间加权口径，比按日线
  简单平均更贴近实时；快照没有时才用 K 线算「当日量 / 前 5 日均量」；
· `amountRatio`（5/20 日均额比）、`pct60d`（近 60 日涨幅）：**K 线优先** —— 只要 K 线
  够长（分别 20 / 61 根）就用 K 线自己算，口径唯一、可复现（快照的 `chg60d` 可能是
  自然日窗口，与「60 个交易日」不等价）；K 线不够时回退快照字段；
· `changePct` / `turnover` / `amount`：**快照优先**，`changePct` 缺失时才用最后一根
  K 线推算，并在 `metrics.changePctSource` 与 `warnings` 里如实标注。

已知取舍（如实说明，不是漏项）
------------------------------
· 量比在 `volume` 因子（适度放量 = 有资金关注）与 `risk_score`（极端放量 = 拉抬风险）
  中各自承担不同语义，属有意为之；但同一指标两处出现，极端值会被双重计入，故
  `risk_score` 只对「极端」区间给分（≥ 2 倍起），避免与 `volume` 因子重复惩罚；
· 换手率只计入 `volume` 因子（低换手加分 / 高换手扣分），**不再重复进 `risk_score`**；
· 「已突破（上方无阻力）」给 60 分中性偏正：它不再是被阻力压制的形态，但也可能
  已是追高位，故不给满分；
· **评分封顶（超出现有调研结论的加严项）**：动量权重只有 0.10、位置 0.20，单靠
  扣分拦不住「已经涨上天」的标的，因此额外做两条封顶 —— 风险分达 AVOID（HIGH）→
  总分 ≤ 49，CRITICAL 或命中整只剔除项 → 总分 ≤ 34，过热（RSI > 75 / 近 5 日涨幅
  > 12% / 近 60 日涨幅 ≥ 80%）→ 总分 ≤ 64；`rawScore` 保留未封顶的合成分以便审计；
· 本模块不判断 T+1、涨跌停、停牌、退市与 ST，A 股 / 美股的差异只体现在可配的
  流动性下限、涨幅上限与换手率口径上（`params["min_amount"]` / `max_change_pct`
  支持标量或 `{"cn": .., "us": ..}` 两种写法）。

纯标准库实现，无网络请求；K 线由调用方通过参数注入。
"""

from __future__ import annotations

import math

from . import indicators as I

__all__ = [
    "DEFAULT_SCAN_PARAMS", "FACTORS", "FACTOR_CN", "WEIGHT_GROUPS",
    "GRADE_BANDS", "GRADE_LABELS", "VERDICT_LABELS", "VERDICT_BUCKETS",
    "SCAN_NOTE", "RISK_NOTE", "DISCLAIMER",
    "hard_filters", "score_candidate", "risk_score", "scan_candidates",
    "composite_score", "grade_of", "verdict_bucket",
]

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
#: 五个评分因子（顺序固定，便于前端与测试断言）
FACTORS = ("momentum", "volume", "trend", "position", "risk")
#: 因子中文名（reasons / warnings / 前端 chip 共用）
FACTOR_CN = {
    "momentum": "动量/相对强弱（反转取向）", "volume": "量能", "trend": "趋势",
    "position": "位置", "risk": "风险（反向：越高越安全）",
}
#: 四组复合权重（调研结论第 6 条：技术 0.40 / 量能 0.25 / 风险(反向) 0.25 / 强度·相对强弱 0.10）
WEIGHT_GROUPS = {"technical": 0.40, "volume": 0.25, "risk": 0.25, "strength": 0.10}
#: 组 → 因子（`technical` 组内含「趋势 + 位置」，组内等权拆分）
GROUP_FACTORS = {
    "technical": ("trend", "position"), "volume": ("volume",),
    "risk": ("risk",), "strength": ("momentum",),
}
#: 因子级默认权重（= 组权重按因子数摊平后归一，和恒为 1）
FACTOR_DEFAULT_WEIGHTS = {"momentum": 0.10, "volume": 0.25, "trend": 0.20,
                          "position": 0.20, "risk": 0.25}
#: 默认权重的中文说明（与 `_resolve_weights(None)` 的文案保持一致）
DEFAULT_WEIGHT_NOTE = ("默认权重：技术（趋势+位置）0.40 / 量能 0.25 / 风险（反向）0.25 "
                       "/ 强度 0.10")
#: 等级阈值（降序，用 >= 判定）
GRADE_BANDS = (("A", 80), ("B", 65), ("C", 50), ("D", 35), ("F", 0))
GRADE_LABELS = {"A": "优秀（80–100）", "B": "良好（65–79）", "C": "中性（50–64）",
                "D": "偏弱（35–49）", "F": "差（0–34）"}
#: 五档中文结论（调研结论第 6 条）
VERDICT_LABELS = {"A": "值得买入", "B": "可关注", "C": "中性", "D": "谨慎", "F": "回避"}
#: 三档口径（A → 值得买入；B/C → 观察；D/F → 回避），供只展示三档的接口聚合
VERDICT_BUCKETS = {"A": "值得买入", "B": "观察", "C": "观察", "D": "回避", "F": "回避"}
#: 各市场默认口径（A 股 2 亿元成交额 / 7% 涨幅上限；美股按美元与更宽的涨幅上限）
DEFAULT_MARKET_PARAMS = {
    "cn": {"min_amount": 2e8, "max_change_pct": 7.0},
    "us": {"min_amount": 3e7, "max_change_pct": 10.0},
}

#: 默认参数（对外公开的 `DEFAULT_SCAN_PARAMS`，键名同时接受 camelCase 别名）
DEFAULT_SCAN_PARAMS = {
    # ① 流动性下限（元 / 美元，按市场）
    "min_amount": {k: v["min_amount"] for k, v in DEFAULT_MARKET_PARAMS.items()},
    # ② 反追高：当日涨幅上限（%）
    "max_change_pct": {k: v["max_change_pct"] for k, v in DEFAULT_MARKET_PARAMS.items()},
    # ③ 量能：量比下限，或 5/20 日均额比下限（满足其一）
    "min_volume_ratio": 1.2,
    "min_amount_ratio": 1.3,
    # ③ 反「已经涨上天」
    "pct60d_warn": 80.0,          # 近 60 日累计涨幅 > 该值 → 扣分
    "pct60d_reject": 150.0,       # ≥ 该值 → 风险分打满并整只剔除
    "max_drop_1d": 7.0,           # 近 60 日单日跌幅 > 该值 → 回撤风险
    "max_drop_2d": 10.0,          # 近 60 日两日累计跌幅 > 该值 → 回撤风险
    "turnover_high": 15.0,        # 换手率高于该值 → 量能因子扣分（低换手更优）
    "turnover_low": 3.0,          # 换手率低于该值 → 量能因子加满
    # 风险分阈值
    "avoid_risk_score": 5,        # ≥ 5 → HIGH / AVOID
    "reject_risk_score": 7,       # ≥ 7 → CRITICAL / AVOID，并在扫描阶段整只剔除
    # 数据完备度
    "min_bars": 60,               # 低于该根数记降级 warning（仍尽力评分）
    "reject_bars": 20,            # 低于该根数直接剔除（算不出 MA20 / 阻力位）
    # 指标窗口与区间
    "rsi_n": 14,
    "rsi_best_low": 50.0,         # RSI 温和走强区间下沿（满分）
    "rsi_best_high": 68.0,        # RSI 温和走强区间上沿（满分）
    "rsi_hot": 75.0,              # RSI 高于该值 → 过热扣分
    "dist_near": 3.0,             # 距最近阻力位 < 该值（%）→ 空间不足扣分
    "dist_far": 25.0,             # 距最近阻力位 ≤ 该值（%）→ 满分
    "spark_len": 30,              # 迷你走势图取最近多少根收盘价
    # 复合权重（因子级；也接受 technical/volume/risk/strength 组级写法）
    "weights": dict(FACTOR_DEFAULT_WEIGHTS),
}

#: 口径说明（挂到 `scan_candidates()["note"]`）
SCAN_NOTE = (
    "扫描口径：先过三道硬闸门（① 成交额 ≥ 流动性下限；② 当日涨幅 ≤ 上限，反追高；"
    "③ 量比 ≥ 下限或 5/20 日均额比 ≥ 下限，后者更抗单日异动），未通过者进 rejected、"
    "不参与打分；幸存者按「技术（趋势+位置）0.40 + 量能 0.25 + 风险(反向) 0.25 + "
    "趋势强度/相对强弱 0.10」合成 0–100 分，映射 A(80–100)/B(65–79)/C(50–64)/"
    "D(35–49)/F(0–34)，verdict 为一句话中文结论。动量因子按 A 股短期反转取向给分"
    "（RSI 50–68 且涨幅温和才满分，RSI > 75 或近 5 日涨幅 > 12% 反而扣分），"
    "低换手优于高换手；单个技术指标（RSI/ADX 等）只作为过滤器或加权项之一，"
    "本结果不是单指标信号。数据缺失一律跳过对应闸门并计入 stats.missingFields，"
    "缺 K 线的标的直接剔除、不臆造得分。排序：score 降序，同分按 amount 降序。"
    "本结果由公开行情数据按固定规则自动计算，仅供技术研究，不构成任何投资建议。"
)
#: 风险分口径说明
RISK_NOTE = (
    "风险分口径（0–10，参照 Penny 的极端放量 + 极端涨幅反拉抬合成思路）："
    "极端放量（量比 ≥ 2 / ≥ 3 / ≥ 5 分别记 1 / 2 / 3）、近 60 日累计涨幅"
    "（≥ 40 / ≥ 60 / ≥ 80 分别记 1 / 2 / 3，≥ 150 记 4 并标记剔除）、"
    "近 60 日大幅回撤（单日跌幅 > 7% 或两日累计跌幅 > 10%，取二者较大值记 2，不叠加）、"
    "MA20 乖离过大（≥ 15 / ≥ 25 记 1 / 2）、高波动（ATR% ≥ 8 记 1），"
    "合计截断到 10；0–2 = LOW / OK，3–4 = MEDIUM / CAUTION，5–6 = HIGH / AVOID，"
    "≥ 7 = CRITICAL / AVOID（扫描阶段整只剔除）；命中「近 60 日涨幅 ≥ 150%」这一"
    "单项时同样直接记 CRITICAL 并整只剔除，不因总分不足 7 分而被稀释。"
    "风险分只刻画「过热 / 拉抬 / 回撤」"
    "这类尾部风险，不衡量基本面与流动性风险，也不预测下跌概率。"
)
#: 免责声明（与 core/advisor.py 的 DISCLAIMER 同口径）
DISCLAIMER = (
    "本结果由公开行情数据经固定规则自动计算得出，仅用于技术研究与学习，"
    "不构成任何投资建议；规则存在失效风险，历史统计不代表未来表现，"
    "据此操作的盈亏由投资者自行承担。"
)

#: 参数别名（snake_case ↔ camelCase），便于服务端直接透传前端参数
_EXTRA_ALIASES = {
    "min_amount": ("minAmount", "minAmountByMarket", "min_amount_by_market"),
    "max_change_pct": ("maxChangePct", "maxChgPct", "maxPct"),
    "min_volume_ratio": ("minVolumeRatio", "vrMin", "vr_min"),
    "min_amount_ratio": ("minAmountRatio", "amountRatioMin", "amtRatioMin"),
    "pct60d_warn": ("pct60dWarn", "maxPct60d"),
    "pct60d_reject": ("pct60dReject", "rejectPct60d"),
    "max_drop_1d": ("maxDrop1d", "maxDrop1D"),
    "max_drop_2d": ("maxDrop2d", "maxDrop2D"),
    "turnover_high": ("turnoverHigh", "maxTurnover"),
    "turnover_low": ("turnoverLow", "minTurnover"),
    "avoid_risk_score": ("avoidRiskScore",),
    "reject_risk_score": ("rejectRiskScore", "maxRiskScore"),
    "min_bars": ("minBars",),
    "reject_bars": ("rejectBars",),
    "rsi_n": ("rsiN",),
    "rsi_best_low": ("rsiBestLow",),
    "rsi_best_high": ("rsiBestHigh",),
    "rsi_hot": ("rsiHot",),
    "dist_near": ("distNear", "resistanceNear"),
    "dist_far": ("distFar", "resistanceFar"),
    "spark_len": ("sparkLen", "sparkLength"),
    "weights": ("weight", "weightGroups"),
}
_EPS = 1e-12


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _num(x):
    """宽松转 float；None / bool / NaN / ±inf / 无法解析的字符串一律返回 None。"""
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        v = float(x)
    else:
        try:
            v = float(str(x).strip())
        except (TypeError, ValueError):
            return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def _r(x, nd=4):
    """四舍五入；无效值返回 None（出口统一清洗，绝不让 inf / NaN 进 JSON）。"""
    v = _num(x)
    return None if v is None else round(v, nd)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _camel(key):
    parts = str(key).split("_")
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])


def _get(src, key):
    """按 snake_case / camelCase / 显式别名从参数字典取值。"""
    if not isinstance(src, dict):
        return None
    if key in src:
        return src[key]
    for a in (_camel(key),) + tuple(_EXTRA_ALIASES.get(key, ())):
        if a in src:
            return src[a]
    return None


def _by_market(value, default):
    """市场化参数：支持标量（各市场同值）或 ``{"cn": .., "us": ..}``。"""
    out = {k: float(v) for k, v in dict(default).items()}
    n = _num(value)
    if n is not None:
        return {k: n for k in out}
    if isinstance(value, dict):
        for k in out:
            x = _num(value.get(k))
            if x is not None:
                out[k] = x
    return out


def _market_of(row):
    """市场判定：显式 ``market`` 优先 → 6 位数字代码视为 A 股 → 纯字母代码视为美股。

    默认回落到 A 股口径（更严的流动性下限与涨幅上限），避免因为认不出市场而放宽闸门。
    """
    src = row if isinstance(row, dict) else {}
    m = str(src.get("market") or "").strip().lower()
    if m.startswith("us") or m in ("nasdaq", "nyse", "amex"):
        return "us"
    if m.startswith("cn") or m in ("sh", "sz", "bj", "sse", "szse"):
        return "cn"
    code = str(src.get("code") or src.get("symbol") or "").strip()
    if len(code) == 6 and code.isdigit():
        return "cn"
    letters = code.replace(".", "").replace("-", "")
    if letters and letters.isalpha() and len(letters) <= 5:
        return "us"
    return "cn"


def _money(v, market="cn"):
    """金额中文格式化（亿元 / 万元 / 元；美元市场用同刻度换单位词）。"""
    unit = "美元" if market == "us" else "元"
    x = _num(v)
    if x is None:
        return "—"
    if abs(x) >= 1e8:
        return "%.2f 亿%s" % (x / 1e8, unit)
    if abs(x) >= 1e4:
        return "%.2f 万%s" % (x / 1e4, unit)
    return "%.2f %s" % (x, unit)


def _fmt(v, nd=2, suffix=""):
    """数值格式化（None → 「—」），用于中文原因与结论文案。"""
    x = _num(v)
    return "—" if x is None else ("%." + str(int(nd)) + "f%s") % (x, suffix)


def _clean_bars(bars):
    """清洗 K 线：只保留收盘价有效且为正的记录。

    · **不重排顺序**（与 `core/advisor.py` / `core/forecast.py` 一致）：上游保证
      「旧 → 新」，按时间字段重排会在时间格式不规范时把序列打乱、制造假跳空；
    · `high` / `low` / `open` 缺失时用收盘价补齐，并保证 `low ≤ close ≤ high`；
    · 纯数字序列（如 ``[10.0, 10.2, ...]``）也接受，按收盘价处理；
    · 脏数据（非 dict / 缺 close / 价格为负 / NaN）整根剔除；
    · ``bars`` 不是列表（None / 数字 / 字符串 / dict）时按空序列处理，不抛异常。
    """
    if not isinstance(bars, (list, tuple)):
        return []
    out = []
    for b in bars or []:
        if isinstance(b, dict):
            c = _num(b.get("close", b.get("price", b.get("c"))))
            if c is None or c <= 0:
                continue
            h = _num(b.get("high"))
            l = _num(b.get("low"))
            o = _num(b.get("open"))
            h = c if (h is None or h <= 0) else h
            l = c if (l is None or l <= 0) else l
            o = c if (o is None or o <= 0) else o
            v = _num(b.get("volume", b.get("vol")))
            a = _num(b.get("amount"))
            out.append({"open": o, "high": max(h, c), "low": min(l, c), "close": c,
                        "volume": None if v is None else max(0.0, v),
                        "amount": None if (a is None or a < 0) else a})
            continue
        c = _num(b)
        if c is None or c <= 0:
            continue
        out.append({"open": c, "high": c, "low": c, "close": c,
                    "volume": None, "amount": None})
    return out


def _mean(seq):
    vals = [v for v in seq if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _vol_ratio(vols, n=5):
    """当日量 / 近 n 日均量（不含当日）；量缺失或前 n 日不全 → None。"""
    if len(vols) < n + 1:
        return None
    prev = vols[-n - 1:-1]
    m = _mean(prev)
    last = vols[-1]
    if last is None or m is None or m <= 0:
        return None
    return last / m


def _amount_ratio(amts, short=5, long=20):
    """近 short 日均额 / 近 long 日均额（抗单日异动的资金结构口径）。"""
    if len(amts) < long:
        return None
    a, b = _mean(amts[-short:]), _mean(amts[-long:])
    if a is None or b is None or b <= 0:
        return None
    return a / b


def _min_ret(closes, lag, window):
    """最近 window 根内的最小 lag 日累计收益（%，负值），数据不足返回 None。"""
    n = len(closes)
    if n < lag + 1:
        return None
    start = max(lag, n - window)
    vals = []
    for i in range(start, n):
        base = closes[i - lag]
        if base and base > 0:
            vals.append((closes[i] / base - 1) * 100)
    return min(vals) if vals else None


def _context(row, bars, params):
    """把「快照行 + K 线」拼成一次评分所需的全部中间量（唯一计算入口）。"""
    src = row if isinstance(row, dict) else {}
    b = bars or []
    closes = [x["close"] for x in b]
    n = len(closes)
    ctx = {"bars": n, "close": closes[-1] if n else None,
           "spark": [_r(c, 4) for c in closes[-int(params["spark_len"]):]] if n else []}
    if n:
        ma5 = I.SMA(closes, 5)
        ma20 = I.SMA(closes, 20)
        ma60 = I.SMA(closes, 60)
        ctx["ma5"] = ma5[-1] if n >= 5 else None
        ctx["ma20"] = ma20[-1] if n >= 20 else None
        ctx["ma60"] = ma60[-1] if n >= 60 else None
        rsi_n = int(params["rsi_n"])
        ctx["rsi"] = I.RSI(closes, rsi_n)[-1] if n > rsi_n else None
        if n >= 21 and ma20[-1] and ma20[-11] and ma20[-11] > 0:
            ctx["ma20Slope"] = (ma20[-1] / ma20[-11] - 1) * 100
        else:
            ctx["ma20Slope"] = None
        ctx["ma20Dev"] = ((closes[-1] / ctx["ma20"] - 1) * 100
                          if ctx["ma20"] else None)
        atr = I.ATR(b, 14)
        av = None
        for x in reversed(atr):
            if I.ok(x):
                av = x
                break
        ctx["atrPct"] = (av / closes[-1] * 100) if (av and closes[-1]) else None
        vols = [x.get("volume") for x in b]
        amts = [x.get("amount") for x in b]
        ctx["volumeRatioBars"] = _vol_ratio(vols, 5)
        ctx["amountRatioBars"] = _amount_ratio(amts, 5, 20)
        ctx["ret5"] = (closes[-1] / closes[-6] - 1) * 100 if n >= 6 else None
        ctx["ret20"] = (closes[-1] / closes[-21] - 1) * 100 if n >= 21 else None
        ctx["pct60dBars"] = (closes[-1] / closes[-61] - 1) * 100 if n >= 61 else None
        ctx["maxDrop1d"] = _min_ret(closes, 1, 60)
        ctx["maxDrop2d"] = _min_ret(closes, 2, 60)
        # 最近阻力位：最近 60 根（不含今天）中「高于现价的最小 high」
        win = b[max(0, n - 61):n - 1]
        above = [x["high"] for x in win if x["high"] > closes[-1] * (1 + 1e-9)]
        if above:
            ctx["resistance"] = min(above)
            ctx["distToResistance"] = (ctx["resistance"] / closes[-1] - 1) * 100
            ctx["atHigh"] = False
        elif win:
            ctx["resistance"] = None      # 上方无阻力（已突破 / 创新高）
            ctx["distToResistance"] = 0.0
            ctx["atHigh"] = True
        else:
            ctx["resistance"] = None
            ctx["distToResistance"] = None
            ctx["atHigh"] = False
    else:
        for k in ("ma5", "ma20", "ma60", "rsi", "ma20Slope", "ma20Dev", "atrPct",
                  "volumeRatioBars", "amountRatioBars", "ret5", "ret20",
                  "pct60dBars", "maxDrop1d", "maxDrop2d", "resistance",
                  "distToResistance"):
            ctx[k] = None
        ctx["atHigh"] = False

    # 供应商快照字段优先（量比是盘中时间加权口径，比日线推算更贴近实时）
    row_rv = _num(src.get("volumeRatio", src.get("vr")))
    if row_rv is not None:
        ctx["volumeRatio"] = row_rv
        # 上游（scanner 主流程）把「由 K 线推算的量比」填进行里时会带来源标记，
        # 避免把推算值标成快照口径（provenance 必须准确）
        ctx["volumeRatioSource"] = str(src.get("volumeRatioSource") or "row")
    elif ctx.get("volumeRatioBars") is not None:
        ctx["volumeRatio"], ctx["volumeRatioSource"] = ctx["volumeRatioBars"], "bars"
    else:
        ctx["volumeRatio"], ctx["volumeRatioSource"] = None, None
    row_ar = _num(src.get("amountRatio", src.get("amountRatio5_20")))
    ctx["amountRatio"] = ctx.get("amountRatioBars") if ctx.get("amountRatioBars") is not None else row_ar
    ctx["amountRatioSource"] = ("bars" if ctx.get("amountRatioBars") is not None
                                else ("row" if row_ar is not None else None))
    ctx["turnover"] = _num(src.get("turnover", src.get("turnoverRate")))
    ctx["amount"] = _num(src.get("amount"))
    if ctx["amount"] is None and n:
        ctx["amount"] = b[-1].get("amount")
    ctx["changePct"] = _num(src.get("changePct", src.get("pct")))
    ctx["changePctSource"] = "row"
    if ctx["changePct"] is None and n >= 2 and closes[-2] > 0:
        ctx["changePct"] = (closes[-1] / closes[-2] - 1) * 100
        ctx["changePctSource"] = "bars"
    row_p60 = _num(src.get("chg60d", src.get("pct60d")))
    if ctx.get("pct60dBars") is not None:
        ctx["pct60d"], ctx["pct60dSource"] = ctx["pct60dBars"], "bars"
    elif row_p60 is not None:
        ctx["pct60d"], ctx["pct60dSource"] = row_p60, "row"
    else:
        ctx["pct60d"], ctx["pct60dSource"] = None, None
    ctx["market"] = _market_of(src)
    return ctx


def _merge_row(row, extras):
    """行 + 上游预计算补充字段（extras 只补空缺，不覆盖快照原值）。"""
    src = dict(row) if isinstance(row, dict) else {}
    if isinstance(extras, dict):
        for k, v in extras.items():
            if v is not None and src.get(k) is None:
                src[k] = v
    return src


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
def _resolve_weights(value):
    """解析复合权重：支持组级（technical/volume/risk/strength）与因子级两种写法。

    显式给出的键按「组权重在组内等分」折算为因子权重，未给出的因子权重记 0；
    结果合计 ≤ 0（或键名全部无法识别）时回退默认权重，保证评分不会全零。
    返回 ``(因子权重字典, 中文说明)``，权重和恒为 1。
    """
    dflt = dict(FACTOR_DEFAULT_WEIGHTS)
    if not isinstance(value, dict) or not value:
        return dflt, DEFAULT_WEIGHT_NOTE
    given = {}
    unknown = []
    for k, v in value.items():
        key = str(k)
        if key in WEIGHT_GROUPS:
            members = GROUP_FACTORS[key]
            w = _num(v)
            w = 0.0 if w is None else max(0.0, w)
            for f in members:
                given[f] = w / len(members)
        elif key in FACTOR_DEFAULT_WEIGHTS:
            w = _num(v)
            given[key] = 0.0 if w is None else max(0.0, w)
        else:
            unknown.append(key)
    if not given:
        return dflt, "权重键名无法识别（%s），已回退默认权重" % (",".join(sorted(unknown)) or "空")
    out = {f: given.get(f, 0.0) for f in FACTORS}
    total = sum(out.values())
    if total <= _EPS:
        return dflt, "自定义权重合计为 0，已回退默认权重"
    out = {f: out[f] / total for f in FACTORS}
    text = "自定义权重（已归一化）：" + " / ".join(
        "%s %.2f" % (FACTOR_CN[f].split("（")[0], out[f]) for f in FACTORS)
    if unknown:
        text += "；忽略无法识别的键 %s" % (",".join(sorted(unknown)),)
    return out, text


def _norm_params(params):
    """参数归一化：非法值回退默认、比例/数量类夹取、权重解析，返回生效参数副本。"""
    src = params if isinstance(params, dict) else {}
    p = {}
    for key, dflt in DEFAULT_SCAN_PARAMS.items():
        if key == "weights":
            continue
        v = _get(src, key)
        if key in ("min_amount", "max_change_pct"):
            p[key] = _by_market(v, dflt)
            continue
        n = _num(v)
        p[key] = float(dflt) if n is None else n
    for key, lo in (("min_bars", 20), ("reject_bars", 2), ("rsi_n", 2),
                    ("spark_len", 1), ("avoid_risk_score", 0), ("reject_risk_score", 0)):
        p[key] = int(max(lo, p[key]))
    if p["reject_bars"] > p["min_bars"]:
        p["reject_bars"] = p["min_bars"]
    p["pct60d_reject"] = max(p["pct60d_warn"], p["pct60d_reject"])
    if p["rsi_best_low"] > p["rsi_best_high"]:
        p["rsi_best_low"], p["rsi_best_high"] = p["rsi_best_high"], p["rsi_best_low"]
    p["dist_far"] = max(p["dist_near"], p["dist_far"])
    p["reject_risk_score"] = max(p["reject_risk_score"], p["avoid_risk_score"])
    # 权重：显式传入的自定义权重优先；若上游把「已归一化的生效参数」再传回来
    # （scan_candidates 逐只调用时会发生），沿用其中的 weightNote，保证文案稳定。
    wv = _get(src, "weights")
    if wv is None:
        p["weights"] = dict(FACTOR_DEFAULT_WEIGHTS)
        p["weightNote"] = DEFAULT_WEIGHT_NOTE
    else:
        weights, auto_note = _resolve_weights(wv)
        p["weights"] = weights
        p["weightNote"] = str(src.get("weightNote") or auto_note)
    return p


# --------------------------------------------------------------------------- #
# 一、三道硬闸门（不含技术面）
# --------------------------------------------------------------------------- #
def hard_filters(row, params=None):
    """三道硬闸门：① 流动性下限 ② 反追高 ③ 量比/资金门槛。

    **不含任何技术面判断**（形态、均线、指标一律不参与），因此它只回答
    「这只标的值不值得进打分环节」，不回答「好不好」。

    参数
    ----
    row : dict
        全市场快照行（字段可缺）：``code`` / ``name`` / ``market`` / ``price`` /
        ``changePct`` / ``amount`` / ``volumeRatio`` / ``amountRatio``（5/20 日均额比）。
    params : dict | None
        见 :data:`DEFAULT_SCAN_PARAMS`（同时接受 camelCase 别名）。

    返回
    ----
    dict
        ``pass`` 是否放行；``reasons`` 中文拦截原因（每条都带具体数值）；
        ``tags`` 归因短标签（用于 ``stats.byRejectReason``）；``metrics`` 参与判断的
        指标原值；``warnings`` 因字段缺失而跳过闸门的说明；
        ``missingGates`` 被跳过的闸门数；``missingFieldCounts`` {字段: 次数}。

    规则
    ----
    ① ``amount ≥ min_amount[market]``（A 股默认 2 亿元，美股默认 0.3 亿美元）；
    ② ``changePct ≤ max_change_pct[market]``（A 股默认 7%，美股 10%）——避免当天
       已经冲高的标的被追上；``changePct`` 缺失时**不拦截**，只记 warning（快照源
       偶发缺字段，缺字段不等于不合格，但必须如实告知）；
    ③ ``volumeRatio ≥ min_volume_ratio`` **或** ``amountRatio ≥ min_amount_ratio``
       ——二者满足其一即放行；后者（5 日均额 / 20 日均额）不会被单日异常成交额带偏。

    边界
    ----
    行非法（None / 非 dict）按空行处理；任何数值缺失只跳过对应闸门，不抛异常。
    """
    p = _norm_params(params)
    src = row if isinstance(row, dict) else {}
    market = _market_of(src)
    min_amt = p["min_amount"][market]
    max_chg = p["max_change_pct"][market]

    reasons, tags, warnings, metrics = [], [], [], {}
    missing, missing_counts = 0, {}

    amount = _num(src.get("amount", src.get("成交额")))
    if amount is None:
        missing += 1
        missing_counts["amount"] = missing_counts.get("amount", 0) + 1
        warnings.append("成交额（amount）缺失，流动性下限闸门①已跳过（不拦截，请人工确认）")
    else:
        metrics["amount"] = _r(amount, 2)
        if amount < min_amt:
            reasons.append("成交额 %s 低于流动性下限 %s，流动性不足（硬闸门①）" % (
                _money(amount, market), _money(min_amt, market)))
            tags.append("流动性不足")

    change = _num(src.get("changePct", src.get("pct")))
    if change is None:
        missing += 1
        missing_counts["changePct"] = missing_counts.get("changePct", 0) + 1
        warnings.append("当日涨幅（changePct）缺失，反追高闸门②已跳过（不拦截，避免误杀）")
    else:
        metrics["changePct"] = _r(change, 3)
        if change > max_chg:
            reasons.append("当日涨幅 %.2f%% 超过上限 %.2f%%，当天已冲高（反追高，硬闸门②）" % (
                change, max_chg))
            tags.append("反追高")

    rv = _num(src.get("volumeRatio", src.get("vr")))
    ar = _num(src.get("amountRatio", src.get("amountRatio5_20")))
    if rv is None and ar is None:
        missing += 1
        for f in ("volumeRatio", "amountRatio"):
            missing_counts[f] = missing_counts.get(f, 0) + 1
        warnings.append("量比（volumeRatio）与 5/20 日均额比（amountRatio）均缺失，"
                        "量能闸门③已跳过（不拦截）")
    else:
        metrics["volumeRatio"] = _r(rv, 3)
        metrics["amountRatio"] = _r(ar, 3)
        ok_rv = rv is not None and rv >= p["min_volume_ratio"]
        ok_ar = ar is not None and ar >= p["min_amount_ratio"]
        if not (ok_rv or ok_ar):
            reasons.append(
                "量能不足：量比 %s 低于 %.2f 且 5/20 日均额比 %s 低于 %.2f，"
                "当日无资金承接迹象（硬闸门③）" % (
                    _fmt(rv), p["min_volume_ratio"], _fmt(ar), p["min_amount_ratio"]))
            tags.append("量能不足")

    return {
        "pass": not reasons,
        "reasons": reasons,
        "tags": tags,
        "metrics": metrics,
        "warnings": warnings,
        "missingGates": missing,
        "missingFieldCounts": missing_counts,
    }


# --------------------------------------------------------------------------- #
# 二、风险分（0–10，Penny 反拉抬口径）
# --------------------------------------------------------------------------- #
def risk_score(row, bars, params=None):
    """0–10 风险分 + 等级 + 处置建议（「已经涨上天」的量化表达）。

    参数
    ----
    row : dict
        快照行（``volumeRatio`` / ``chg60d`` 等字段可缺，缺失的项自动降级、不臆造）。
    bars : list[dict]
        K 线（旧 → 新），用于回撤 / 乖离 / 波动 / 60 日涨幅。
    params : dict | None
        见 :data:`DEFAULT_SCAN_PARAMS`。

    返回
    ----
    dict
        ``score`` 0–10（截断后）；``rawScore`` 截断前合计（可能 > 10，便于审计）；
        ``level`` LOW / MEDIUM / HIGH / CRITICAL；``action`` OK / CAUTION / AVOID；
        ``reject`` 是否命中「整只剔除」项（近 60 日涨幅 ≥ 150%）；``evidence`` 实际
        算出的风险证据键（为空说明快照与 K 线都没给出可用字段，此时不应据此判「安全」）；
        ``flags`` 逐项明细 ``{key, label, value, penalty, reject}``；``summary``
        一句话中文；``metrics`` 参与计算的指标；``note`` 口径说明。

    分级与阈值（见 :data:`RISK_NOTE`）
    ---------------------------------
    0–2 = LOW / OK；3–4 = MEDIUM / CAUTION；5–6 = HIGH / AVOID（调研结论：5–6 分
    即 AVOID）；≥ 7 = CRITICAL / AVOID。此外：只要命中 ``reject=True`` 的单项
    （近 60 日涨幅 ≥ ``pct60d_reject``）或总分 ≥ ``reject_risk_score``，等级一律记
    CRITICAL / AVOID，扫描阶段整只剔除 —— 单个极端项不因「总分不够」被稀释掉。
    回撤项取「单日跌幅」与「两日累计跌幅」中较大的一次性扣分，避免同一段下跌被
    重复计两次。
    """
    p = _norm_params(params)
    src = row if isinstance(row, dict) else {}
    ctx = _context(src, _clean_bars(bars), p)

    flags = []
    raw = 0

    def add(key, label, value, penalty, reject=False):
        nonlocal raw
        raw += penalty
        flags.append({"key": key, "label": label, "value": _r(value, 4),
                      "penalty": int(penalty), "reject": bool(reject)})

    rv = ctx["volumeRatio"]
    if rv is not None:
        if rv >= 5.0:
            add("volumeRatio", "极端放量：量比 %.2f ≥ 5.0，成交异常放大（拉抬风险）" % (rv,), rv, 3)
        elif rv >= 3.0:
            add("volumeRatio", "显著放量：量比 %.2f ≥ 3.0" % (rv,), rv, 2)
        elif rv >= 2.0:
            add("volumeRatio", "放量：量比 %.2f ≥ 2.0" % (rv,), rv, 1)

    pct60 = ctx["pct60d"]
    if pct60 is not None:
        if pct60 >= p["pct60d_reject"]:
            add("pct60d", "近 60 日累计涨幅 %.1f%% ≥ %.0f%%，已属「已经涨上天」"
                "（反拉抬，整只剔除）" % (pct60, p["pct60d_reject"]), pct60, 4, reject=True)
        elif pct60 >= p["pct60d_warn"]:
            add("pct60d", "近 60 日累计涨幅 %.1f%% ≥ %.0f%%，位置偏高（扣分）" % (
                pct60, p["pct60d_warn"]), pct60, 3)
        elif pct60 >= 60.0:
            add("pct60d", "近 60 日累计涨幅 %.1f%% 偏快（≥ 60%%）" % (pct60,), pct60, 2)
        elif pct60 >= 40.0:
            add("pct60d", "近 60 日累计涨幅 %.1f%% 略快（≥ 40%%）" % (pct60,), pct60, 1)

    d1, d2 = ctx["maxDrop1d"], ctx["maxDrop2d"]
    hard1 = d1 is not None and d1 <= -p["max_drop_1d"]
    hard2 = d2 is not None and d2 <= -p["max_drop_2d"]
    if hard1 or hard2:
        if hard1 and hard2:
            worst, label = (d1, d2) if d1 <= d2 else (d2, d1)
            text = "近 60 日出现单日跌幅 %.2f%%（> %.1f%%）与两日累计跌幅 %.2f%%（> %.1f%%）" % (
                d1, p["max_drop_1d"], d2, p["max_drop_2d"])
        elif hard1:
            worst, label = d1, None
            text = "近 60 日出现单日跌幅 %.2f%%（> %.1f%%）" % (d1, p["max_drop_1d"])
        else:
            worst, label = d2, None
            text = "近 60 日出现两日累计跌幅 %.2f%%（> %.1f%%）" % (d2, p["max_drop_2d"])
        add("drawdown", text + "，触发反「大幅回撤」项（一次性记 2 分，不叠加）", worst, 2)

    dev = ctx["ma20Dev"]
    if dev is not None:
        if dev >= 25.0:
            add("ma20Dev", "MA20 乖离 +%.1f%% 过大（≥ 25%%），短期严重偏离均线" % (dev,), dev, 2)
        elif dev >= 15.0:
            add("ma20Dev", "MA20 乖离 +%.1f%% 偏大（≥ 15%%）" % (dev,), dev, 1)

    atr = ctx["atrPct"]
    if atr is not None and atr >= 8.0:
        add("atrPct", "波动过高：ATR %.1f%% ≥ 8%%，日内振幅风险大" % (atr,), atr, 1)

    score = min(10, raw)
    reject = any(f["reject"] for f in flags)
    if reject or score >= p["reject_risk_score"]:
        level, action = "CRITICAL", "AVOID"
    elif score >= p["avoid_risk_score"]:
        level, action = "HIGH", "AVOID"
    elif score >= 3:
        level, action = "MEDIUM", "CAUTION"
    else:
        level, action = "LOW", "OK"

    if flags:
        head = "；".join(f["label"] for f in flags[:3])
    else:
        head = "未见极端放量 / 过热涨幅 / 大幅回撤 / 乖离过大等异常"
    summary = "风险分 %d/10（%s）：%s" % (score, level, head)

    evidence = [k for k, v in (
        ("volumeRatio", ctx["volumeRatio"]), ("pct60d", ctx["pct60d"]),
        ("maxDrop1d", ctx["maxDrop1d"]), ("ma20Dev", ctx["ma20Dev"]),
        ("atrPct", ctx["atrPct"])) if v is not None]

    return {
        "score": int(score),
        "rawScore": int(raw),
        "level": level,
        "action": action,
        "reject": bool(reject),
        "evidence": evidence,
        "flags": flags,
        "summary": summary,
        "metrics": {
            "volumeRatio": _r(ctx["volumeRatio"], 3),
            "pct60d": _r(ctx["pct60d"], 2),
            "pct60dSource": ctx["pct60dSource"],
            "maxDrop1d": _r(d1, 2),
            "maxDrop2d": _r(d2, 2),
            "ma20Dev": _r(dev, 2),
            "atrPct": _r(atr, 2),
            "bars": ctx["bars"],
        },
        "note": RISK_NOTE,
    }


# --------------------------------------------------------------------------- #
# 三、评分因子（每个 0–100）
# --------------------------------------------------------------------------- #
def _sub_score(comps):
    """子项归一：`comps = [(得分, 满分, 说明), ...]`，缺失子项不进分子也不进分母。

    这样「缺字段」只会让该因子按剩余子项重新归一，而不是给它一个猜测值。
    """
    usable = [(s, m, t) for s, m, t in comps if s is not None and m]
    if not usable:
        return None, []
    num = sum(s for s, _, _ in usable)
    den = sum(m for _, m, _ in usable)
    return 100.0 * num / den, [t for _, _, t in usable]


def _trend_factor(ctx, p):
    """趋势因子：价 vs MA20、均线排列、MA20 斜率（三个子项，缺失项自动剔除）。"""
    comps = []
    ma5, ma20, ma60 = ctx["ma5"], ctx["ma20"], ctx["ma60"]
    close = ctx["close"]
    if close is not None and ma20:
        dev = close / ma20 - 1
        pts = 40 if dev >= 0 else (20 if dev >= -0.02 else 0)
        comps.append((pts, 40, "价在 MA20 %s" % ("上方" if dev >= 0 else "下方")))
    if ma5 and ma20 and ma60:
        pts = 35 if (ma5 > ma20 > ma60) else (20 if ma5 > ma20 else 0)
        comps.append((pts, 35, "均线排列 %s" % (
            "多头（MA5>MA20>MA60）" if pts == 35 else ("短多（MA5>MA20）" if pts == 20 else "偏空"))))
    slope = ctx["ma20Slope"]
    if slope is not None:
        pts = 25 if slope > 1.0 else (12 if slope >= 0 else 0)
        comps.append((pts, 25, "MA20 斜率 %s" % ("向上" if slope > 1.0 else (
            "走平" if slope >= 0 else "向下"))))
    return _sub_score(comps)


def _position_factor(ctx, p):
    """位置因子：「距最近阻力位的空间」。

    调研结论第 7 条的落实：太近（< dist_near，含贴着阻力位）= 空间不足 → 扣分；
    适度距离（dist_near ~ dist_far）= 既不追高又有空间 → 满分；过远 = 趋势弱 → 中等；
    已突破（上方无阻力）= 中性偏正 60 分（不再被压制，但也可能是追高位）。
    """
    dist = ctx["distToResistance"]
    if dist is None:
        return None, []
    if ctx.get("atHigh"):
        return 60.0, ["已突破近 60 日高点（上方无阻力，中性偏正，需结合量能确认）"]
    if dist < 1.0:
        return 20.0, ["距最近阻力位仅 %.2f%%，几乎贴着压力位，空间不足（反追高）" % (dist,)]
    if dist < p["dist_near"]:
        return 45.0, ["距最近阻力位 %.2f%%（< %.1f%%），上方空间偏窄" % (dist, p["dist_near"])]
    if dist <= p["dist_far"]:
        return 100.0, ["距最近阻力位 %.2f%%，上方空间充足且未追高（3%%–%.0f%% 为最佳区间）" % (
            dist, p["dist_far"])]
    if dist <= 45.0:
        return 70.0, ["距最近阻力位 %.2f%% 偏远，趋势强度不足" % (dist,)]
    return 45.0, ["距最近阻力位 %.2f%% 过远，上方缺乏有效参照（多为弱势标的）" % (dist,)]


def _volume_factor(ctx, p):
    """量能因子：量比（适度放量最优）+ 5/20 日均额比 + 换手率（低换手更优）。"""
    comps = []
    rv = ctx["volumeRatio"]
    if rv is not None:
        if 1.2 <= rv <= 3.0:
            pts = 60
        elif rv > 5.0:
            pts = 15          # 极端放量：拉抬 / 异动风险，反而不加分
        elif rv > 3.0:
            pts = 45
        elif rv >= 1.0:
            pts = 40
        elif rv >= 0.7:
            pts = 25
        else:
            pts = 10          # 缩量：无资金关注
        comps.append((pts, 60, "量比 %.2f" % (rv,)))
    ar = ctx["amountRatio"]
    if ar is not None:
        pts = 25 if ar >= p["min_amount_ratio"] else (12 if ar >= 1.0 else 0)
        comps.append((pts, 25, "5/20 日均额比 %.2f" % (ar,)))
    to = ctx["turnover"]
    if to is not None:
        if to <= p["turnover_low"]:
            pts = 15
        elif to <= 8.0:
            pts = 12
        elif to <= p["turnover_high"]:
            pts = 6
        else:
            pts = 0           # 高换手组历史持续跑输（调研结论第 4 条）
        comps.append((pts, 15, "换手率 %.2f%%" % (to,)))
    return _sub_score(comps)


def _momentum_factor(ctx, p):
    """动量/相对强弱因子（**短期反转取向**，不奖励高涨幅）。

    调研结论第 4 条的落实：A 股在中短期（< 4 个月）偏反转效应，本工具持有 5–20 个
    交易日正落在该区间，因此这里奖励「温和走强 + 不过热」：
    · RSI 落在 [rsi_best_low, rsi_best_high]（默认 50–68）给满分子项，RSI > rsi_hot
      （默认 75）只给 10 分（过热 − 短期反转风险）；
    · 近 5 日涨幅 0%–5% 给满分，> 12% 归零（涨太快反而扣分）；
    · 近 20 日涨幅 0%–20% 给满分，> 20% 只给 5 分。
    """
    comps = []
    rsi = ctx["rsi"]
    if rsi is not None:
        lo, hi, hot = p["rsi_best_low"], p["rsi_best_high"], p["rsi_hot"]
        if lo <= rsi <= hi:
            pts = 60
        elif hi < rsi <= hot:
            pts = 35
        elif rsi > hot:
            pts = 10          # 过热：短期反转风险，扣分
        elif 45.0 <= rsi < lo:
            pts = 40
        elif 38.0 <= rsi < 45.0:
            pts = 25
        else:
            pts = 10
        comps.append((pts, 60, "RSI %.1f" % (rsi,)))
    ret5 = ctx["ret5"]
    if ret5 is not None:
        if 0.0 <= ret5 <= 5.0:
            pts = 25
        elif 5.0 < ret5 <= 12.0:
            pts = 15
        elif ret5 > 12.0:
            pts = 0           # 回归项：高涨幅不得分
        elif -3.0 <= ret5 < 0.0:
            pts = 12
        elif -8.0 <= ret5 < -3.0:
            pts = 5
        else:
            pts = 0
        comps.append((pts, 25, "近 5 日 %+.2f%%" % (ret5,)))
    ret20 = ctx["ret20"]
    if ret20 is not None:
        if 0.0 <= ret20 <= 20.0:
            pts = 15
        elif ret20 > 20.0:
            pts = 5
        elif -5.0 <= ret20 < 0.0:
            pts = 8
        else:
            pts = 3
        comps.append((pts, 15, "近 20 日 %+.2f%%" % (ret20,)))
    return _sub_score(comps)


# --------------------------------------------------------------------------- #
# 四、复合评分与等级
# --------------------------------------------------------------------------- #
def grade_of(score):
    """0–100 分 → A/B/C/D/F（阈值见 :data:`GRADE_BANDS`，非法分数按 F 处理）。"""
    v = _num(score)
    if v is None:
        return "F"
    for g, lo in GRADE_BANDS:
        if v >= lo:
            return g
    return "F"


def verdict_bucket(grade):
    """五档等级 → 三档口径（值得买入 / 观察 / 回避）。"""
    g = str(grade or "").strip().upper()
    return VERDICT_BUCKETS.get(g, "观察")


def composite_score(factors, params=None):
    """五因子加权合成 0–100 分（缺失因子按剩余权重重新归一）。

    参数
    ----
    factors : dict
        ``{momentum, volume, trend, position, risk}``，每个 0–100 或 ``None``
        （``None`` = 该项数据缺失，权重会被剔除而非按 0 分或 50 分处理）。
    params : dict | None
        只有 ``weights`` 会用到（组级或因子级写法均可）。

    返回
    ----
    dict
        ``score`` 0–100 整数（四舍五入）；``grade``；``weights`` 生效权重；
        ``used`` / ``dropped`` 参与与缺席的因子；``renormalized`` 是否重新归一化；
        ``note`` 权重说明。全部因子缺失时 ``score = 0``（缺数据不臆造）。
    """
    weights, wnote = _resolve_weights(_get(params if isinstance(params, dict) else {},
                                            "weights"))
    src = factors if isinstance(factors, dict) else {}
    used, dropped = {}, {}
    num = den = 0.0
    for f in FACTORS:
        v = _num(src.get(f))
        w = weights[f]
        if v is None or w <= 0:
            if v is None:
                dropped[f] = w
            continue
        v = _clamp(v, 0.0, 100.0)
        used[f] = v
        num += w * v
        den += w
    if den <= _EPS:
        score = 0
        note = wnote + "；无可用因子（或权重全为 0），按 0 分处理（缺数据不臆造）"
    else:
        score = int(math.floor(num / den + 0.5))
        score = int(_clamp(score, 0, 100))
        note = wnote
        if den < 1.0 - 1e-9:
            note += "；因子 %s 缺数据，权重已在可用因子上重新归一" % (
                ", ".join(sorted(dropped)) or "部分",)
    return {
        "score": score,
        "grade": grade_of(score),
        "weights": {f: round(weights[f], 6) for f in FACTORS},
        "used": {f: _r(v, 1) for f, v in used.items()},
        "dropped": sorted(dropped),
        "renormalized": bool(dropped and den > _EPS),
        "note": note,
    }


def _verdict_text(grade, factors, risk, ctx):
    """一句话中文结论：等级 + 「为什么」（前端只展示这句 + 等级）。"""
    label = VERDICT_LABELS.get(grade, "回避")
    vals = {k: v for k, v in (factors or {}).items() if v is not None}
    risk_bit = "风险分 %s/10（%s）" % (risk["score"], risk["level"])
    if not vals:
        why = "缺少可用的技术面数据，无法形成有效评估（仅提示数据不足）"
    elif grade in ("A", "B"):
        top = sorted(vals.items(), key=lambda kv: (-kv[1], kv[0]))[:2]
        why = "%s 为主要支撑（%s），%s" % (
            "、".join(FACTOR_CN[k].split("（")[0] for k, _ in top),
            "、".join("%.0f 分" % (v,) for _, v in top), risk_bit)
    elif grade == "C":
        hi = max(vals.items(), key=lambda kv: (kv[1], kv[0]))
        low = min(vals.items(), key=lambda kv: (kv[1], kv[0]))
        why = "多空因素交织：%s 相对占优（%.0f 分）而 %s 偏弱（%.0f 分），%s" % (
            FACTOR_CN[hi[0]].split("（")[0], hi[1],
            FACTOR_CN[low[0]].split("（")[0], low[1], risk_bit)
    else:
        low = sorted(vals.items(), key=lambda kv: (kv[1], kv[0]))[:2]
        why = "%s 明显偏弱（%s），%s" % (
            "、".join(FACTOR_CN[k].split("（")[0] for k, _ in low),
            "、".join("%.0f 分" % (v,) for _, v in low), risk_bit)
    if ctx.get("changePct") is not None and ctx.get("changePctSource") == "bars":
        why += "；当日涨幅字段缺失，已用最后一根 K 线推算 %.2f%%" % (ctx["changePct"],)
    return "%s：%s" % (label, why)


# --------------------------------------------------------------------------- #
# 五、单标的评分
# --------------------------------------------------------------------------- #
def score_candidate(row, bars, params=None, extras=None):
    """给单只标的打 0–100 分（五因子加权），返回可解释的等级与一句话结论。

    参数
    ----
    row : dict
        快照行（字段可缺）；``bars`` 可以补足量比 / 均额比 / 60 日涨幅 / 涨幅。
    bars : list[dict]
        K 线（旧 → 新）；为空时所有技术面因子记为缺失（``None``），缺失项按剩余
        权重归一化，**不用快照值编造技术面得分**。
    params : dict | None
        见 :data:`DEFAULT_SCAN_PARAMS`。
    extras : dict | None
        上游预计算的补充字段（只补 row 中缺的键），键名与快照一致：
        ``volumeRatio`` / ``amountRatio`` / ``turnover`` / ``chg60d`` / ``amount``。

    返回
    ----
    dict
        ``score`` 0–100（含封顶）；``rawScore`` 封顶前的合成分（审计 / 测试用）；
        ``capped`` 命中的封顶规则；``grade`` A–F；``verdict`` 一句话中文（含「为什么」）；
        ``verdict3`` 三档口径；``factors`` 五因子（``risk`` 已反向，越高越安全）；
        ``weights`` 生效权重；``reasons`` 支撑理由（每条都带权重或多指标语境，
        不出现单指标买入结论）；``warnings`` 数据降级与风险提示；``metrics`` 指标原值；
        ``risk`` 风险分明细；``degraded`` 是否发生了数据降级。

    封顶规则（调研结论 3/4 的落实，见模块文档「已知取舍」）
    ----------------------------------------------------
    · 风险分达 AVOID（HIGH）→ 总分封顶 49（最高 D 档）；CRITICAL 或命中整只剔除项
      → 封顶 34（F 档）；
    · 过热（RSI > ``rsi_hot`` / 近 5 日涨幅 > 12% / 近 60 日涨幅 ≥ ``pct60d_warn``）
      → 封顶 64（最高 C 档，不给 A/B）。
    扣分权重只有 0.10–0.25，单靠扣分拦不住「已经涨上天」的标的，故另设封顶。

    边界
    ----
    `row=None` / `bars=None` / 脏数据一律不抛异常；无任何可用因子时 ``score = 0``、
    ``grade = "F"``、``degraded = True``，并给出「数据不足」的 warning。
    """
    p = _norm_params(params)
    b = _clean_bars(bars)
    src = _merge_row(row, extras)
    ctx = _context(src, b, p)
    risk = risk_score(src, b, p)

    raw_factors = {
        "trend": _trend_factor(ctx, p)[0],
        "position": _position_factor(ctx, p)[0],
        "volume": _volume_factor(ctx, p)[0],
        "momentum": _momentum_factor(ctx, p)[0],
        # 风险因子（反向）：只有在真的算出了至少一项风险证据、且 K 线长度足以让
        # 这些证据有意义时才给分；否则记 None（缺数据不臆造「安全」这个结论）。
        "risk": (100.0 - 10.0 * risk["score"])
        if (risk["evidence"] and ctx["bars"] >= p["reject_bars"]) else None,
    }
    comp = composite_score(raw_factors, p)
    factors = {f: _r(raw_factors[f], 1) for f in FACTORS}

    # 封顶规则（调研结论 3/4 的落实）：扣分权重不足以拦住「涨上天 / 过热」，
    # 因此另设两条硬性上限；rawScore 保留未封顶的合成分供审计与测试。
    caps = []
    if risk["reject"]:
        caps.append((34, "风险分触发整只剔除项（%s）" % (
            "；".join(f["label"] for f in risk["flags"] if f["reject"]) or "极端",)))
    elif risk["action"] == "AVOID":
        caps.append((34 if risk["level"] == "CRITICAL" else 49,
                     "风险分 %d/10 达 %s / AVOID 档" % (risk["score"], risk["level"])))
    hot = []
    if ctx["rsi"] is not None and ctx["rsi"] > p["rsi_hot"]:
        hot.append("RSI %.1f > %.0f" % (ctx["rsi"], p["rsi_hot"]))
    if ctx["ret5"] is not None and ctx["ret5"] > 12.0:
        hot.append("近 5 日涨幅 %+.2f%% > 12%%" % (ctx["ret5"],))
    if ctx["pct60d"] is not None and ctx["pct60d"] >= p["pct60d_warn"]:
        hot.append("近 60 日涨幅 %.1f%% ≥ %.0f%%" % (ctx["pct60d"], p["pct60d_warn"]))
    if hot:
        caps.append((64, "过热封顶：%s（短期反转取向，不给 A/B 档）" % ("、".join(hot),)))
    score = comp["score"]
    for cap, _why in caps:
        score = min(score, cap)
    score = int(_clamp(score, 0, 100))
    grade = grade_of(score)

    reasons, warnings = [], []
    if ctx["bars"]:
        if factors["trend"] is not None and factors["trend"] >= 60:
            reasons.append("趋势（权重 %.2f）：收盘 %.3f 位于 MA20 %s %.2f%%，%s，MA20 "
                           "近 10 日斜率 %s" % (
                               p["weights"]["trend"], ctx["close"], "上方" if (
                                   ctx["ma20Dev"] or 0) >= 0 else "下方",
                               abs(ctx["ma20Dev"] or 0.0),
                               "MA5 > MA20 > MA60 多头排列" if (
                                   ctx["ma5"] and ctx["ma20"] and ctx["ma60"]
                                   and ctx["ma5"] > ctx["ma20"] > ctx["ma60"])
                               else "均线结构尚未完全转多",
                               _fmt(ctx["ma20Slope"], 2, "%")))
        if factors["position"] is not None and factors["position"] >= 60:
            ptext = _position_factor(ctx, p)[1]
            reasons.append("位置（权重 %.2f）：%s" % (p["weights"]["position"],
                                                     "；".join(ptext) or "位置中性"))
        if factors["volume"] is not None and factors["volume"] >= 60:
            reasons.append("量能（权重 %.2f）：量比 %s（%s）、5/20 日均额比 %s、换手率 %s" % (
                p["weights"]["volume"], _fmt(ctx["volumeRatio"]),
                {"row": "快照口径", "bars": "K线推算", None: "缺失"}[ctx["volumeRatioSource"]],
                _fmt(ctx["amountRatio"]), _fmt(ctx["turnover"], 2, "%")))
        if factors["momentum"] is not None and factors["momentum"] >= 60:
            reasons.append("动量（权重 %.2f，短期反转取向）：RSI %s 处于 %.0f–%.0f 的温和"
                           "走强区间，近 5 日 %s、近 20 日 %s，未见短期过热" % (
                               p["weights"]["momentum"], _fmt(ctx["rsi"], 1),
                               p["rsi_best_low"], p["rsi_best_high"],
                               _fmt(ctx["ret5"], 2, "%"), _fmt(ctx["ret20"], 2, "%")))
        elif ctx["rsi"] is not None and factors["momentum"] is not None and ctx["rsi"] > p["rsi_hot"]:
            warnings.append("动量：RSI %.1f 已进入过热区（> %.0f），按短期反转取向扣分，"
                            "该指标不作为买入理由" % (ctx["rsi"], p["rsi_hot"]))
        reasons.append("风险（反向权重 %.2f）：%s" % (p["weights"]["risk"], risk["summary"]))
    else:
        warnings.append("无有效 K 线，技术面因子（趋势/位置/量能/动量）全部缺失，"
                        "已按剩余因子归一化，不使用快照数据编造技术面得分")

    if ctx["bars"] and ctx["bars"] < p["min_bars"]:
        warnings.append("K 线仅 %d 根（< %d），长周期因子（MA60 / 60 日涨幅 / 60 日回撤）"
                        "精度下降" % (ctx["bars"], p["min_bars"]))
    for key, label in (("turnover", "换手率"), ("volumeRatio", "量比"),
                       ("amountRatio", "5/20 日均额比"), ("pct60d", "60 日累计涨幅")):
        if ctx.get(key) is None:
            warnings.append("%s（%s）缺失，对应子项已从该因子分子分母中剔除（降级不臆造）" % (
                label, key))
    if ctx.get("changePctSource") == "bars":
        warnings.append("当日涨幅（changePct）缺失，已用最后一根 K 线推算 %s，反追高闸门"
                        "在扫描阶段会跳过该标的" % _fmt(ctx["changePct"], 2, "%"))
    if ctx.get("changePct") is not None and ctx["changePct"] > p["max_change_pct"][ctx["market"]]:
        warnings.append("当日涨幅 %.2f%% 超过上限 %.2f%%，属追高位置（不影响评分口径，"
                        "但扫描阶段会被硬闸门②拦截）" % (
                            ctx["changePct"], p["max_change_pct"][ctx["market"]]))
    for f in risk["flags"]:
        warnings.append("风险提示：%s" % (f["label"],))
    if comp["dropped"]:
        warnings.append("因子 %s 无数据，权重已在可用因子上重新归一" % (
            "、".join(FACTOR_CN[f].split("（")[0] for f in comp["dropped"]),))
    for cap, why in caps:
        warnings.append("评分封顶至 %d 分：%s" % (cap, why))

    metrics = {
        "price": _r(ctx["close"], 4), "changePct": _r(ctx["changePct"], 3),
        "changePctSource": ctx["changePctSource"],
        "amount": _r(ctx["amount"], 2),
        "volumeRatio": _r(ctx["volumeRatio"], 3),
        "volumeRatioSource": ctx["volumeRatioSource"],
        "amountRatio": _r(ctx["amountRatio"], 3),
        "turnover": _r(ctx["turnover"], 3),
        "ma5": _r(ctx["ma5"], 4), "ma20": _r(ctx["ma20"], 4), "ma60": _r(ctx["ma60"], 4),
        "ma20Dev": _r(ctx["ma20Dev"], 2), "ma20Slope": _r(ctx["ma20Slope"], 2),
        "rsi": _r(ctx["rsi"], 2),
        "ret5": _r(ctx["ret5"], 2), "ret20": _r(ctx["ret20"], 2),
        "pct60d": _r(ctx["pct60d"], 2), "pct60dSource": ctx["pct60dSource"],
        "maxDrop1d": _r(ctx["maxDrop1d"], 2), "maxDrop2d": _r(ctx["maxDrop2d"], 2),
        "atrPct": _r(ctx["atrPct"], 2),
        "resistance": _r(ctx["resistance"], 4),
        "distToResistance": _r(ctx["distToResistance"], 2),
        "atHigh": bool(ctx.get("atHigh")),
        "bars": ctx["bars"],
    }

    note = str(p.get("weightNote") or comp["note"])
    if comp["renormalized"]:
        note += "；因子 %s 缺数据，权重已在可用因子上重新归一" % (
            "、".join(FACTOR_CN[f].split("（")[0] for f in comp["dropped"]),)
    return {
        "score": score,
        "rawScore": comp["score"],
        "capped": [{"cap": int(c), "why": w} for c, w in caps],
        "grade": grade,
        "verdict": _verdict_text(grade, factors, risk, ctx),
        "verdict3": verdict_bucket(grade),
        "factors": factors,
        "weights": comp["weights"],
        "reasons": reasons,
        "warnings": warnings,
        "metrics": metrics,
        "risk": risk,
        # 降级标记：权重重新归一、无 K 线、K 线不足 min_bars、或无任何可用因子
        "degraded": bool(comp["renormalized"] or not comp["used"] or not ctx["bars"]
                         or ctx["bars"] < p["min_bars"]),
        "note": note,
    }


# --------------------------------------------------------------------------- #
# 六、全市场扫描主入口
# --------------------------------------------------------------------------- #
def _reject(bucket, reason, tags=None, code="", name="", price=None, change_pct=None,
            stage="data", detail=""):
    """登记一条剔除记录（``reasons`` 给人看，``tags`` 给 ``stats.byRejectReason`` 聚合）。"""
    item = {
        "code": code, "name": name, "price": _r(price, 4),
        "changePct": _r(change_pct, 3), "stage": stage,
        "reasons": list(reason) if isinstance(reason, (list, tuple)) else [str(reason)],
        "tags": list(tags) if isinstance(tags, (list, tuple)) else ([str(tags)] if tags else []),
    }
    if detail:
        item["detail"] = detail
    bucket.append(item)
    return item


def scan_candidates(rows, bars_map=None, params=None, limit=None):
    """全市场扫描主入口：硬闸门过滤 → 逐只评分 → 排序 → 截断。

    参数
    ----
    rows : list[dict]
        全市场快照行（``code`` / ``name`` / ``price`` / ``changePct`` / ``amount`` /
        ``volume`` / ``turnover`` 等，字段可缺、可为字符串数值）。
    bars_map : dict
        ``{code: [bars]}``，旧 → 新；可缺项（缺项标的直接剔除、不臆造得分）。
    params : dict | None
        见 :data:`DEFAULT_SCAN_PARAMS`；返回里的 ``params`` 是生效参数。
    limit : int | None
        只保留前 N 个候选（先排序后截断）；``None`` / 非法值 / ≤ 0 表示不截断；
        被截断的数量会如实写进 ``stats["truncated"]``。

    返回
    ----
    dict
        ``ok``；``candidates`` 候选（每条含 ``code/name/price/changePct/score/grade/
        verdict/verdict3/factors/reasons/warnings/risk/metrics/spark/degraded``）；
        ``rejected`` 剔除明细（``code/name/price/changePct/stage/reasons/tags``，
        ``stage`` ∈ data（行非法 / 缺码 / 缺K线 / K线过短）、hard（硬闸门）、
        risk（风险分超阈值））；``stats``；``params``；``note``。

        ``stats``：``scanned``（扫描总数）、``scored``（过了硬闸门、完成打分的数量）、
        ``passed``（最终候选数量，截断前）、``rejected``、``byGrade`` A–F 分布、
        ``byRejectReason`` 剔除原因分布、``missingFields``（因字段缺失被跳过的闸门数）、
        ``missingFieldCounts``、``returned``（截断后返回数）、``truncated``（被截断数）。

    处理顺序（重要）
    ----------------
    ① 行合法性 → ② 是否有可用 K 线 → ③ 三道硬闸门 → ④ 风险分阈值剔除 → ⑤ 评分。
    缺 K 线的标的在 ② 就被剔除（原因「无K线数据」），因此**不会**因为快照好看而被
    编造出技术面得分；被剔除的标的一律不参与打分与排序，但仍计入 ``scanned``。

    排序：``score`` 降序，同分按 ``amount`` 降序（流动性更好的优先）；``limit`` 之后
    的候选被截断但仍在 ``stats`` 中计数。任何脏输入都不抛异常。
    """
    p = _norm_params(params)
    row_list = list(rows) if isinstance(rows, (list, tuple)) else []
    bmap = bars_map if isinstance(bars_map, dict) else {}

    lim = _num(limit)
    lim = int(lim) if (lim is not None and lim > 0) else None

    candidates, rejected = [], []
    by_grade = {g: 0 for g, _ in GRADE_BANDS}
    by_reason = {}
    missing_total, missing_counts = 0, {}
    scored = 0

    def bump(reason):
        by_reason[reason] = by_reason.get(reason, 0) + 1

    for idx, row in enumerate(row_list):
        if not isinstance(row, dict):
            stage_code = "#%d" % (idx + 1)
            _reject(rejected, "行数据非法（不是 dict），已跳过", tags=["数据非法"],
                    code=stage_code, stage="data")
            bump("数据非法")
            continue

        code = str(row.get("code") or row.get("symbol") or "").strip()
        name = str(row.get("name") or "").strip()
        price, change = _num(row.get("price")), _num(row.get("changePct"))
        if not code:
            _reject(rejected, "缺少代码（code/symbol 为空），无法匹配K线", tags=["缺少代码"],
                    name=name, price=price, change_pct=change, stage="data")
            bump("缺少代码")
            continue

        raw_bars = bmap.get(code)
        if raw_bars is None:
            raw_bars = bmap.get(str(code).upper())
        bars = _clean_bars(raw_bars)
        if not bars:
            _reject(rejected, "无K线数据：bars_map 中找不到该标的的有效K线，不做技术面评分",
                    tags=["无K线数据"], code=code, name=name, price=price,
                    change_pct=change, stage="data")
            bump("无K线数据")
            continue
        if len(bars) < p["reject_bars"]:
            _reject(rejected, "K线不足：仅 %d 根（< %d 根），无法计算 MA20 与阻力位，"
                              "不做技术面评分" % (len(bars), p["reject_bars"]),
                    tags=["K线不足"], code=code, name=name, price=price,
                    change_pct=change, stage="data", detail="bars=%d" % len(bars))
            bump("K线不足")
            continue

        # 用 K 线补足闸门③所需的量能结构（这是调研结论第 2 条明确要求的口径）
        ctx0 = _context(row, bars, p)
        enriched = dict(row)
        if enriched.get("volumeRatio") is None and ctx0["volumeRatioBars"] is not None:
            enriched["volumeRatio"] = ctx0["volumeRatioBars"]
            enriched["volumeRatioSource"] = "bars"
        if enriched.get("amountRatio") is None and ctx0["amountRatioBars"] is not None:
            enriched["amountRatio"] = ctx0["amountRatioBars"]
        if enriched.get("amount") is None and ctx0["amount"] is not None:
            enriched["amount"] = ctx0["amount"]

        hf = hard_filters(enriched, p)
        missing_total += hf["missingGates"]
        for k, v in hf["missingFieldCounts"].items():
            missing_counts[k] = missing_counts.get(k, 0) + v
        if not hf["pass"]:
            _reject(rejected, hf["reasons"], tags=hf["tags"] or ["硬闸门未通过"],
                    code=code, name=name, price=price, change_pct=change, stage="hard")
            for t in hf["tags"] or ["硬闸门未通过"]:
                bump(t)
            continue

        cand = score_candidate(row, bars, p, extras={
            "volumeRatio": enriched.get("volumeRatio"),
            "volumeRatioSource": enriched.get("volumeRatioSource"),
            "amountRatio": enriched.get("amountRatio"),
            "amount": enriched.get("amount"),
        })
        scored += 1

        risk = cand["risk"]
        if risk["reject"] or risk["score"] >= p["reject_risk_score"]:
            _reject(rejected, "风险分 %d/10（%s，%s）触发剔除阈值 %d：%s" % (
                risk["score"], risk["level"],
                "含整只剔除项" if risk["reject"] else "达阈值",
                p["reject_risk_score"], risk["summary"]),
                tags=["风险分超阈值"], code=code, name=name, price=price,
                change_pct=change, stage="risk",
                detail="flags=%s" % ",".join(f["key"] for f in risk["flags"]))
            bump("风险分超阈值")
            continue

        cand.update({
            "code": code, "name": name or code, "market": _market_of(row),
            "price": _r(price if price is not None else cand["metrics"]["price"], 4),
            "changePct": _r(change if change is not None else cand["metrics"]["changePct"], 3),
            "amount": _r(_num(row.get("amount", enriched.get("amount"))), 2),
            "spark": ctx0["spark"][-int(p["spark_len"]):],
        })
        candidates.append(cand)
        by_grade[cand["grade"]] = by_grade.get(cand["grade"], 0) + 1

    passed = len(candidates)
    candidates.sort(key=lambda c: (-c["score"], -(_num(c.get("amount")) or 0.0)))
    if lim is not None and len(candidates) > lim:
        truncated = len(candidates) - lim
        candidates = candidates[:lim]
    else:
        truncated = 0

    amt_txt = " / ".join("%s %s" % ("A股" if k == "cn" else "美股",
                                    _money(p["min_amount"][k], k)) for k in ("cn", "us"))
    chg_txt = " / ".join("%s %.2f%%" % ("A股" if k == "cn" else "美股",
                                        p["max_change_pct"][k]) for k in ("cn", "us"))
    notes = [
        "生效参数：流动性下限 %s；当日涨幅上限 %s；量比 ≥ %.2f 或 5/20 日均额比 ≥ %.2f；"
        "60 日涨幅 ≥ %.0f%% 扣分 / ≥ %.0f%% 剔除；单日跌幅 > %.1f%% 或两日累计跌幅 > %.1f%% "
        "记回撤风险；风险分 ≥ %d 整只剔除；最少 K 线 %d 根" % (
            amt_txt, chg_txt, p["min_volume_ratio"], p["min_amount_ratio"],
            p["pct60d_warn"], p["pct60d_reject"], p["max_drop_1d"], p["max_drop_2d"],
            p["reject_risk_score"], p["reject_bars"]),
        "本次共扫描 %d 只：过闸门 %d 只、剔除 %d 只（其中因字段缺失被跳过的闸门 %d 次）；"
        "候选 %d 只，截断 %d 只，返回 %d 只（排序：score 降序，同分按 amount 降序）" % (
            len(row_list), scored, len(rejected), missing_total, passed, truncated,
            len(candidates)),
        p["weightNote"],
        SCAN_NOTE,
    ]

    return {
        "ok": True,
        "candidates": candidates,
        "rejected": rejected,
        "stats": {
            "scanned": len(row_list),
            "scored": scored,
            "passed": passed,
            "rejected": len(rejected),
            "byGrade": by_grade,
            "byRejectReason": by_reason,
            "missingFields": missing_total,
            "missingFieldCounts": missing_counts,
            "returned": len(candidates),
            "truncated": truncated,
            "limit": lim,
        },
        "params": p,
        "note": "；".join(notes),
    }
