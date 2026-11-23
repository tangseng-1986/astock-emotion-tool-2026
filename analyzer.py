# -*- coding: utf-8 -*-
"""分析层：T-1待选池 -> T日盘中观察池、趋势过滤、情绪指标、弱转强/A杀信号、可调权重评分。

时序口径（核心）：
  观察日 T   = 我们要观察的"今天"（盘中实时 or 某收盘日）
  待选池     = T-1 交易日的涨停 + 跌停个股（剔除 ST）
  观察池     = 待选池经近3月趋势过滤后留存的个股，叠加 T 日盘中实时状态
  弱转强信号 = T-1 跌停 且 T 日盘中反手涨停 -> 情绪转强买入信号（纳入自选池）
  A杀信号    = T-1 涨停 且 T 日盘中跌停       -> 情绪转弱风险（不纳入自选池）
"""
import numpy as np
import pandas as pd

import config as C
import data_fetcher as D


# ============ 趋势判定 ============
def _slope_pct(close: pd.Series, window: int) -> float:
    y = close.tail(window).values.astype(float)
    if len(y) < 2 or y[0] == 0:
        return 0.0
    x = np.arange(len(y), dtype=float)
    slope = np.polyfit(x, y, 1)[0]
    return float(slope * window / y[0])


def classify_trend(kline: pd.DataFrame, append_pct: float = None) -> dict:
    """输入前复权日线，输出趋势标签、短期动能与连续大跌标记。

    append_pct：当K线尚未更新到待选池来源日（盘后行情源延迟/缓存滞后）时，
      用涨跌停池的当日真实涨跌幅补一根收盘价，再计算连续大跌/窗口累计/趋势。
      否则会漏判"18日跌停+19日跌停"这类连续跌停（K线只到18日时只算1天）。
    """
    res = {"trend": "数据不足", "slope60": None, "ma20": None, "ma60": None,
           "high3m": None, "drawdown": None, "price": None, "bars": 0,
           "ret5": None, "连续大跌": False, "连跌天数": 0, "大跌次数5日": 0,
           "窗口累计跌幅": None, "破位大跌": False}
    if kline is None or len(kline) < C.TREND_MIN_BARS:
        return res
    close = kline["收盘"].astype(float).reset_index(drop=True)
    high = kline["最高"].astype(float).reset_index(drop=True)
    if append_pct is not None and np.isfinite(float(append_pct)):
        last = float(close.iloc[-1])
        new_close = last * (1.0 + float(append_pct) / 100.0)
        if new_close > 0:
            close = pd.concat([close, pd.Series([new_close])], ignore_index=True)
            high = pd.concat([high, pd.Series([max(new_close, float(high.iloc[-1]))])],
                             ignore_index=True)
    price = float(close.iloc[-1])
    ma20 = float(close.tail(C.MA_SHORT).mean())
    ma60 = float(close.tail(C.MA_MID).mean())
    high3m = float(high.tail(C.MA_MID).max())
    slope60 = _slope_pct(close, C.MA_MID)
    drawdown = (high3m - price) / high3m if high3m > 0 else 0.0
    # 日涨跌幅（用于短期动能 / 连续大跌判定）
    pct = close.pct_change() * 100
    ret5 = float(close.iloc[-1] / close.iloc[-1 - C.MOMENTUM_LOOKBACK] - 1) * 100 \
        if len(close) > C.MOMENTUM_LOOKBACK else 0.0
    # 窗口内大跌统计（近 DROP_WINDOW_DAYS 个交易日）
    win_pct = pct.tail(C.DROP_WINDOW_DAYS).fillna(0)
    big = win_pct <= -C.BIG_DROP_PCT
    n_big = int(big.sum())
    # 复合累计跌幅：用复利口径 sum(log1p(pct/100))，避免简单相加误差
    cum = float(np.expm1(np.log1p(win_pct / 100.0).sum()) * 100) if len(win_pct) else 0.0
    # 当前连续大跌天数（从最新一根K线往回数）
    streak = 0
    for v in pct.iloc[::-1]:
        if pd.notna(v) and v <= -C.BIG_DROP_PCT:
            streak += 1
        else:
            break
    consec = bool(streak >= C.CONSEC_DROP_DAYS)
    # 破位大跌：窗口内大跌次数达标 且 累计跌幅达标（捕捉"跌停-反弹-再跌停"的洗盘破位）
    breakdown = bool(n_big >= C.WINDOW_DROP_COUNT and cum <= C.WINDOW_CUM_DROP_PCT)
    res.update(price=round(price, 2), ma20=round(ma20, 2), ma60=round(ma60, 2),
               high3m=round(high3m, 2), slope60=round(slope60, 4),
               drawdown=round(drawdown, 4), bars=len(kline),
               ret5=round(ret5, 2), 连续大跌=consec,
               连跌天数=streak, 大跌次数5日=n_big,
               窗口累计跌幅=round(cum, 2), 破位大跌=breakdown)

    at_high = price >= high3m * 0.90
    # 下跌趋势需"斜率走弱 且 价格处于均线下方"同时满足；
    # 仅凭短暂跌破MA60（斜率仍强正）判下跌会误杀强势高位股
    is_down = (slope60 <= C.DOWN_SLOPE_PCT and price < ma60 * C.DOWN_PRICE_BELOW_MA60) and not at_high
    is_up = (slope60 > C.UP_SLOPE_PCT and price >= ma60 * C.UP_PRICE_ABOVE_MA60
             and ma20 >= ma60 * C.UP_MA20_ABOVE_MA60)
    is_high = (price >= high3m * C.HIGH_ZONE_PCT and price >= ma60 * C.HIGH_MA60_FLOOR
               and drawdown <= C.HIGH_MAX_DRAWDOWN)

    if at_high and (is_up or is_high):
        res["trend"] = "上升趋势" if is_up else "高位震荡"
    elif is_down:
        res["trend"] = "下跌趋势"
    elif is_up:
        res["trend"] = "上升趋势"
    elif is_high:
        res["trend"] = "高位震荡"
    else:
        res["trend"] = "震荡偏弱"
    return res


# ============ 破位大跌判定（公共，供分析/回测/盘中共用） ============
def is_breakdown(info: dict) -> bool:
    """短期破位判定：连续N日大跌，或窗口内多次大跌且累计跌幅超阈值。
    info 需含 连续大跌 / 破位大跌 字段；回测场景可只算连续大跌。"""
    return bool(info.get("连续大跌") or info.get("破位大跌"))


# ============ 可调权重：综合评分 ============
def _clip01(x):
    return float(np.clip(x, 0, 1))


def composite_score(c: dict, weights: dict = None) -> float:
    """按权重计算个股综合评分(0-100)。五因子归一化：
      trend          : 60日斜率映射 + 均线多头
      position       : 现价相对3月高点的位置
      limit_quality  : 连板高度 / 封板质量 / 炸板次数
      theme          : 涨停原因题材数量
      short_momentum : 近5日收益；连续大跌/多次大跌显著惩罚（短期破位风险）
    """
    w = {**C.WEIGHTS, **(weights or {})}
    total_w = sum(w.values()) or 1

    slope = c.get("slope60")
    trend_s = _clip01(((slope or 0) + 0.15) / 0.45) if slope is not None else 0.4
    ma20, ma60, price = c.get("ma20"), c.get("ma60"), c.get("price")
    if ma20 and ma60:
        trend_s = 0.6 * trend_s + 0.4 * _clip01((ma20 / ma60 - 0.9) / 0.2)

    dd = c.get("drawdown")
    pos_s = _clip01(1 - (dd if dd is not None else 0.5) / 0.3)

    board = c.get("连板数", 1) or 1
    limit_s = _clip01((board - 1) / 4)
    if c.get("炸板次数") is not None:
        limit_s = 0.7 * limit_s + 0.3 * _clip01(1 - c["炸板次数"] / 3)

    reason = c.get("原因标注", "") or ""
    theme_s = _clip01(len([x for x in ["+", "＋"] if x in reason]) / 3)

    ret5 = c.get("ret5")
    mom_s = _clip01(((ret5 if ret5 is not None else 0) - C.MOM_LOSS) / (C.MOM_GAIN - C.MOM_LOSS))
    if is_breakdown(c):
        mom_s *= C.CONSEC_DROP_PENALTY
    elif c.get("连续大跌") or c.get("大跌次数5日", 0) >= 2:
        mom_s *= 0.75

    raw = (w.get("trend", 0) * trend_s + w.get("position", 0) * pos_s +
           w.get("limit_quality", 0) * limit_s + w.get("theme", 0) * theme_s +
           w.get("short_momentum", 0) * mom_s)
    return round(raw / total_w * 100, 1)


# ============ 原因标注 ============
def limit_reason(row: pd.Series, kind: str, reason_info: dict) -> str:
    code = str(row.get("代码", ""))
    info = reason_info.get(code, {})
    if kind == "涨停":
        r = info.get("reason")
        if r and r != "—":
            return f"{r}｜连板:{row.get('连板数', 1)}（同花顺,需核实）"
        return f"封板资金{float(row.get('封板资金', 0))/1e8:.2f}亿｜连板:{row.get('连板数', 1)}｜炸板{row.get('炸板次数', 0)}次"
    parts = [f"连续跌停{int(row.get('连续跌停', 1))}天",
             f"开板{int(row.get('开板次数', 0))}次"]
    pe = row.get("动态市盈率")
    if pe is not None and np.isfinite(pe) and 0 < pe < 1000:
        parts.append(f"动态PE{float(pe):.1f}")
    return " / ".join(parts) + "（结构化字段推断,需核实）"


# ============ 候选池去重 ============
def _dedupe_candidates(items: list) -> list:
    """同一代码只保留一条候选记录（数据源偶尔返回同码多行，且元数据可能不同）。

    保留规则（依次比较）：
      1) 涨跌幅方向与"涨停/跌停"类型一致；
      2) 连板数/连续跌停天数更大（信息更完整，如3连板优先于1连板）；
      3) 涨跌幅绝对值更大；
      4) 成交额更大。
    顺序仍按首次出现位置，避免影响看板排序习惯。
    """
    best, order = {}, []
    for c in items:
        code = c["代码"]
        if code not in best:
            best[code] = c
            order.append(code)
            continue

        def rank(x):
            pct = float(x.get("涨跌幅") or 0)
            typed = 1 if ((x["类型"] == "涨停" and pct > 0) or
                          (x["类型"] == "跌停" and pct < 0)) else 0
            return (typed, int(x.get("连板数") or 1), abs(pct), float(x.get("成交额") or 0))

        if rank(c) > rank(best[code]):
            best[code] = c
    return [best[code] for code in order]


# ============ T-1待选池 -> T日观察池 ============
def build_pools(obs_date: str, mode: str = "close", weights: dict = None,
                forward: bool = False) -> dict:
    """构建观察池。
    forward=False（回顾池）：待选池 = T-1 涨跌停，叠加 T 日状态。
        用于：弱转强/A杀信号判定（T-1→T 状态转移）+ 观察池跟踪表。
    forward=True（前瞻池）：待选池 = T 日（观察日当日）收盘涨跌停，趋势过滤后供 T+1 观察。
        收盘复盘的主池：待选池全量表、分类统计、连板梯队、情绪温度均以此为准。
    """
    if forward:
        base_date = D.normalize_date(obs_date)  # 待选池 = 当日收盘涨跌停
    else:
        base_date = D.prev_trade_date(obs_date, C.BASE_DAY_OFFSET)  # T-1
    next_date = D.next_trade_date(obs_date)


    # 待选池来源：T-1 涨跌停
    up = D.limit_up_pool(base_date)
    down = D.limit_down_pool(base_date)
    reasons = D.ths_reasons(base_date)

    candidates = []
    for kind, df in (("涨停", up), ("跌停", down)):
        if df.empty:
            continue
        for _, row in df.loc[~df["_excluded"]].iterrows():
            candidates.append({
                "代码": row["代码"], "名称": row["名称"], "类型": kind,
                "来源日": base_date,
                "涨跌幅": round(float(row["涨跌幅"]), 2),
                "最新价": float(row["最新价"]),
                "所属行业": row.get("所属行业", "—"),
                "换手率": round(float(row.get("换手率", 0)), 2),
                "成交额": float(row.get("成交额", 0)),
                "连板数": int(row.get("连板数", 1)) if kind == "涨停" else int(row.get("连续跌停", 1)),
                "炸板次数": int(row.get("炸板次数", 0)) if kind == "涨停" else None,
                "封板资金": float(row.get("封板资金", 0)) if kind == "涨停" else 0,
                "原因标注": limit_reason(row, kind, reasons),
                "首次封板时间": str(row.get("首次封板时间", row.get("最后封板时间", "—"))),
            })

    # 数据源同码多行去重：避免观察池/待选池出现重复个股、分类统计重复计数
    candidates = _dedupe_candidates(candidates)

    # 趋势过滤 + 短期破位过滤：用截至 T 的K线
    # 注意：腾讯/东财K线盘后可能尚未更新到观察日，磁盘缓存也可能滞后。
    # 待选池本身来自当日真实涨跌停池，其「涨跌幅 / 连续跌停天数」是权威数据，
    # 用它补齐趋势序列，杜绝「连续两日跌停却只算1天」这类漏判（凯撒文化类）。
    klines = D.daily_kline_batch([c["代码"] for c in candidates], obs_date)
    for c in candidates:
        k = klines.get(c["代码"])
        append_pct = None
        if k is not None and len(k) and "日期" in k.columns:
            try:
                last_kd = D.normalize_date(str(k["日期"].iloc[-1]))
            except Exception:
                last_kd = ""
            if last_kd and last_kd < base_date:
                append_pct = c["涨跌幅"]        # 涨跌停池当日真实涨跌幅（如 -10.02）
        tr = classify_trend(k, append_pct=append_pct)
        c.update(tr)
        # 权威兜底：涨跌停池的「连续跌停天数」直接判定，不依赖K线是否更新。
        # 跌停股的 连板数 字段即「连续跌停」（见候选构造）。
        if c["类型"] == "跌停":
            streak_pool = int(c.get("连板数", 1) or 1)
            if streak_pool >= C.CONSEC_DROP_DAYS:
                c["连续大跌"] = True
                c["连跌天数"] = max(int(c.get("连跌天数", 0) or 0), streak_pool)
                c["连续跌停权威"] = True
        # 洗盘前趋势：剔除最近2根K线（T日反包 + T-1跌停）后的趋势，
        # 用于弱转强分级——判定"洗盘前处于什么趋势"比反包后的趋势更有意义
        if k is not None and len(k) >= C.TREND_MIN_BARS + C.CONSEC_DROP_DAYS:
            c["前期趋势"] = classify_trend(k.iloc[:-C.CONSEC_DROP_DAYS])["trend"]
        else:
            c["前期趋势"] = tr["trend"]
        c["综合评分"] = composite_score(c, weights)
        excl = []
        if c["trend"] not in ("上升趋势", "高位震荡"):
            excl.append("趋势不符")
        if is_breakdown(c):
            if c.get("连续跌停权威"):
                excl.append(f"连续{c.get('连跌天数', 0)}日跌停(涨跌停池)")
            elif c.get("连续大跌"):
                excl.append(f"连续{c.get('连跌天数', 0)}日大跌")
            if c.get("破位大跌"):
                excl.append(f"窗口{c.get('大跌次数5日', 0)}次大跌/累计{c.get('窗口累计跌幅')}%")
        c["剔除原因"] = "、".join(excl)

    watch = [c for c in candidates if c["trend"] in ("上升趋势", "高位震荡")
             and not is_breakdown(c)]

    # 按涨停/跌停分开统计：待选池 -> 观察池（含连续大跌剔除归因）
    split = {}
    for kind in ("涨停", "跌停"):
        base_n = sum(1 for c in candidates if c["类型"] == kind)
        kept_n = sum(1 for c in watch if c["类型"] == kind)
        drop_n = sum(1 for c in candidates if c["类型"] == kind
                     and c["trend"] in ("上升趋势", "高位震荡") and is_breakdown(c))
        split[kind] = {"待选池": base_n, "纳入观察池": kept_n,
                       "纳入率": round(kept_n / base_n * 100, 1) if base_n else 0.0,
                       "连续大跌剔除": drop_n}

    # 叠加 T 日状态——状态/涨跌幅/现价必须同源，避免"跌停却显示 +10%"这类错配
    # 前瞻池无 T+1 状态（未来数据），标记为"待观察"
    if forward:
        for c in candidates:
            c["T日涨跌幅"] = None
            c["T日现价"] = None
            c["T日状态"] = "待观察"
            c["状态来源"] = f"待{next_date}观察"
    else:
        day_q = D.day_quotes([c["代码"] for c in candidates], obs_date, mode)
        for c in candidates:
            q = day_q.get(c["代码"], {})
            c["T日涨跌幅"] = q.get("涨跌幅")
            c["T日现价"] = q.get("现价")
            c["T日状态"] = q.get("状态", "无数据")
            c["状态来源"] = q.get("来源", "无数据")

    # 情绪指标：待选池来源日的收盘结构 + T日盘中广度
    sentiment = _sentiment(base_date, mode)
    if mode == "intraday" and not forward:
        breadth = D.realtime_market_breadth()
        sentiment["盘中广度"] = breadth

    return {
        "obs_date": obs_date, "base_date": base_date, "mode": mode,
        "forward": forward, "next_date": next_date,
        "sentiment": sentiment,
        "candidate_count": len(candidates), "watch_count": len(watch),
        "split": split,
        "candidates": candidates, "watch": watch,
        "weights_used": {**C.WEIGHTS, **(weights or {})},
    }


def _sentiment(date: str, mode: str) -> dict:
    up = D.limit_up_pool(date)
    down = D.limit_down_pool(date)
    broken = D.broken_pool(date)
    prev_up = D.prev_limit_up_pool(date)
    all_up = int((~up["_excluded"]).sum()) if not up.empty else 0
    all_down = int((~down["_excluded"]).sum()) if not down.empty else 0
    n_broken = len(broken)
    blast = n_broken / (all_up + n_broken) if (all_up + n_broken) else 0.0
    board_dist, max_board = {}, 1
    if not up.empty:
        for b in up.loc[~up["_excluded"], "连板数"].astype(int).tolist():
            board_dist[b] = board_dist.get(b, 0) + 1
            max_board = max(max_board, b)
    premium = float(prev_up["涨跌幅"].mean()) if not prev_up.empty and "涨跌幅" in prev_up else None
    prem_up = float((prev_up["涨跌幅"] > 0).mean()) if not prev_up.empty and "涨跌幅" in prev_up else None
    return {
        "统计日": date, "涨停家数": all_up, "跌停家数": all_down, "炸板数": n_broken,
        "炸板率": round(blast, 4), "最高连板": max_board,
        "连板梯队": dict(sorted(board_dist.items())),
        "昨涨停今日溢价": round(premium, 2) if premium is not None else None,
        "昨涨停上涨占比": round(prem_up, 4) if prem_up is not None else None,
    }


# ============ 信号（基于 T-1待选池 -> T日状态 的状态转移） ============
def detect_signals(pools: dict, params: dict = None) -> dict:
    """信号在全待选池上判定（弱转强本就是下跌后的反转，先按趋势过滤会漏掉真信号），
    以【趋势为主】分级：高位震荡(A) / 上升趋势(B) / 震荡偏弱(C) / 下跌趋势(D)。

    v6：可买等级由 params['buyable_grades'] 覆盖（默认取自动调参结论或 C.W2S_BUYABLE_GRADES）。
    回测显示 C 级中线(T+20)胜率最高，自动调参会据此把 C 级纳入中线关注。
    """
    params = params or {}
    buyable = set(params.get("buyable_grades", C.W2S_BUYABLE_GRADES) or ())
    hold = int(params.get("hold_horizon", C.HOLD_HORIZON))
    obs_date = pools["obs_date"]
    base_date = pools["base_date"]
    watch_codes = {c["代码"] for c in pools["watch"]}
    candidates = pools["candidates"]  # 全部待选池，已叠加 T 日状态

    weak_to_strong, a_kill = [], []
    type_counter = {}
    for c in candidates:
        status = c.get("T日状态")
        in_watch = "✓" if c["代码"] in watch_codes else "—"
        if c["类型"] == "跌停" and status == "涨停":
            # 以"跌停洗盘前的趋势"分级：判断弱转强质量要看洗盘前所处的趋势位置
            trend = c.get("前期趋势") or c.get("trend", "—")
            meta = C.W2S_Trend_TYPES.get(trend, C.W2S_DEFAULT_TYPE)
            grade = meta["grade"]
            type_counter[meta["label"]] = type_counter.get(meta["label"], 0) + 1
            weak_to_strong.append({
                "代码": c["代码"], "名称": c["名称"], "所属行业": c["所属行业"],
                "昨状态": "跌停", "今状态": f"涨停({c.get('T日涨跌幅')}%)",
                "连板数": c.get("连板数", 1), "综合评分": c.get("综合评分"),
                "趋势": trend, "当前趋势": c.get("trend", "—"),
                "信号类型": meta["label"], "等级": grade,
                "在观察池": in_watch,
                "封板时间": c.get("首次封板时间", "—"), "原因": c["原因标注"],
                "信号": f"弱转强·{meta['label']}（{grade}级）",
                "建议": meta["action"] + (f"；{'封板瞬间为打板买点' if grade in ('A','B') else f'中线持有约{hold}个交易日'}，需结合盘口与仓位纪律"
                                     if grade in buyable else "；不作为买入标的，仅观察"),
                "纳入自选池": grade in buyable,
                "可买等级": sorted(buyable),   # 入池时的规则快照，便于日后核对历史自动池条目
                "纳入价格": c.get("T日现价"),   # 自选池跟踪的成本基准（信号触发时价格）
                "持有周期": hold if grade in buyable else None,
                "连续大跌": c.get("连续大跌", False),
                "破位大跌": c.get("破位大跌", False),
                "ret5": c.get("ret5"),
                "状态来源": c.get("状态来源", ""),
            })
        elif c["类型"] == "涨停" and status == "跌停":
            a_kill.append({
                "代码": c["代码"], "名称": c["名称"], "所属行业": c["所属行业"],
                "昨状态": "涨停", "今状态": f"跌停({c.get('T日涨跌幅')}%)",
                "综合评分": c.get("综合评分"), "趋势": c.get("trend", "—"),
                "在观察池": in_watch, "原因": c["原因标注"],
                "风险等级": "高" if c.get("trend") in ("上升趋势", "高位震荡") else "中",
                "提示": "情绪转弱·A杀风险，不纳入自选池",
                "状态来源": c.get("状态来源", ""),
            })
    # 趋势为主：先按等级，再按综合评分排序
    weak_to_strong.sort(key=lambda x: (x["等级"], -(x.get("综合评分") or 0)))
    return {"obs_date": obs_date, "base_date": base_date,
            "weak_to_strong": weak_to_strong, "a_kill": a_kill,
            "w2s_type_count": type_counter,
            "w2s_buyable": sum(1 for x in weak_to_strong if x["纳入自选池"])}


def sentiment_score(s: dict, weights: dict = None) -> tuple:
    """情绪温度综合评分(0-100)与周期定位。"""
    if not s:
        return None, "数据缺失"
    w = {**C.WEIGHTS, **(weights or {})}
    up = s.get("涨停家数", 0)
    down = s.get("跌停家数", 0)
    blast = s.get("炸板率", 0.5)
    maxb = s.get("最高连板", 1)
    prem = s.get("昨涨停今日溢价")
    score = 0.0
    score += min(up / 100, 1) * 30
    score += max(0, 1 - down / max(up, 1)) * 20
    score += (1 - min(blast, 1)) * 20
    score += min(maxb / 8, 1) * 15
    score += (float(np.clip((prem + 3) / 10, 0, 1)) * 15) if prem is not None else 7.5
    score = round(score, 1)
    if score >= 78:
        phase = "主升/高潮"
    elif score >= 60:
        phase = "修复/加强"
    elif score >= 42:
        phase = "震荡分歧"
    elif score >= 25:
        phase = "退潮/冰点边缘"
    else:
        phase = "冰点"
    return score, phase
