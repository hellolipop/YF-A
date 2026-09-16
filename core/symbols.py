# -*- coding: utf-8 -*-
"""AlphaDesk · 标的名称识别（core/symbols.py）

要解决的实际问题：用户在 AI 选股里输入的往往不是代码 —— 可能是中文名（茅台 / 贵州
茅台）、简称（平安 / 宁德）、拼音首字母（GZMT / gzmt）、带交易所前缀的代码
（sh600519 / 000001.SZ）、或「代码:名称」的混合写法（600519:贵州茅台）。同时反过来
也存在：用户只输代码时，界面与记录里应该显示**真实名称**而不是一串数字。

三类结果必须分清（这是本模块的核心设计）
----------------------------------------
``code``       代码直判成功（不需要任何数据源）
``name``       名称/简称识别成功（唯一命中）
``ambiguous``  命中多个同档候选，**返回候选清单交给用户选**
``unknown``    本地与远端都没有命中

为什么要单独有 ``ambiguous`` 而不是「取搜索结果的第一个」：
「平安」同时命中 平安银行(000001) 与 中国平安(601318)，静默取第一个会把分析对象换成
另一只股票，而用户完全不会察觉 —— 这类静默改变对象的错误，比「识别失败」危险得多。
所以只有**唯一命中**才自动落定，多命中一律交回用户选择。

识别顺序：先本地后远端，先精确后模糊
------------------------------------
1. **代码直判**：``600519`` / ``sh600519`` / ``000001.SZ`` / ``AAPL`` —— 零成本；
2. **本地名录**：用全市场快照（code + name）建索引，做**名称精确 / 前缀 / 子串**与
   **代码精确 / 前缀**匹配。快、可离线、不消耗第三方配额，覆盖绝大多数中文输入；
3. **远端搜索**：本地无命中时才打搜索接口（东财 suggest），覆盖拼音首字母、全拼与
   错别字 —— 这些需要拼音表或模糊匹配能力，项目零依赖不便自带，交给远端更可靠。

拼音为什么不在本地做：汉字→拼音需要一个覆盖数千字的拼音表，项目坚持零第三方依赖，
自建表的维护成本与出错面都太大（而且股票名里还有多音字）。远端搜索接口本身支持拼音，
因此拼音输入走第 3 步；这条边界在模块与 README 里都写明了，避免用户以为是本地能力。

一个刻意的取舍：**只输代码时不去远端取名称**。因为 ``core/advisor.py`` 在取报价时
本来就会拿到名称（行情源返回），从报价回填是免费的；识别层只负责在本地名录里顺手查
一下（命中就带上，没命中留空由上层回填），避免为了显示名称多打一轮请求。
"""

from __future__ import annotations

import re

__all__ = [
    "parse_token", "normalize_text", "build_index", "match_local", "resolve",
    "MARKET_CN", "MARKET_US", "KIND_CODE", "KIND_NAME", "KIND_AMBIGUOUS", "KIND_UNKNOWN",
    "TIER_LABEL", "MAX_TOKENS",
]

MARKET_CN = "cn"
MARKET_US = "us"

KIND_CODE = "code"
KIND_NAME = "name"
KIND_AMBIGUOUS = "ambiguous"
KIND_UNKNOWN = "unknown"

#: 单次解析的标的数上限（与 AI 选股接口、推送通道保持同一口径）
MAX_TOKENS = 60

#: 匹配档位 → 分值（分值只用于排序与「同档即歧义」判断，不代表概率）
TIER_EXACT = 100          # 名称完全相同 / 代码完全相同
TIER_PREFIX = 80          # 名称以查询开头
TIER_SUBSTR = 60          # 名称包含查询
TIER_CODE_PREFIX = 50     # 代码以查询开头
TIER_LABEL = {
    TIER_EXACT: "完全匹配",
    TIER_PREFIX: "名称前缀",
    TIER_SUBSTR: "名称包含",
    TIER_CODE_PREFIX: "代码前缀",
}

_CODE_CN = re.compile(r"^\d{6}$")
_CODE_CN_DOT = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")
_CODE_CN_PREFIX = re.compile(r"^(SH|SZ|BJ)\.?(\d{6})$")
_CODE_US = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_CODE_US_DOT = re.compile(r"^([A-Z][A-Z0-9.\-]{0,9})\.(US|O|N)$")
#: 名称里可以忽略的字符（空白、全角空格、·、-）
_NOISE = re.compile(r"[\s\u3000·・\-_*]+")

#: 全角 → 半角（数字、字母、常见标点）：中文输入法下极容易打出全角
_FULLWIDTH = {i: i - 0xFEE0 for i in range(0xFF01, 0xFF5F)}
_FULLWIDTH[0x3000] = 0x20


def normalize_text(value):
    """归一化：全角转半角 → 去噪（空格/中点/连字符）→ 大写。

    股票代码与名称在中文输入法下会出现全角字符（``６００５１９``）与全角冒号，
    不归一化就会出现「明明输对了却识别不出来」这种最难向用户解释的失败。
    """
    text = str(value if value is not None else "")
    text = text.translate(_FULLWIDTH)
    text = _NOISE.sub("", text)
    return text.strip().upper()


def parse_token(raw, market=MARKET_CN):
    """把单个输入解析成 ``{code, market, name, source}``；不像代码时返回 None。

    支持的写法（与前端 ``web/js/views/advisor.js`` 的 ``parseToken`` 保持同一规则集，
    两处必须一致，否则同一个输入在「提交」与「识别」两条路径上会得到不同结果）：

    ========================  ==========================================
    ``600519`` / ``sh600519``  A股：6 位数字，可带 SH/SZ/BJ 前缀或后缀
    ``000001.SZ``              A股：``代码.交易所``
    ``AAPL`` / ``BRK.B``       美股：字母开头，允许 ``.`` 与 ``-``
    ``600519:贵州茅台``        附带名称（名称不参与上游请求，只用于展示）
    ``nvda:US``               美股带市场后缀
    ========================  ==========================================

    刻意**不**把任意中文串当代码：中文输入一律交给名称识别，避免把「平安」这类
    词误当成代码去请求上游。
    """
    text = str(raw if raw is not None else "").strip()
    if not text:
        return None
    # 先切「代码:名称」——名称里也可能有冒号，因此只按第一个冒号切一次。
    # 分隔符必须包含**全角空格 U+3000**：中文输入法下「贵州茅台　600519」是常见写法，
    # 而 normalize_text 会先把它转成半角空格、紧接着当噪声删掉，于是两个 token 粘在
    # 一起，既不像代码也命中不了名录（实测踩到的「明明输对了却识别不出来」）
    name = ""
    body = text
    for sep in (":", "：", " ", "\u3000"):
        if sep in text:
            head, _, tail = text.partition(sep)
            head_n, tail_n = normalize_text(head), normalize_text(tail)
            if head_n and not _looks_like_code(head_n) and _looks_like_code(tail_n):
                # ``贵州茅台 600519`` 这种「名称在前」的写法
                body, name = tail.strip(), head.strip()
            else:
                body, name = head.strip(), tail.strip()
            # 市场后缀不是显示名称：``nvda:US`` 里的 ``US`` 是市场标记，
            # 若当成 name 会被上层当作「调用方明确给出的名称」而永久显示成「名称：US」
            if name and _is_market_suffix(name):
                body = body or head.strip()
                name = ""
            break

    norm = _light_norm(body)
    if not norm:
        return None

    m = _CODE_CN_DOT.match(norm)
    if m:
        return {"code": m.group(1), "market": MARKET_CN, "name": name, "source": "code"}
    m = _CODE_CN_PREFIX.match(norm)
    if m:
        return {"code": m.group(2), "market": MARKET_CN, "name": name, "source": "code"}
    if _CODE_CN.match(norm):
        return {"code": norm, "market": MARKET_CN, "name": name, "source": "code"}
    m = _CODE_US_DOT.match(norm)
    if m:
        return {"code": m.group(1), "market": MARKET_US, "name": name, "source": "code"}
    if _CODE_US.match(norm) and not _has_cjk(norm):
        return {"code": norm, "market": MARKET_US, "name": name, "source": "code"}
    return None


#: 交易所后缀（`600519:SH` / `nvda:US`）：这些是市场标记，不是显示名称
_MARKET_SUFFIX = {"US", "O", "N", "SH", "SZ", "BJ", "CN", "HK"}


def _is_market_suffix(text):
    return normalize_text(text) in _MARKET_SUFFIX


def _has_cjk(text):
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(text or ""))


def _light_norm(text):
    """代码解析用的轻量归一：处理全角、空白与大写，**保留 ``.`` 与 ``-``**。

    ``normalize_text`` 会删掉 ``-``（对名称是好事），但美股类别股代码 ``BRK-B`` 靠它
    区分，所以代码正则必须跑在轻量归一后的串上，否则 ``_CODE_US`` 里的 ``-`` 永远不可达。
    """
    return str(text if text is not None else "").translate(_FULLWIDTH).strip().upper()


def _looks_like_code(text):
    return bool(_CODE_CN.match(text) or _CODE_US.match(text)) and not _has_cjk(text)


def build_index(rows, market=MARKET_CN):
    """用地全市场快照（或任意含 code/name 的行）建名称索引。

    ``rows`` 来自服务端的市场快照（code + name），约 5000+ 条 A 股；
    索引只保留归一化后的名称、原始名称与代码，内存开销可忽略。
    子串匹配是对整张表线性扫描（5000 条 × 每次查询）—— 微秒级，不需要额外倒排结构，
    换来的是「随便输名称的一部分都能命中」这种对用户最直观的行为。
    """
    items = []
    by_code = {}
    by_name = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        code = normalize_text(row.get("code"))
        name = str(row.get("name") or "").strip()
        if not code or not name:
            continue
        key = normalize_text(name)
        items.append({"code": code, "name": name, "key": key, "market": market})
        by_code[code] = name
        by_name.setdefault(key, []).append(code)
    return {"items": items, "byCode": by_code, "byName": by_name,
            "market": market, "count": len(items)}


def match_local(query, index, limit=5):
    """在本地名录里匹配，返回按（档位降序, 名称长度升序, 代码升序）排序的候选。

    排序规则刻意做成**完全确定**的：同档位时名称短的优先（「平安银行」比
    「中国平安银行…」更可能是用户想要的），再按代码升序兜底，因此同样的输入在任何
    时候都得到同样的顺序，歧义候选列表不会随机跳动。
    """
    q = normalize_text(query)
    if not q or not isinstance(index, dict):
        return []
    items = index.get("items") or []
    out = []
    for it in items:
        key = it.get("key") or ""
        code = it.get("code") or ""
        score = 0
        if key == q:
            score = TIER_EXACT
        elif code == q:
            score = TIER_EXACT
        elif len(q) >= 2 and key.startswith(q):
            # 前缀也必须至少两个字：单字命中（「中」→ 中国平安）虽然只有一个候选，
            # 但那不是「识别成功」而是「碰巧」；自动落定等于替用户换了股票
            score = TIER_PREFIX
        elif len(q) >= 2 and q in key:
            score = TIER_SUBSTR
        elif len(q) >= 2 and code.startswith(q):
            score = TIER_CODE_PREFIX
        if score:
            out.append({"code": code, "name": it.get("name"), "key": key,
                        "market": it.get("market"), "score": score,
                        "tier": TIER_LABEL.get(score, "")})
    out.sort(key=lambda x: (-x["score"], len(x["key"]), x["code"]))
    return out[:max(1, int(limit or 5))]


def _hit(item, source="local"):
    return {"code": item.get("code"), "name": item.get("name"),
            "market": item.get("market"), "tier": item.get("tier"),
            "score": item.get("score"), "source": source}


def _remote_hits(query, search_fn, market, limit=5):
    """远端搜索（东财 suggest）：拼音 / 全拼 / 错别字 / 非常用简称都靠它。"""
    if not callable(search_fn):
        return []
    try:
        res = search_fn(query, market)
    except Exception:  # noqa: BLE001  远端故障不应让整批识别失败
        return []
    rows = (res or {}).get("rows") if isinstance(res, dict) else res
    # 形状校验必须在切片之前：曾对 int / str / None 直接切片，异常会冒出 resolve 之外，
    # 违背「任何输入都不抛异常」的承诺（搜索接口换成别的实现时很容易踩到）
    if not isinstance(rows, (list, tuple)):
        return []
    out = []
    cap = max(1, int(limit or 5))
    for row in rows:
        # 先过滤脏行再计数：否则头部混入脏行会吃掉 limit 配额，把靠后的有效候选挤掉
        if len(out) >= cap:
            break
        if not isinstance(row, dict) or not row.get("code"):
            continue
        code = normalize_text(row.get("code"))
        name = str(row.get("name") or "").strip()
        out.append({"code": code, "name": name,
                    "market": row.get("market") or market,
                    "tier": "远端搜索", "score": 0, "source": "remote",
                    "classify": row.get("classify"), "type": row.get("type"),
                    # 「回显」= 远端把输入原样当成代码返回（名称与代码相同）。
                    # 搜索接口对「像代码但没收录」的输入就是这么兜底的，它只说明
                    # 「这个串可能是代码」，不构成「找到了这只股票」的证据。
                    "echo": bool(code and normalize_text(name) == code)})
    return out


def _resolve_by_name(text, index, search_fn, mkt, limit=5):
    """名称识别：本地名录 → 远端搜索。

    返回 ``(kind, code, market, name, hits, note, guess)``；``guess`` 表示这是
    「按代码处理」的猜测结果（远端只回显、没有真实名称），界面必须把它与确定命中区分开。

    纯字母串会先走这里（见 resolve 的说明）：``gzmt`` 这类拼音首字母与美股代码长得
    一模一样，只有「先当名称找、找不到才当代码」才能既不误判拼音、又不丢美股代码。
    """
    local = match_local(text, index, limit=limit) if index else []
    exact = [h for h in local if h["score"] == TIER_EXACT]
    if len(exact) == 1:
        top = exact[0]
        return (KIND_NAME, top["code"], top["market"], top["name"],
                [_hit(h) for h in local], "名称完全匹配", False)
    if len(local) == 1:
        top = local[0]
        return (KIND_NAME, top["code"], top["market"], top["name"],
                [_hit(h) for h in local],
                "本地名录唯一命中（%s）" % (top.get("tier") or ""), False)
    if len(local) > 1:
        return (KIND_AMBIGUOUS, None, mkt, None, [_hit(h) for h in local],
                "命中 %d 个候选（「%s」不是唯一名称），请选择或改用代码" % (len(local), text), False)

    remote = _remote_hits(text, search_fn, mkt, limit=limit)
    real = [h for h in remote if not h.get("echo")]
    if len(real) == 1:
        top = real[0]
        return (KIND_NAME, top["code"], top["market"], top["name"], remote, "远端搜索唯一命中", False)
    if len(real) > 1:
        first = real[0]
        # 只看首位不够：远端可能返回一串「同类但不同标的」（例如搜「银行」），
        # 这种同族候选必须交回用户，不能替用户挑一个
        same_family = [h for h in real[1:] if _same_family(first.get("name"), h.get("name"))]
        if not same_family:
            return (KIND_NAME, first["code"], first["market"], first["name"], remote,
                    "远端搜索首选（另有 %d 个候选可改选）" % (len(real) - 1), False)
        return (KIND_AMBIGUOUS, None, mkt, None, remote,
                "远端命中多个相近名称，请选择或改用代码", False)
    if remote:
        # 远端只回显（没有真实名称）：按代码处理，但必须标 guess ——
        # 「接口回显」不等于「这只股票存在」，界面要把它与确定命中区分开
        top = remote[0]
        return (KIND_CODE, top["code"], top["market"], "",
                remote, "远端仅回显输入、没有真实名称，按代码处理（可能不存在）", True)
    return (None, None, mkt, None, [], "", False)


def _same_family(a, b):
    """两个名称是否属于「同一族」（前两个字相同，如 平安银行 / 中国平安 不算同族，
    但 宁德时代 / 宁德新能源 算）—— 用于判断远端候选能否自动选第一个。"""
    a, b = str(a or ""), str(b or "")
    if len(a) < 2 or len(b) < 2:
        return False
    return a[:2] == b[:2]


def resolve(tokens, market=MARKET_CN, index=None, search_fn=None, limit=5,
            max_tokens=MAX_TOKENS):
    """批量识别：``tokens`` 可以是字符串列表，也可以是 ``{code}/{raw}`` 字典列表。

    返回 ``{"items": [...], "summary": {...}, "index": {...}}``。每个 item 的
    ``kind`` 是 ``code`` / ``name`` / ``ambiguous`` / ``unknown`` 之一；
    ``ambiguous`` 带 ``hits``（候选清单）与 ``note``（为什么不能自动定），
    ``unknown`` 带 ``note`` 说明试过哪些途径 —— 让界面能如实告诉用户
    「为什么没识别出来」，而不是只丢一句「未识别的输入」。

    **纯字母串在 A 股语境下先按名称识别**：``gzmt``（拼音首字母）与美股代码长得
    完全一样，先当代码会让「输拼音查茅台」永远得到一只不存在的美股；而先当名称、
    找不到再当代码，两种意图都能满足 —— 因为远端搜索对拼音与美股代码都能命中。

    任何输入都不抛异常：单条失败只影响它自己，其余照常识别。
    """
    mkt = MARKET_US if str(market or "").strip().lower().startswith("us") else MARKET_CN
    raw_list = list(tokens or [])
    cap = int(max_tokens or MAX_TOKENS)
    truncated = len(raw_list) > cap
    raw_list = raw_list[:cap]

    items = []
    seen = set()
    for raw in raw_list:
        if isinstance(raw, dict):
            text = str(raw.get("raw") or raw.get("code") or raw.get("symbol") or "").strip()
            incoming = {"code": normalize_text(raw.get("code")),
                        "market": str(raw.get("market") or mkt).lower(),
                        "name": str(raw.get("name") or "").strip()}
            incoming["market"] = (MARKET_US if incoming["market"].startswith("us")
                                  else MARKET_CN)
        else:
            text = str(raw or "").strip()
            incoming = None
        if not text and not (incoming and incoming.get("code")):
            continue

        parsed = parse_token(text, mkt) if text else None
        norm = normalize_text(text)
        letter_like = bool(norm and _CODE_US.match(norm) and not _has_cjk(norm))
        # A股语境 + 纯字母：名称优先；其余情况（数字代码、带前缀代码、美股语境）代码优先
        code_first = not (letter_like and mkt == MARKET_CN)

        # 只有「真的带代码」的字典才算代码来源：`{"raw": "贵州茅台"}` 这种没有 code
        # 的输入若被当成代码，会产出一条 code='' 的「已识别」结果，前端带着空代码去查
        # 一只不存在的标的（实测踩到过）
        src = parsed or ((incoming or {}) if (incoming or {}).get("code") else None)
        result = None
        guessed = False    # 「名称与远端都没命中、只能按代码处理」的猜测标记
        if code_first and src:
            code = src["code"]
            item_market = src.get("market") or mkt
            name = (parsed or {}).get("name") or (incoming or {}).get("name") or ""
            if not name and isinstance(index, dict):
                name = (index.get("byCode") or {}).get(code, "")
            result = (KIND_CODE, code, item_market, name, [],
                      "识别为代码" + ("，名称已由本地名录补全" if name
                                     else "；名称待行情返回后回填"))
        else:
            kind, code, item_market, name, hits, note, guessed = _resolve_by_name(
                text, index, search_fn, mkt, limit=limit)
            if kind:
                result = (kind, code, item_market, name, hits, note)
            elif src:
                # 名称路径全落空，但它确实长得像代码（如 A 股语境下的 AAPL 或某个冷门
                # 美股代码）。这里**不拦**：东财 suggest 收录不全，直接判「未识别」会把
                # 合法但冷门的代码也挡掉。但必须打上 guess 标记 —— 让界面把「猜的」
                # 和「确定的」区分开，用户才不会误以为拼错的代码一定有效。
                code = src["code"]
                item_market = src.get("market") or mkt
                name = (index.get("byCode") or {}).get(code, "") if isinstance(index, dict) else ""
                result = (KIND_CODE, code, item_market, name, [],
                          "名称与远端搜索都没有命中，按代码处理"
                          + ("，名称已由本地名录补全" if name else "")
                          + "（若为拼写错误请检查）")
                guessed = True
            else:
                result = (KIND_UNKNOWN, None, mkt, None, [],
                          "本地名录与远端搜索都没有命中；请改用代码，或检查名称是否输入有误")

        kind, code, item_market, name, hits, note = result
        if code:
            key = item_market + ":" + code
            if key in seen:
                continue
            seen.add(key)
        items.append({"raw": text or code, "kind": kind, "code": code,
                      "market": item_market, "name": name, "hits": hits, "note": note,
                      "guess": bool(guessed)})

    summary = {
        "total": len(items),
        "resolved": len([i for i in items if i["kind"] in (KIND_CODE, KIND_NAME)]),
        "code": len([i for i in items if i["kind"] == KIND_CODE]),
        "name": len([i for i in items if i["kind"] == KIND_NAME]),
        "ambiguous": len([i for i in items if i["kind"] == KIND_AMBIGUOUS]),
        "unknown": len([i for i in items if i["kind"] == KIND_UNKNOWN]),
        "named": len([i for i in items if i.get("name")]),
        "guessed": len([i for i in items if i.get("guess")]),
        "indexed": int((index or {}).get("count") or 0),
        "truncated": truncated,
    }
    return {"items": items, "summary": summary,
            "index": {"market": (index or {}).get("market") or mkt,
                      "count": int((index or {}).get("count") or 0)},
            "note": ("识别顺序：代码直判 → 本地全市场名录（名称精确/前缀/子串、代码前缀）"
                     "→ 远端搜索（拼音、全拼、错别字）。多命中的一律返回候选而不自动选定，"
                     "避免静默把分析对象换成另一只股票。")}
