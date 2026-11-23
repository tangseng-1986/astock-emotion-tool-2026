# -*- coding: utf-8 -*-
"""回测引擎：校验参数/权重合理性与策略有效性（趋势分级口径）。

方法（自包含，不依赖涨跌停池接口的历史窗口限制）：
  1. 用最近若干交易日的涨停池构建"活跃股票宇宙"（曾涨停的票 = 情绪票池）。
  2. 对宇宙内每只票拉长周期前复权K线，按涨跌停幅度从K线反推每日是否涨停/跌停。
  3. 逐日回放：某日 D-1 跌停 且 D 反手涨停 => 弱转强信号；D-1 涨停 且 D 跌停 => A杀。
  4. 对弱转强信号按【趋势类型】分级（高位震荡/上升趋势/震荡偏弱/下跌趋势），
     统计各等级后验收益与胜率（观察窗口 T+5/T+10/T+20），验证"趋势为主"的分级是否合理。
  5. 参数敏感性：评分门槛扫描、连续大跌过滤、情绪分位、多套权重方案对比。

注意：K线为近似复权口径，涨跌停用涨跌幅阈值近似判定，结果用于参数相对比较，非精确成交回测。
"""
import numpy as np
import pandas as pd

import config as C
import data_fetcher as D
import analyzer as A


def build_universe(end_date: str, n_dates: int = None, cap: int = None) -> list:
    """用最近N个交易日涨停池的并集构建回测股票宇宙。
    涨停池接口不可用时，回退到本地K线缓存中的历史活跃股，保证回测不因单一数据源失败而中断。"""
    n_dates = n_dates or C.BACKTEST_UNIVERSE_DATES
    cap = cap or C.BACKTEST_UNIVERSE_CAP
    cal = D.trade_calendar()
    dates = [d for d in cal if d <= end_date][-n_dates:]
    pool = []
    ok_dates = 0
    for d in dates:
        try:
            zt = D.limit_up_pool(d)
            if not zt.empty:
                pool.extend(zt.loc[~zt["_excluded"], "代码"].tolist())
                ok_dates += 1
        except Exception:
            continue
    if pool:
        freq = pd.Series(pool).value_counts()
        return freq.index.tolist()[:cap]
    # 兜底：从本地K线缓存提取代码（缓存文件名形如 000707_xxx.csv）
    fallback = []
    try:
        cache_dir = C.KLINE_CACHE
        for p in cache_dir.glob("*.csv"):
            code = p.name.split("_")[0]
            if len(code) == 6 and code.isdigit():
                fallback.append(code)
    except Exception:
        pass
    if fallback:
        freq = pd.Series(fallback).value_counts()
        return freq.index.tolist()[:cap]
    return []


def _daily_limit_flags(kline: pd.DataFrame) -> pd.DataFrame:
    """由K线反推每日涨跌停标记。返回 date/pct/limit_up/limit_down/close/high。"""
    if kline is None or len(kline) < 2 or "收盘" not in kline.columns:
        return pd.DataFrame()
    cols = ["日期", "收盘"] + (["最高"] if "最高" in kline.columns else [])
    df = kline[cols].copy()
    df["日期"] = df["日期"].astype(str).str.replace("-", "").str[:8]
    df["close"] = pd.to_numeric(df["收盘"], errors="coerce")
    if "最高" in df.columns:
        df["high"] = pd.to_numeric(df["最高"], errors="coerce")
    else:
        df["high"] = df["close"]
    df["pct"] = ((df["close"] / df["close"].shift(1) - 1) * 100).round(2)
    tol = 0.15
    df["limit_up"] = df["pct"] >= C.LIMIT_MAIN - tol
    df["limit_down"] = df["pct"] <= -C.LIMIT_MAIN + tol
    return df.reset_index(drop=True)


def _forward_returns(flags: pd.DataFrame, idx: int, horizons=None) -> dict:
    """信号日后 T+5/T+10/T+20 相对信号日收盘的收益。数据不足的节点留空。"""
    out = {}
    if idx >= len(flags) - 1:
        return out
    base = flags.loc[idx, "close"]
    if not base or base <= 0:
        return out
    horizons = horizons or C.BACKTEST_HORIZONS
    for k in horizons:
        j = idx + k
        if j < len(flags):
            out[f"T+{k}"] = round((flags.loc[j, "close"] / base - 1) * 100, 2)
    return out


def _trend_at(flags: pd.DataFrame, i: int) -> str:
    """用信号日之前(含当日)的K线判定趋势，避免未来函数。"""
    if i < C.TREND_MIN_BARS:
        return "数据不足"
    lo = max(0, i - C.TREND_LOOKBACK_DAYS)
    sub = pd.DataFrame({
        "收盘": flags["close"].iloc[lo:i + 1].values,
        "最高": flags["high"].iloc[lo:i + 1].values,
    })
    try:
        return A.classify_trend(sub)["trend"]
    except Exception:
        return "数据不足"


def _factors_at(flags: pd.DataFrame, i: int, emo: float) -> dict:
    """构造信号日的五因子归一化评分（与 analyzer.composite_score 口径对齐）。"""
    close = flags["close"]
    slope = A._slope_pct(close.iloc[max(0, i - C.MA_MID):i], C.MA_MID) if i >= 10 else 0.0
    trend_s = A._clip01((slope + 0.15) / 0.45)
    pos_s = A._clip01(1 - abs(float(flags.loc[i, "pct"]) - C.LIMIT_MAIN) / 20)
    limit_s = 0.6                       # 弱转强多为首板，封板质量取中性
    theme_s = A._clip01((emo - 30) / 50)
    ret5 = (float(close.iloc[i]) / float(close.iloc[i - C.MOMENTUM_LOOKBACK]) - 1) * 100 \
        if i >= C.MOMENTUM_LOOKBACK else 0.0
    mom_s = A._clip01((ret5 - C.MOM_LOSS) / (C.MOM_GAIN - C.MOM_LOSS))
    p1 = float(flags.loc[i - 1, "pct"]) if i >= 1 else 0.0
    p2 = float(flags.loc[i - 2, "pct"]) if i >= 2 else 0.0
    consec = p1 <= -C.BIG_DROP_PCT and p2 <= -C.BIG_DROP_PCT
    if consec:
        mom_s *= C.CONSEC_DROP_PENALTY
    return {"trend": trend_s, "position": pos_s, "limit_quality": limit_s,
            "theme": theme_s, "short_momentum": mom_s, "consec": consec, "ret5": ret5}


def _weighted_score(f: dict, weights: dict) -> float:
    w = {**C.WEIGHTS, **(weights or {})}
    tw = sum(w.values()) or 1
    return sum(w.get(k, 0) * f.get(k, 0) for k in w) / tw * 100


def _stats(items: list) -> dict:
    if not items:
        return {"样本数": 0}
    res = {"样本数": len(items), "各周期有效样本": {}}
    for k in C.BACKTEST_HORIZONS:
        vals = [x["fr"].get(f"T+{k}") for x in items if x["fr"].get(f"T+{k}") is not None]
        res["各周期有效样本"][f"T+{k}"] = len(vals)
        res[f"T+{k}平均收益%"] = float(round(np.mean(vals), 2)) if vals else None
        res[f"T+{k}胜率%"] = round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1) if vals else None
    t5 = [x["fr"].get("T+5") for x in items if x["fr"].get("T+5") is not None]
    res["中位T+5%"] = float(round(float(np.median(t5)), 2)) if t5 else None
    return res


def run_backtest(end_date: str = None, weights: dict = None, verbose: bool = True) -> dict:
    end_date = D.normalize_date(end_date) if end_date else D.latest_trade_date()
    weights = weights or {}
    if verbose:
        print(f"[回测] 构建股票宇宙(截至{end_date}) ...")
    universe = build_universe(end_date)
    if not universe:
        return {"error": "股票宇宙为空，无法回测"}

    klines = D.daily_kline_batch(universe, end_date, lookback_days=C.TREND_LOOKBACK_DAYS,
                                 workers=C.KLINE_WORKERS)
    flag_map = {}
    all_dates = set()
    for code, k in klines.items():
        flags = _daily_limit_flags(k)
        if flags.empty:
            continue
        flag_map[code] = flags
        all_dates.update(flags["日期"].tolist())
    trade_dates = sorted(all_dates)
    bt_dates = trade_dates[-C.BACKTEST_LOOKBACK_DAYS:]
    if len(bt_dates) < 10:
        return {"error": f"有效交易日不足({len(bt_dates)})，无法回测"}
    date_set = set(bt_dates)
    if verbose:
        print(f"[回测] 宇宙 {len(universe)} 只，回放 {bt_dates[0]} ~ {bt_dates[-1]} 共 {len(bt_dates)} 日")

    # 每日情绪分（宇宙口径）
    daily_emotion = []
    for d in bt_dates:
        up_n = down_n = 0
        for flags in flag_map.values():
            row = flags[flags["日期"] == d]
            if row.empty:
                continue
            r = row.iloc[0]
            up_n += int(r["limit_up"])
            down_n += int(r["limit_down"])
        daily_emotion.append({"date": d, "up": up_n, "down": down_n,
                              "emo": 50 + (up_n - down_n)})
    emo_map = {x["date"]: x["emo"] for x in daily_emotion}

    signals, akills = [], []
    for code, flags in flag_map.items():
        flags = flags.reset_index(drop=True)
        # 弱转强：前一日跌停 -> 今日涨停
        for i in range(1, len(flags)):
            d = flags.loc[i, "日期"]
            if d not in date_set:
                continue
            if not (flags.loc[i - 1, "limit_down"] and flags.loc[i, "limit_up"]):
                continue
            fr = _forward_returns(flags, i)
            if not fr:
                continue
            emo = emo_map.get(d, 50)
            # 与实盘一致：弱转强分级用"跌停洗盘前"的趋势（信号日前2根K线）
            trend = _trend_at(flags, max(0, i - C.CONSEC_DROP_DAYS))
            factors = _factors_at(flags, i, emo)
            meta = C.W2S_Trend_TYPES.get(trend, C.W2S_DEFAULT_TYPE)
            signals.append({
                "code": code, "date": d, "fr": fr, "emo": emo,
                "trend": trend, "grade": meta["grade"], "type": meta["label"],
                "buyable": meta["buy"], "score": round(_weighted_score(factors, weights), 1),
                "consec": factors["consec"], "factors": factors,
            })
        # A杀：前一日涨停 -> 今日跌停
        for i in range(1, len(flags)):
            d = flags.loc[i, "日期"]
            if d not in date_set:
                continue
            if flags.loc[i - 1, "limit_up"] and flags.loc[i, "limit_down"]:
                fr = _forward_returns(flags, i)
                if fr:
                    akills.append({"code": code, "date": d, "fr": fr,
                                   "trend": _trend_at(flags, i)})

    overall = _stats(signals)
    akill_stats = _stats(akills)

    # 1) 趋势分级后验：验证"趋势为主"的分类是否有区分度
    trend_split = {}
    for trend in list(C.W2S_Trend_TYPES.keys()) + ["数据不足"]:
        items = [x for x in signals if x["trend"] == trend]
        if items:
            st = _stats(items)
            st["等级"] = C.W2S_Trend_TYPES.get(trend, C.W2S_DEFAULT_TYPE)["grade"]
            trend_split[trend] = st

    # 2) 情绪分位
    emo_vals = [x["emo"] for x in daily_emotion]
    med_emo = float(np.median(emo_vals)) if emo_vals else 50
    hi = _stats([x for x in signals if x["emo"] >= med_emo])
    lo = _stats([x for x in signals if x["emo"] < med_emo])

    # 3) 连续大跌过滤敏感性：验证剔除规则是否有效
    consec_yes = _stats([x for x in signals if x["consec"]])
    consec_no = _stats([x for x in signals if not x["consec"]])

    # 4) 评分门槛扫描
    threshold_sensitivity = []
    for th in [35, 40, 45, 50, 55, 60]:
        kept = [x for x in signals if x["score"] >= th]
        st = _stats(kept)
        row = {"门槛": th, "样本数": st.get("样本数", 0),
               "样本代码": [x["code"] for x in kept[:30]]}
        for k in C.BACKTEST_HORIZONS:
            row[f"T+{k}平均收益%"] = st.get(f"T+{k}平均收益%")
            row[f"T+{k}胜率%"] = st.get(f"T+{k}胜率%")
        threshold_sensitivity.append(row)

    # 5) 权重方案敏感性
    schemes = {
        "当前权重": dict(C.WEIGHTS),
        "趋势优先": {"trend": 50, "position": 10, "limit_quality": 15, "theme": 10, "short_momentum": 15},
        "题材优先": {"trend": 15, "position": 10, "limit_quality": 15, "theme": 45, "short_momentum": 15},
        "动能优先": {"trend": 15, "position": 10, "limit_quality": 15, "theme": 10, "short_momentum": 50},
    }
    if weights:
        schemes["自定义权重"] = {**C.WEIGHTS, **weights}
    weight_sensitivity = _weight_sensitivity(signals, schemes)

    # 下钻样本（供看板图表点击联动）
    signal_samples = [{
        "code": x["code"], "date": x["date"], "trend": x["trend"],
        "grade": x["grade"], "type": x["type"], "score": x["score"],
        "emo": x["emo"], "consec": x["consec"],
        **{f"T+{k}": x["fr"].get(f"T+{k}") for k in C.BACKTEST_HORIZONS},
    } for x in signals]

    return {
        "end_date": end_date,
        "period": f"{bt_dates[0]} ~ {bt_dates[-1]}",
        "universe_size": len(universe), "trade_days": len(bt_dates),
        "overall": overall, "akill": akill_stats,
        "trend_split": trend_split,
        "emotion_split": {"中位情绪分": round(med_emo, 1), "高情绪": hi, "低情绪": lo},
        "consecutive_drop": {"连续大跌后反转": consec_yes, "非连续大跌反转": consec_no},
        "threshold_sensitivity": threshold_sensitivity,
        "weight_sensitivity": weight_sensitivity,
        "signal_samples": signal_samples[:400],
        "daily_emotion": daily_emotion[-30:],
        "caveat": "回测基于K线涨跌幅近似判定涨跌停，用于参数相对比较，非精确成交回测；不构成投资建议。",
    }


def _weight_sensitivity(signals: list, schemes: dict) -> dict:
    """比较不同权重方案下，评分门槛对信号样本的筛选效果（用同一批因子按各方案重算评分）。
    每个方案同时给出 T+5/T+10/T+20 的过门槛收益与胜率。"""
    primary = C.BACKTEST_HORIZONS[0]  # 主周期 T+5
    out = {}
    for name, w in schemes.items():
        scored = []  # (score, fr, code)
        for x in signals:
            if x["fr"].get(f"T+{primary}") is None:
                continue
            s = _weighted_score(x["factors"], w)
            scored.append((s, x["fr"], x["code"]))
        if not scored:
            out[name] = {"样本数": 0}
            continue
        kept = [(s, fr, c) for s, fr, c in scored if s >= C.SCORE_WATCH_MIN]
        row = {"样本数": len(scored), "过门槛样本": len(kept)}
        for k in C.BACKTEST_HORIZONS:
            all_vals = [fr.get(f"T+{k}") for _, fr, _ in scored if fr.get(f"T+{k}") is not None]
            kept_vals = [fr.get(f"T+{k}") for _, fr, _ in kept if fr.get(f"T+{k}") is not None]
            row[f"全样本T+{k}%"] = round(float(np.mean(all_vals)), 2) if all_vals else None
            row[f"过门槛T+{k}%"] = round(float(np.mean(kept_vals)), 2) if kept_vals else None
            row[f"过门槛T+{k}胜率%"] = round(sum(1 for r in kept_vals if r > 0) / len(kept_vals) * 100, 1) if kept_vals else None
        row["过门槛样本代码"] = [c for _, _, c in kept][:30]
        out[name] = row
    return out
