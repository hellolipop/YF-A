#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AlphaDesk · A股「四类新数据」抓取层（providers/features.py）

零第三方依赖（仅 Python 标准库），直接请求腾讯 / 东方财富的公开行情接口：
  · auction(code)            集合竞价快照（09:15 委托快照 + 09:25 撮合结果）
  · ticks(code, limit)       分笔成交明细（含主动买卖方向、笔数）
  · dragon_tiger(date)       龙虎榜（每日上榜明细）
  · limit_up_ladder(date)    涨停梯队（含连板数、高度分布、断层）

统一返回结构（四个函数完全一致的外层信封，业务数据一律放在 data 内）::

    {
      "ok": True,                # 本次取数是否成功（失败时 data 为「空实现」形状，不会缺字段）
      "data": {...},             # 业务数据，形状固定，见各函数 docstring
      "source": "东方财富（分笔明细·集合竞价段）",
      "fetchedAt": "2026-09-15 13:05:00",   # 本地取数时间（东八区）
      "dataTime": "2026-09-15 09:25:01",    # 数据本身的时间（交易日 / 快照时间）
      "error": None,             # 失败原因（多源失败时为「；」拼接的中文说明）
      "degraded": False,         # True = 主源失败走了降级源，或直接失败
      "stale": False             # True = 上游全部失败，返回的是进程内缓存的上一次结果
    }

网络细节（本机实测必须如此，否则请求会被本机代理 / 证书链打断）：
  1. 绕过本机 HTTP 代理：build_opener(ProxyHandler({}))，不读 http_proxy / macOS 系统代理；
  2. 容忍证书问题：HTTPSHandler(context=ssl._create_unverified_context())；
  3. 腾讯 qt.gtimg.cn 返回 GBK 编码，必须按 GBK 解码（否则股票名乱码）；
  4. 东方财富 push2.eastmoney.com 在本机存在「连接被对端重置」的抖动，
     因此分笔类接口按 push2 → push2delay 顺序自动切换主机。

==============================  实测验证记录（2026-09-15）  ==============================
以下结论全部由 python3 真实请求获得（非推测字段），验证样本：600519 / 000001 / 300750 /
688004 / 430047（北交所）：

[可用] 东方财富分笔明细 push2(/push2delay).eastmoney.com /api/qt/stock/details/get
  · 参数 fields2=f51,f52,f53,f54,f55，pos=0 返回当日全部（实测 600519 共 1607 条），
    pos=-N 返回最近 N 条；返回 data.prePrice（昨收）、data.decimal。
  · 单条格式 "HH:MM:SS,价格,成交量(手),成交笔数,方向"，方向取值实测 {1,2,4}。
  · 口径校验：sum(成交量, 09:30 之后) + 09:25 撮合量 == 腾讯实时行情的当日成交量(手)
    （600519：7617+158=7775 ✓；000001：460179+2034=462213 vs 462212 ✓ ±1 手；
     300750：140882+3156=144038 vs 144037 ✓ ±1 手）。
    09:15~09:25 段的记录成交笔数为 0（属竞价委托快照，不计入成交量）。
  · 方向口径：按方向汇总成交量与腾讯「外盘/内盘」同向但不等（三家样本均如此），
    说明两家的主动买卖判定规则不同，故字段仅作参考，不宣称与腾讯一致。
  · 北交所（430047 / 833171）实测 data.code 正常但 details 为空 → 北交所无分笔。

[可用] 腾讯分笔明细 stock.gtimg.cn/data/index.php
  · action=all|today|list 返回 [日期, "每页时间区间|..."]，可先拿到总页数（如 000001 当日 32 页）；
  · action=data&p=N 返回 70 条/页，时间升序，索引从 0 开始；
  · 单条格式 "序号/时间/价格/相对上一笔的涨跌/成交量(手)/成交额(元)/方向(S/B/M)"；
  · 首页第一条即集合竞价撮合（600519 09:25:01 158 手 1281.00 元 = 当日今开；
    000001 09:25:00 2034 手；300750 09:25:00 3156 手；688004 09:25:03 11935 手）；
  · 首条的「相对上一笔涨跌」字段不可用（600519 首条给出 -1.20，与昨收/今开均不自洽），
    本模块只在连续竞价段使用该字段并单独标注；
  · 不支持历史日期：加 &d=YYYYMMDD 实测返回的仍是当日数据（参数无效），仅当日可用；
  · 北交所（bj430047）实测无返回 → 不支持。

[不可用/已下线] 腾讯 web.ifzq.gtimg.cn/appstock/app/cjmx/getCjmxList
  · 实测返回 {"code":11,"msg":"Can't load controller:CjmxController"}，接口已下线，未采用。

[可用] 东方财富涨停池 push2ex.eastmoney.com/getTopicZTPool
  · 参数 ut / dpt=wz.ztzt / Pageindex / pagesize / sort=fbt:asc / date=YYYYMMDD；
  · 返回 data.tc（涨停家数）、data.qdate（查询日，恒为当日，不能作为入参校验依据）、
    data.pool[].{c,m,n,p,zdp,amount,ltsz,tshare,hs,lbc,fbt,lbt,fund,zbc,hybk,zttj{days,ct}}；
  · 价格缩放实测：p/1000 == 实时价（002912 28450→28.45、000993 16800→16.80、
    688004 27380→27.38 等 6 只逐一与腾讯实时价吻合）；
  · lbc = 连板数（000993 lbc=5，zttj={days:5,ct:5}「5 天 5 板」），zbc = 当日炸板次数，
    fbt/lbt 形如 92500 / 112503（HHMMSS）；
  · 历史深度实测：2026-08-26 起有数据，2026-08-18~08-25 及更早全部返回空 → 仅最近约 20 天。

[可用] 东方财富龙虎榜 datacenter-web.eastmoney.com/api/data/v1/get
  · reportName=RPT_DAILYBILLBOARD_DETAILSNEW，filter=(TRADE_DATE='YYYY-MM-DD')，columns=ALL；
  · 实测 2026-09-14 返回 84 条、2026-09-11 68 条、2026-01-05 77 条、2025-09-15 71 条（历史深度足够）；
  · 空数据返回 {"success":false,"code":9201,"message":"返回数据为空"}（当日盘后未发布 / 周末），
    日期格式错误返回 code 9501「日期格式有误」；
  · 一行 = 一个「上榜原因」，同一标的当日可能多行（不同原因金额不可直接相加，故本模块只做
    结构聚合，不做求和，避免编造口径）。

[口径说明] 集合竞价段（09:15~09:25）的字段语义（实测推断，非官方文档）
  · 东财分笔在 09:15~09:25 给出的记录的「成交笔数」恒为 0，且不计入当日成交量
    （600519：09:30 后合计 7617 + 竞价撮合 158 = 7775 == 腾讯成交量），
    即该段是「竞价期间委托快照」，不是成交；
  · 该段「量」字段实测为**非递增**序列（000001：116→316→317→735→…→1225→690），
    说明它是「每个快照时点的委托量」而非累计未匹配量（撤单会让数值回落），
    因此 auction() 里把逐条合计命名为 orderVolume 并明确标注「仅作参考」，
    同时额外给出 lastOrderVolume（撮合前最后一条快照的委托量，最接近待撮合委托量口径）；
  · 竞价撮合的判定：09:15:00~09:25:59 内**第一条成交笔数 > 0** 的记录
    （600519 → 09:25:01、000001 → 09:25:00、300750 → 09:25:00、688004 → 09:25:03）。
    其方向字段实测不稳定（同为竞价撮合，600519/000001 给 2、300750 给 1），故不作买卖标注。

[未实现] 集合竞价「未匹配量 / 撤单量」：腾讯与东方财富的公开接口均未提供该字段
         （东财 push2 stock/get 的竞价扩展字段在本机实测连接被重置，无稳定来源），
         故 auction() 中 unmatched 恒为空并在 unmatchedNote 中说明，前端应做降级展示。
==========================================================================================
免责声明：数据来自公开行情接口，仅供研究学习，不构成投资建议。
"""

import gzip
import json
import re
import ssl
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TIMEOUT = 10
RETRY = 1

CN_TZ = timezone(timedelta(hours=8))

# 东方财富分笔：主域名在本机存在连接抖动，push2delay 实测稳定，做主机级降级
EM_PUSH_HOSTS = ("https://push2.eastmoney.com", "https://push2delay.eastmoney.com")
EM_PUSH_UT = "fa5fd1943c7b386f172d6893dbfba10b"
EM_DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EM_ZT_POOL = "https://push2ex.eastmoney.com/getTopicZTPool"
EM_ZT_UT = "7eea3edcaed734bea9cbfc24409ed989"

REF_EM = "https://quote.eastmoney.com/"
REF_EM_DATA = "https://data.eastmoney.com/"
REF_TX = "https://gu.qq.com/"

# 竞价委托快照明细最多回传的条数（避免响应过大）
AUCTION_TICK_LIMIT = 30
# 分笔单次最多返回条数（腾讯源每页 70 条，超出部分只取最后 N 条）
TICKS_MAX = 500
# 龙虎榜分页抓取上限
LHB_PAGE_SIZE = 200
LHB_MAX_PAGES = 5

# 缓存 TTL（秒）：盘中分笔更新快，给短 TTL；榜单数据变化慢，给长 TTL
TTL_AUCTION = 15
TTL_TICKS = 5
TTL_LHB = 300
TTL_ZT = 30

# 交易时段（东八区，含集合竞价）
AUCTION_START = "09:15:00"
AUCTION_MATCH = "09:25:59"   # <= 该时刻且成交笔数 > 0 的第一条 = 开盘集合竞价撮合

# --------------------------------------------------------------------------- #
# HTTP：绕过本机代理 + 容忍证书问题
# --------------------------------------------------------------------------- #

_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),                                   # 显式置空 → 不走本机 HTTP 代理
    urllib.request.HTTPSHandler(context=ssl._create_unverified_context()),  # 容忍证书链缺失
)


def _http(url, referer=REF_EM, encoding="utf-8", timeout=TIMEOUT, retry=RETRY):
    """GET 文本（自动解 gzip，失败重试 retry 次）"""
    last = None
    for i in range(retry + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Referer": referer, "Accept": "*/*",
                "Accept-Encoding": "gzip, deflate", "Connection": "close",
            })
            with _OPENER.open(req, timeout=timeout) as resp:
                raw = resp.read()
                if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                    raw = gzip.decompress(raw)
            return raw.decode(encoding, errors="ignore")
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i < retry:
                time.sleep(0.25)
    raise RuntimeError("HTTP 请求失败：%s（%s）" % (url[:120], last))


def _json(url, referer=REF_EM, timeout=TIMEOUT, retry=RETRY):
    return json.loads(_http(url, referer=referer, timeout=timeout, retry=retry))


# --------------------------------------------------------------------------- #
# 进程内缓存（与 server.py 同风格：可返回 stale 旧值）
# --------------------------------------------------------------------------- #

_CACHE = {}
_CACHE_LOCK = threading.Lock()


def _cache_put(key, val):
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), val)


def _cache_peek(key):
    with _CACHE_LOCK:
        item = _CACHE.get(key)
    return (item[1], time.time() - item[0]) if item else (None, None)


def _cached(key, ttl, producer):
    """TTL 内直接返回；上游失败时回退到最近一次成功结果并打 stale 标记"""
    with _CACHE_LOCK:
        item = _CACHE.get(key)
    if item and time.time() - item[0] <= ttl:
        out = dict(item[1])
        out["stale"] = False
        return out
    try:
        val = producer()
        _cache_put(key, val)
        out = dict(val)
        out["stale"] = False
        return out
    except Exception:  # noqa: BLE001
        val, age = _cache_peek(key)
        if val is not None:
            out = dict(val)
            out["stale"] = True
            out["staleAge"] = int(age) if age else None
            return out
        raise


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

def _now():
    return datetime.now(CN_TZ)


def _iso(ts=None):
    if ts is None:
        return _now().strftime("%Y-%m-%d %H:%M:%S")
    return datetime.fromtimestamp(ts, CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _num(v, default=None):
    """宽松数值转换（'-'、''、None 一律返回 default）"""
    if v is None or v == "-" or v == "":
        return default
    try:
        f = float(v)
        return default if f != f else f
    except (TypeError, ValueError):
        return default


def _hhmmss(v):
    """涨停池封板时间 92500 / 112503 -> '09:25:00' / '11:25:03'"""
    n = _num(v)
    if not n:
        return None
    n = int(n)
    return "%02d:%02d:%02d" % (n // 10000, (n // 100) % 100, n % 100)


def _norm_date(date):
    """日期归一化：None / '20260915' / '2026-09-15' / '2026/09/15' -> 'YYYY-MM-DD'

    非法日期抛 ValueError（东财对非法日期返回 code 9501「日期格式有误」）。
    """
    if date is None or str(date).strip() == "":
        return _now().strftime("%Y-%m-%d")
    s = str(date).strip().replace("/", "-").replace(".", "-")
    if re.match(r"^\d{8}$", s):
        s = "%s-%s-%s" % (s[0:4], s[4:6], s[6:8])
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise ValueError("日期格式应为 YYYY-MM-DD 或 YYYYMMDD，实际收到 %r" % (date,))


def _norm_code(code):
    """统一代码：返回 (market, 6 位代码)；market ∈ sh / sz / bj

    支持 '600519' / 'sh600519' / '600519.SH' / 'SZ000001' 等写法。
    """
    s = str(code or "").strip().upper().replace(" ", "")
    m = re.match(r"^(SH|SZ|BJ)(\d{6})$", s)
    if m:
        return m.group(1).lower(), m.group(2)
    m = re.match(r"^(\d{6})\.(SH|SZ|BJ)$", s)
    if m:
        return m.group(2).lower(), m.group(1)
    m = re.match(r"^(\d{6})$", s)
    if not m:
        raise ValueError("无法识别的股票代码：%r" % code)
    d = m.group(1)
    if d[0] == "6" or d[0] == "9":
        return "sh", d
    if d[0] in ("0", "2", "3"):
        return "sz", d
    if d[0] in ("4", "8"):
        return "bj", d
    return "sh", d


def _tx_symbol(code):
    market, d = _norm_code(code)
    return market + d


def _em_secid(code):
    """东方财富 secid：沪市（含科创板）1.xxxxxx，深市（含创业板）0.xxxxxx"""
    market, d = _norm_code(code)
    return ("1." if market == "sh" else "0.") + d


def _wrap(ok, data, source, error=None, data_time=None, degraded=False, stale=False):
    """统一信封"""
    return {
        "ok": bool(ok),
        "data": data,
        "source": source,
        "fetchedAt": _iso(),
        "dataTime": data_time,
        "error": error,
        "degraded": bool(degraded),
        "stale": bool(stale),
    }


# =========================================================================== #
# 数据源 A：东方财富分笔明细
# =========================================================================== #

EM_SIDE_TEXT = {"1": "卖盘", "2": "买盘", "4": "中性"}
TX_SIDE_TEXT = {"S": "卖盘", "B": "买盘", "M": "中性"}
EM_SIDE_RULE = "东方财富：1=卖盘 2=买盘 4=中性（实测主动买卖判定与腾讯外盘/内盘口径存在差异，仅供参考）"
TX_SIDE_RULE = "腾讯：S=卖盘 B=买盘 M=中性"


def _em_details_url(host, secid, pos, mpi):
    return ("%s/api/qt/stock/details/get?fields1=f1,f2,f3,f4&fields2=f51,f52,f53,f54,f55"
            "&mpi=%d&pos=%d&secid=%s&iscca=1&ut=%s&invt=2" % (host, mpi, pos, secid, EM_PUSH_UT))


def _em_details(code, pos=0, mpi=100000):
    """拉取东方财富分笔原始响应（主机级降级：push2 → push2delay）"""
    secid = _em_secid(code)
    errs = []
    for host in EM_PUSH_HOSTS:
        try:
            r = _json(_em_details_url(host, secid, pos, mpi), referer=REF_EM)
            d = r.get("data") or {}
            if d.get("details"):
                return {
                    "host": host,
                    "code": d.get("code"),
                    "preClose": _num(d.get("prePrice")),
                    "decimal": _num(d.get("decimal")),
                    "details": d.get("details") or [],
                }
            errs.append("%s 返回空明细" % host.split("//")[-1])
        except Exception as exc:  # noqa: BLE001
            errs.append("%s %s" % (host.split("//")[-1], exc))
    raise RuntimeError("；".join(errs) or "东方财富分笔无数据")


def _parse_em_details(details):
    """东方财富分笔字符串 -> 结构化列表（时间升序）

    单条格式："HH:MM:SS,价格,成交量(手),成交笔数,方向"
    """
    out = []
    for line in details or []:
        f = str(line).split(",")
        if len(f) < 5:
            continue
        price = _num(f[1])
        vol = _num(f[2])
        if price is None or vol is None:
            continue
        side = f[4]
        out.append({
            "time": f[0],
            "price": price,
            "volume": vol,                       # 手
            "amount": round(price * vol * 100, 2),  # 元（1 手 = 100 股）
            "trades": int(_num(f[3]) or 0),      # 成交笔数（竞价委托快照为 0）
            "side": side,
            "sideText": EM_SIDE_TEXT.get(side, "未知"),
        })
    return out


# =========================================================================== #
# 数据源 B：腾讯分笔明细 + 腾讯实时行情
# =========================================================================== #

def _tx_detail_pages(code):
    """返回 (交易日 YYYYMMDD, 每页时间区间列表)；页面按时间升序，每页 70 条"""
    sym = _tx_symbol(code)
    txt = _http("http://stock.gtimg.cn/data/index.php?appn=detail&action=all&c=" + sym,
                referer=REF_TX, timeout=TIMEOUT)
    m = re.search(r'\[(\d{4}\d{2}\d{2})\s*,\s*"(.*?)"\s*\]', txt, re.S)
    if not m:
        return None, []
    ranges = [x.strip().strip('"') for x in m.group(2).split("|") if x.strip()]
    return m.group(1), ranges


def _parse_tx_detail_page(txt):
    """腾讯分笔字符串 -> 结构化列表（页内时间升序）

    原始响应形如：v_detail_data_sh600519=[0,"0/09:25:01/1281.00/-1.20/158/20239800/S|1/..."]

    单条格式："序号/时间/价格/相对上一笔的涨跌/成交量(手)/成交额(元)/方向"
    """
    m = re.search(r'=\[[^"]*"(.*?)"\s*\]', txt, re.S) or re.search(r'"(.*?)"', txt, re.S)
    if not m:
        return []
    body = m.group(1).strip()
    out = []
    for item in body.split("|"):
        f = item.split("/")
        if len(f) < 6:
            continue
        price = _num(f[2])
        vol = _num(f[4])
        if price is None or vol is None:
            continue
        side = (f[6] if len(f) > 6 else "M").upper()
        out.append({
            "seq": int(_num(f[0]) or 0),
            "time": f[1],
            "price": price,
            "volume": vol,                    # 手
            "amount": _num(f[5]),             # 元（数据源真实成交额）
            "trades": None,                   # 腾讯分笔不提供笔数
            "side": side,
            "sideText": TX_SIDE_TEXT.get(side, "未知"),
            "_rawChange": _num(f[3]),         # 相对上一笔的涨跌（首条不可靠）
        })
    return out


def _tx_detail_last(code, limit):
    """腾讯分笔：按「末页往前」取最后 limit 条（时间升序）"""
    date, ranges = _tx_detail_pages(code)
    if not ranges:
        return date, []
    need = max(1, int(limit))
    pages = []
    idx = len(ranges) - 1
    while idx >= 0 and sum(len(p) for p in pages) < need:
        sym = _tx_symbol(code)
        txt = _http("http://stock.gtimg.cn/data/index.php?appn=detail&action=data&c=%s&p=%d"
                    % (sym, idx), referer=REF_TX, timeout=TIMEOUT)
        pages.insert(0, _parse_tx_detail_page(txt))
        idx -= 1
    rows = [r for p in pages for r in p]
    return date, rows[-need:]


def _parse_tx_quote(txt):
    """腾讯实时行情 -> 关键字段（GBK 解码后解析）"""
    m = re.search(r'="(.*)";?', txt, re.S)
    if not m:
        return None
    f = m.group(1).split("~")
    if len(f) < 35:
        return None
    t = f[30].strip()
    stamp = None
    if len(t) >= 14:
        stamp = "%s-%s-%s %s:%s:%s" % (t[0:4], t[4:6], t[6:8], t[8:10], t[10:12], t[12:14])
    return {
        "code": f[2], "name": f[1], "price": _num(f[3]), "preClose": _num(f[4]),
        "open": _num(f[5]), "volume": _num(f[6]), "outer": _num(f[7]), "inner": _num(f[8]),
        "stamp": stamp, "change": _num(f[31]), "changePct": _num(f[32]),
        "high": _num(f[33]), "low": _num(f[34]),
    }


def _tx_quote(code):
    """腾讯实时行情（qt.gtimg.cn 返回 GBK，必须按 GBK 解码）"""
    txt = _http("https://qt.gtimg.cn/q=" + _tx_symbol(code), referer=REF_TX, encoding="gbk")
    q = _parse_tx_quote(txt)
    if not q:
        raise RuntimeError("腾讯行情无数据或格式变化")
    return q


# =========================================================================== #
# 对外接口 1：集合竞价快照
# =========================================================================== #

def _empty_auction(code=None):
    """集合竞价「空实现」形状（上游全部失败时返回，字段齐备便于前端降级）"""
    return {
        "code": code,
        "market": None,
        "tradeDate": None,
        "phase": "no_data",           # matched(已撮合) / auctioning(竞价中) / no_data(无数据)
        "auctionTime": None,          # 竞价撮合时间
        "price": None,                # 竞价成交价（= 当日今开）
        "volume": None,               # 竞价成交量（手）
        "amount": None,               # 竞价成交额（元）
        "trades": None,               # 竞价撮合笔数
        "preClose": None,
        "change": None,
        "changePct": None,
        "priceSource": None,          # 竞价价格来源说明
        "orderCount": 0,              # 09:15~09:25 竞价委托快照条数
        # 逐条快照量合计（实测该序列非递增，说明是「每快照委托量」而非累计未匹配量，
        # 撤单会造成数值回落，故合计值偏大，仅作参考）
        "orderVolume": None,
        "lastOrderVolume": None,      # 撮合前最后一条快照的委托量（最接近「待撮合委托量」的口径）
        "orderTicks": [],             # 竞价委托快照明细（最多 AUCTION_TICK_LIMIT 条）
        "lastOrder": None,            # 撮合前最后一条委托快照
        "unmatched": None,            # 未匹配量：公开接口缺失，恒为 None
        "unmatchedNote": ("公开接口（腾讯 / 东方财富）均未提供集合竞价未匹配量与撤单量，"
                          "该字段留空，前端应做降级展示"),
    }


def _auction_from_em(code, today):
    """东方财富分笔：09:15~09:25 委托快照 + 09:25 撮合"""
    raw = _em_details(code, pos=0, mpi=100000)
    rows = _parse_em_details(raw["details"])
    if not rows:
        raise RuntimeError("东方财富分笔无当日明细")
    pre = raw.get("preClose")
    auc_rows = [r for r in rows if AUCTION_START <= r["time"] <= AUCTION_MATCH]
    orders = [r for r in auc_rows if r["trades"] == 0]
    match = next((r for r in auc_rows if r["trades"] > 0), None)

    order_ticks = [{"time": r["time"], "price": r["price"], "volume": r["volume"]}
                   for r in orders[-AUCTION_TICK_LIMIT:]]
    out = {
        "code": raw.get("code") or _norm_code(code)[1],
        "market": _norm_code(code)[0],
        "tradeDate": "%s-%s-%s" % (today[0:4], today[4:6], today[6:8]),
        "orderCount": len(orders),
        "orderVolume": round(sum(r["volume"] for r in orders), 2) if orders else None,
        "orderTicks": order_ticks,
        "lastOrder": order_ticks[-1] if order_ticks else None,
        "lastOrderVolume": order_ticks[-1]["volume"] if order_ticks else None,
    }
    if match:
        vol = match["volume"]
        out.update({
            "phase": "matched",
            "auctionTime": match["time"],
            "price": match["price"],
            "volume": vol,
            "amount": round(match["price"] * vol * 100, 2),
            "trades": match["trades"],
            "priceSource": "东方财富分笔：09:15~09:25 段内首条成交笔数>0 的记录（开盘集合竞价撮合）",
        })
    else:
        last = order_ticks[-1] if order_ticks else None
        out.update({
            "phase": "auctioning" if orders else "no_data",
            "auctionTime": last["time"] if last else None,
            "price": last["price"] if last else None,
            "volume": None, "amount": None, "trades": None,
            "priceSource": ("竞价进行中（尚未撮合），price 为最后一条竞价委托快照价，仅供盘中观察"
                            if orders else None),
        })
    if pre:
        out["preClose"] = pre
        if out["price"] is not None:
            out["change"] = round(out["price"] - pre, 3)
            out["changePct"] = round((out["price"] / pre - 1) * 100, 3)
    return out


def _auction_from_tx(code):
    """腾讯降级：分笔首页首条（竞价撮合）+ 实时行情（今开/昨收）"""
    q = _tx_quote(code)
    date, rows = _tx_detail_last(code, 1)
    snap = None
    if rows:
        first = rows[0]
        if first["time"] <= AUCTION_MATCH and first["volume"]:
            snap = {
                "phase": "matched",
                "auctionTime": first["time"],
                "price": first["price"],
                "volume": first["volume"],
                "amount": first["amount"],
                "trades": None,
                "priceSource": "腾讯分笔首页第一条（开盘集合竞价撮合）",
            }
    if snap is None and q.get("open"):
        snap = {
            "phase": "matched",
            "auctionTime": None,
            "price": q.get("open"),
            "volume": None,
            "amount": None,
            "trades": None,
            "priceSource": "腾讯实时行情「今开」（分笔缺失时的兜底；竞价成交量不可得）",
        }
    if snap is None:
        raise RuntimeError("腾讯分笔与行情均无当日竞价数据")
    pre = q.get("preClose")
    snap.update({
        "code": q.get("code") or _norm_code(code)[1],
        "market": _norm_code(code)[0],
        "tradeDate": ("%s-%s-%s" % (date[0:4], date[4:6], date[6:8])) if date else None,
        "orderCount": 0,
        "orderVolume": None,
        "lastOrderVolume": None,
        "orderTicks": [],
        "lastOrder": None,
    })
    if pre:
        snap["preClose"] = pre
        snap["change"] = round(snap["price"] - pre, 3)
        snap["changePct"] = round((snap["price"] / pre - 1) * 100, 3)
    return snap


def auction(code):
    """集合竞价快照。

    数据源：东方财富分笔（09:15~09:25 竞价委托快照 + 09:25 撮合）为主；
            腾讯分笔首条 + 腾讯实时行情为降级源。
    返回：统一信封，data 见 :func:`_empty_auction`（键名固定，失败时也保持同形状）。
    说明：北交所无分笔数据（实测），仅当腾讯行情能给出「今开」时才能兜底；
          「未匹配量/撤单量」公开接口不提供，恒为 None；
          orderVolume 是竞价段逐条快照量的合计（该序列非递增，含撤单回落，仅作参考），
          lastOrderVolume 为撮合前最后一条快照的委托量，口径更接近「待撮合委托量」。
    """
    market, d = _norm_code(code)
    key = "auction:%s%s" % (market, d)

    def build():
        errs = []
        try:
            data = _auction_from_em(d, _now().strftime("%Y%m%d"))
            if data.get("phase") != "no_data":
                src = "东方财富（分笔明细·集合竞价段）"
                dt = "%s %s" % (data["tradeDate"], data["auctionTime"]) if data.get("auctionTime") \
                    else data.get("tradeDate")
                return _wrap(True, data, src, data_time=dt)
            errs.append("东方财富分笔：当日无集合竞价记录（可能未开盘或非交易日）")
        except Exception as exc:  # noqa: BLE001
            errs.append("东方财富分笔：%s" % exc)
        try:
            data = _auction_from_tx(d)
            dt = "%s %s" % (data["tradeDate"], data["auctionTime"]) if data.get("auctionTime") \
                else data.get("tradeDate")
            return _wrap(True, data, "腾讯行情（分笔首条 + 实时行情，降级）",
                         data_time=dt, degraded=True)
        except Exception as exc:  # noqa: BLE001
            errs.append("腾讯行情：%s" % exc)
        if market == "bj":
            errs.append("北交所（4/8 开头）实测无集合竞价/分笔数据，行情也无今开可兜底")
        return _wrap(False, _empty_auction(d), None, error="；".join(errs), degraded=True)

    try:
        return _cached(key, TTL_AUCTION, build)
    except Exception as exc:  # noqa: BLE001  （缓存也无旧值时兜底，绝不抛异常给调用方）
        return _wrap(False, _empty_auction(d), None, error=str(exc), degraded=True)


# =========================================================================== #
# 对外接口 2：分笔成交明细
# =========================================================================== #

def _empty_ticks(code=None):
    """分笔「空实现」形状"""
    return {
        "code": code,
        "market": None,
        "tradeDate": None,
        "preClose": None,
        "ticks": [],
        "count": 0,
        "latestTime": None,
        "latestPrice": None,
        "sideRule": None,
        "note": None,
    }


def _ticks_from_em(code, limit):
    """东方财富分笔：pos=-limit 直接取最近 limit 笔"""
    raw = _em_details(code, pos=-limit, mpi=limit)
    rows = _parse_em_details(raw["details"])
    return rows[-limit:], raw.get("preClose"), _now().strftime("%Y-%m-%d")


def _ticks_from_tx(code, limit):
    """腾讯降级分笔（仅当日；不提供笔数，方向为 B/S/M）

    返回 (rows, preClose, 交易日)；与 _ticks_from_em 保持同一形状。
    """
    date, rows = _tx_detail_last(code, limit)
    try:
        pre = _tx_quote(code).get("preClose")
    except Exception:  # noqa: BLE001
        pre = None
    if date:
        day = "%s-%s-%s" % (date[0:4], date[4:6], date[6:8])
    else:
        day = _now().strftime("%Y-%m-%d")
    # 口径差异：腾讯的 amount 是数据源真实成交额，trades 缺失记为 None（不猜）
    return rows, pre, day


def ticks(code, limit=60):
    """分笔成交明细（时间升序，最后一条为最新）。

    数据源：东方财富分笔（含成交笔数与方向 1/2/4）为主；腾讯分笔（方向 B/S/M）为降级源。
    返回：统一信封；data.ticks 每项 {time, price, volume(手), amount(元), trades, side, sideText}。
    限制：仅当日分笔（两个源都不支持历史日期，腾讯 &d= 参数实测无效）；
          北交所无分笔数据；腾讯源 trades 恒为 None。
    """
    market, d = _norm_code(code)
    lim = max(1, min(int(limit or 60), TICKS_MAX))
    key = "ticks:%s%s:%d" % (market, d, lim)

    def build():
        errs = []
        try:
            rows, pre, day = _ticks_from_em(d, lim)
            if rows:
                data = {
                    "code": d, "market": market, "tradeDate": day, "preClose": pre,
                    "ticks": rows, "count": len(rows),
                    "latestTime": rows[-1]["time"], "latestPrice": rows[-1]["price"],
                    "sideRule": EM_SIDE_RULE, "note": None,
                }
                return _wrap(True, data, "东方财富（分笔明细 push2）",
                             data_time="%s %s" % (day, rows[-1]["time"]))
            errs.append("东方财富分笔：当日无明细（可能未开盘或非交易日）")
        except Exception as exc:  # noqa: BLE001
            errs.append("东方财富分笔：%s" % exc)
        try:
            rows, pre, day = _ticks_from_tx(d, lim)
            if rows:
                data = {
                    "code": d, "market": market, "tradeDate": day, "preClose": pre,
                    "ticks": rows, "count": len(rows),
                    "latestTime": rows[-1]["time"], "latestPrice": rows[-1]["price"],
                    "sideRule": TX_SIDE_RULE,
                    "note": "降级源：腾讯分笔不提供成交笔数字段（trades=None），首条为集合竞价撮合",
                }
                return _wrap(True, data, "腾讯行情（分笔明细，降级）",
                             data_time="%s %s" % (day, rows[-1]["time"]), degraded=True)
            errs.append("腾讯分笔：当日无明细")
        except Exception as exc:  # noqa: BLE001
            errs.append("腾讯分笔：%s" % exc)
        if market == "bj":
            errs.append("北交所（4/8 开头）实测无分笔数据：东财返回空明细，腾讯无返回")
        empty = _empty_ticks(d)
        empty["market"] = market
        empty["sideRule"] = EM_SIDE_RULE
        empty["note"] = "北交所无分笔数据；非交易日 / 未开盘时同样为空"
        return _wrap(False, empty, None, error="；".join(errs), degraded=True)

    try:
        return _cached(key, TTL_TICKS, build)
    except Exception as exc:  # noqa: BLE001
        empty = _empty_ticks(d)
        empty["market"] = market
        return _wrap(False, empty, None, error=str(exc), degraded=True)


# =========================================================================== #
# 对外接口 3：龙虎榜
# =========================================================================== #

def _empty_lhb(requested=None):
    """龙虎榜「空实现」形状"""
    return {
        "requestedDate": requested,
        "date": None,
        "fallback": False,       # 是否自动回溯到更早的交易日
        "fallbackDays": 0,
        "count": 0,
        "rows": [],
        "stocks": [],
        "topNetBuy": [],
        "topNetSell": [],
        "note": None,
    }


def _lhb_row(r):
    """东方财富 RPT_DAILYBILLBOARD_DETAILSNEW 单行 -> 统一字段（字段名均来自实测响应）"""
    sec = str(r.get("SECUCODE") or "")
    code = str(r.get("SECURITY_CODE") or "")
    try:
        market = _norm_code(code)[0]
    except ValueError:
        market = None
    return {
        "code": code,
        "name": r.get("SECURITY_NAME_ABBR"),
        "market": market,
        "secucode": sec,
        "tradeMarket": r.get("TRADE_MARKET"),
        "close": _num(r.get("CLOSE_PRICE")),
        "changePct": _num(r.get("CHANGE_RATE")),
        "turnoverRate": _num(r.get("TURNOVERRATE")),
        "netAmt": _num(r.get("BILLBOARD_NET_AMT")),      # 榜单净买额（元）
        "buyAmt": _num(r.get("BILLBOARD_BUY_AMT")),
        "sellAmt": _num(r.get("BILLBOARD_SELL_AMT")),
        "dealAmt": _num(r.get("BILLBOARD_DEAL_AMT")),    # 榜单成交额（元）
        "amount": _num(r.get("ACCUM_AMOUNT")),           # 当日总成交额（元）
        "netRatio": _num(r.get("DEAL_NET_RATIO")),       # 净买额占成交额比（%）
        "dealRatio": _num(r.get("DEAL_AMOUNT_RATIO")),   # 榜单成交额占比（%）
        "freeCap": _num(r.get("FREE_MARKET_CAP")),
        "reason": r.get("EXPLANATION"),                  # 上榜原因
        "explain": r.get("EXPLAIN"),                     # 席位特征说明（如「3家机构买入，成功率…」）
        "seatSum": {"buy": _num(r.get("SUM_BUY_AMT")), "sell": _num(r.get("SUM_SELL_AMT")),
                    "buySeat": _num(r.get("BUY_SEAT")), "sellSeat": _num(r.get("SELL_SEAT"))},
        "changeType": r.get("CHANGE_TYPE"),
        "performance": {
            "d1": _num(r.get("D1_CLOSE_ADJCHRATE")), "d2": _num(r.get("D2_CLOSE_ADJCHRATE")),
            "d5": _num(r.get("D5_CLOSE_ADJCHRATE")), "d10": _num(r.get("D10_CLOSE_ADJCHRATE")),
            "d20": _num(r.get("D20_CLOSE_ADJCHRATE")), "d30": _num(r.get("D30_CLOSE_ADJCHRATE")),
        },
    }


def _lhb_aggregate(rows):
    """龙虎榜逐条记录 -> (按代码聚合的股票列表, 净买额前 10, 净卖额前 10)

    说明：同一标的当日可能因多个「上榜原因」出现多行，各行金额口径不同，
          因此这里只做**结构聚合**（合并原因 / 记录索引），不对金额求和，避免编造口径。
    """
    agg = {}
    for i, row in enumerate(rows):
        item = agg.setdefault(row["code"], {
            "code": row["code"], "name": row["name"], "market": row["market"],
            "close": row["close"], "changePct": row["changePct"],
            "turnoverRate": row["turnoverRate"], "reasons": [], "rowIndexes": [],
            "tradeMarkets": [],
        })
        if row["reason"] and row["reason"] not in item["reasons"]:
            item["reasons"].append(row["reason"])
        if row["tradeMarket"] and row["tradeMarket"] not in item["tradeMarkets"]:
            item["tradeMarkets"].append(row["tradeMarket"])
        item["rowIndexes"].append(i)
    stocks = sorted(agg.values(), key=lambda x: x["code"])
    net_sorted = sorted([r for r in rows if r["netAmt"] is not None],
                        key=lambda x: x["netAmt"], reverse=True)
    return stocks, net_sorted[:10], list(reversed(net_sorted[-10:]))


def _em_lhb(date_str):
    """东方财富数据中心：按交易日抓取龙虎榜明细（分页）"""
    rows = []
    page = 1
    while page <= LHB_MAX_PAGES:
        url = ("%s?sortColumns=BILLBOARD_NET_AMT&sortTypes=-1&pageSize=%d&pageNumber=%d"
               "&reportName=RPT_DAILYBILLBOARD_DETAILSNEW&columns=ALL&source=WEB&client=WEB"
               "&filter=(TRADE_DATE%%3D%%27%s%%27)" % (EM_DATACENTER, LHB_PAGE_SIZE, page, date_str))
        r = _json(url, referer=REF_EM_DATA)
        res = r.get("result")
        if not res or not res.get("data"):
            if page == 1 and not r.get("success"):
                raise _LhbEmpty(r.get("message") or "返回数据为空", r.get("code"))
            break
        rows.extend(res["data"])
        pages = int(_num(res.get("pages")) or 1)
        if page >= pages:
            break
        page += 1
    return rows


class _LhbEmpty(RuntimeError):
    """龙虎榜当日无数据（东财返回 success=false / 9201），用于触发交易日回溯"""


def _recent_trading_day(start, lookback_days, probe):
    """从 start(YYYY-MM-DD) 起往前找最近一个有数据的交易日（跳过周末，probe 返回 True 表示有数据）"""
    cur = datetime.strptime(start, "%Y-%m-%d")
    for i in range(lookback_days + 1):
        day = cur - timedelta(days=i)
        if day.weekday() >= 5:          # 周六 / 周日直接跳过，减少无谓请求
            continue
        ds = day.strftime("%Y-%m-%d")
        try:
            if probe(ds):
                return ds, i
        except _LhbEmpty:
            continue
        except Exception:               # noqa: BLE001
            continue
    return None, None


def dragon_tiger(date=None):
    """龙虎榜（每日上榜明细）。

    数据源：东方财富数据中心 RPT_DAILYBILLBOARD_DETAILSNEW（实测可回溯多年）。
    入参：date 支持 'YYYY-MM-DD' / 'YYYYMMDD' / None（None = 最近一个有数据的交易日）。
    返回：统一信封；data.rows 一行 = 一个「上榜原因」，同一标的当日可能多行；
          data.stocks 按代码做结构聚合（只合并原因，不对金额求和，避免编造口径）。
    说明：当日龙虎榜通常盘后发布，盘中和周末会取到空 → 自动回溯最多 15 个自然日，
          并在 data.fallback / data.fallbackDays 中明确标注实际使用的交易日。
    """
    try:
        start = _norm_date(date)
    except ValueError as exc:
        return _wrap(False, _empty_lhb(str(date)), None, error=str(exc), degraded=True)
    key = "lhb:%s" % start

    def build():
        cache_rows = {}

        def has_data(ds):
            rows = _em_lhb(ds)
            cache_rows[ds] = rows
            return bool(rows)

        actual, back = _recent_trading_day(start, 15, has_data)
        if not actual:
            empty = _empty_lhb(start)
            empty["note"] = ("近 15 个自然日内东财均返回空（当日榜单通常盘后发布；"
                             "周末 / 非交易日无数据）")
            return _wrap(False, empty, "东方财富（数据中心·龙虎榜）",
                         error="未找到有数据的交易日", degraded=True)
        raw = cache_rows.get(actual) or _em_lhb(actual)
        rows = [_lhb_row(x) for x in raw]
        stocks, top_buy, top_sell = _lhb_aggregate(rows)
        data = {
            "requestedDate": start,
            "date": actual,
            "fallback": back > 0,
            "fallbackDays": back,
            "count": len(rows),
            "rows": rows,
            "stocks": stocks,
            "topNetBuy": top_buy,
            "topNetSell": top_sell,
            "note": ("rows 为逐条上榜记录（一行一个原因）；stocks 仅按代码合并原因、"
                     "不对金额求和；topNetBuy/topNetSell 基于逐条记录"),
        }
        return _wrap(True, data, "东方财富（数据中心·龙虎榜 RPT_DAILYBILLBOARD_DETAILSNEW）",
                     data_time=actual, degraded=back > 0)

    try:
        return _cached(key, TTL_LHB, build)
    except Exception as exc:  # noqa: BLE001
        return _wrap(False, _empty_lhb(start), None, error=str(exc), degraded=True)


# =========================================================================== #
# 对外接口 4：涨停梯队（含连板数）
# =========================================================================== #

def _empty_ladder(requested=None):
    """涨停梯队「空实现」形状"""
    return {
        "requestedDate": requested,
        "date": None,
        "fallback": False,
        "fallbackDays": 0,
        "count": 0,          # 涨停家数
        "maxLadder": 0,      # 最高连板数
        "ladders": [],       # 梯队（按连板数降序）
        "stocks": [],        # 全部涨停股
        "gaps": [],          # 断层：1..maxLadder 中缺失的连板层级
        "note": None,
    }


def _parse_zt_pool(pool):
    """涨停池 -> 统一结构（价格 p 实测为「元*1000」，需除以 1000）"""
    out = []
    for r in pool or []:
        code = str(r.get("c") or "")
        try:
            market = _norm_code(code)[0]
        except ValueError:
            market = None
        m = _num(r.get("m"))
        if market is None:
            market = "sh" if m == 1 else ("sz" if m == 0 else None)
        price_raw = _num(r.get("p"))
        stat = r.get("zttj") or {}
        out.append({
            "code": code,
            "name": r.get("n"),
            "market": market,
            "price": round(price_raw / 1000.0, 3) if price_raw is not None else None,
            "changePct": _num(r.get("zdp")),
            "ladder": int(_num(r.get("lbc")) or 0),          # 连板数
            "stat": {"days": int(_num(stat.get("days")) or 0), "ct": int(_num(stat.get("ct")) or 0)},
            "statText": "%d天%d板" % (int(_num(stat.get("days")) or 0),
                                      int(_num(stat.get("ct")) or 0)) if stat else None,
            "firstSealTime": _hhmmss(r.get("fbt")),           # 首次封板时间
            "lastSealTime": _hhmmss(r.get("lbt")),            # 最后封板时间
            "openTimes": int(_num(r.get("zbc")) or 0),        # 当日炸板次数
            "sealFund": _num(r.get("fund")),                  # 封板资金（元）
            "amount": _num(r.get("amount")),                  # 成交额（元）
            "turnoverRate": _num(r.get("hs")),                # 换手率（%）
            "floatCap": _num(r.get("ltsz")),                  # 流通市值（元）
            "totalCap": _num(r.get("tshare")),                # 总市值（元）
            "industry": r.get("hybk"),                        # 所属行业
        })
    out.sort(key=lambda x: (-x["ladder"], x["firstSealTime"] or "", x["code"]))
    return out


def _group_ladder(stocks):
    """按连板数分梯队（降序），并标记断层"""
    buckets = {}
    for s in stocks:
        buckets.setdefault(s["ladder"], []).append(s)
    ladders = [{"level": lv, "count": len(buckets[lv]), "stocks": buckets[lv]}
               for lv in sorted(buckets.keys(), reverse=True)]
    top = max(buckets.keys()) if buckets else 0
    gaps = [lv for lv in range(1, top + 1) if lv not in buckets]
    return ladders, top, gaps


def _em_zt_pool(date_str):
    """东方财富涨停池（date_str = YYYYMMDD）"""
    url = ("%s?ut=%s&dpt=wz.ztzt&Pageindex=0&pagesize=200&sort=fbt%%3Aasc&date=%s"
           % (EM_ZT_POOL, EM_ZT_UT, date_str))
    r = _json(url, referer=REF_EM)
    if _num(r.get("rc")) not in (0,):
        raise RuntimeError("涨停池接口返回 rc=%s" % r.get("rc"))
    d = r.get("data") or {}
    return d.get("pool") or [], d.get("tc")


def limit_up_ladder(date=None):
    """涨停梯队（含连板数）。

    数据源：东方财富涨停池 push2ex getTopicZTPool（实测仅提供最近约 20 个自然日，
            2026-08-26 起有数据，更早返回空）。
    入参：date 支持 'YYYY-MM-DD' / 'YYYYMMDD' / None（None = 最近一个有数据的交易日）。
    返回：统一信封；data.ladders 为按连板数降序的梯队，data.gaps 为缺失层级（断层）。
    字段：price 已按实测缩放（原始 p ÷ 1000）；ladder = lbc 连板数；stat = 涨停统计（n 天 m 板）。
    """
    try:
        start = _norm_date(date)
    except ValueError as exc:
        return _wrap(False, _empty_ladder(str(date)), None, error=str(exc), degraded=True)
    key = "zt:%s" % start

    def build():
        cache_pool = {}

        def has_data(ds):
            pool, _tc = _em_zt_pool(ds.replace("-", ""))
            cache_pool[ds] = pool
            return bool(pool)

        actual, back = _recent_trading_day(start, 20, has_data)
        if not actual:
            empty = _empty_ladder(start)
            empty["note"] = ("近 20 个自然日内涨停池均返回空：该接口实测只提供最近约 20 天数据"
                             "（更早交易日需改用其它数据源）")
            return _wrap(False, empty, "东方财富（涨停池 push2ex）",
                         error="未找到有数据的交易日（接口仅保留最近约 20 天）", degraded=True)
        pool = cache_pool.get(actual)
        if pool is None:
            pool, _tc = _em_zt_pool(actual.replace("-", ""))
        stocks = _parse_zt_pool(pool)
        ladders, top, gaps = _group_ladder(stocks)
        data = {
            "requestedDate": start,
            "date": actual,
            "fallback": back > 0,
            "fallbackDays": back,
            "count": len(stocks),
            "maxLadder": top,
            "ladders": ladders,
            "stocks": stocks,
            "gaps": gaps,
            "note": ("ladder=连板数（lbc），stat=涨停统计（n天m板）；gaps 为 1..maxLadder 中"
                     "没有个股的层级（梯队断层）；价格原始字段 p 实测为元×1000，已还原"),
        }
        return _wrap(True, data, "东方财富（涨停池 push2ex getTopicZTPool）",
                     data_time=actual, degraded=back > 0)

    try:
        return _cached(key, TTL_ZT, build)
    except Exception as exc:  # noqa: BLE001
        return _wrap(False, _empty_ladder(start), None, error=str(exc), degraded=True)
