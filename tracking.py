# -*- coding: utf-8 -*-
"""自选池持续跟踪：从入池日起逐日统计后续走势，直至手动剔除。

统计口径：
  - 成本基准：入池信号价（优先）> 入池日收盘 > 入池后首根K线收盘
  - 持有交易日：入池日之后已有的交易日K线数量
  - 累计收益 / 区间最高收益 / 区间最大回撤 / T+5 / T+10 / T+20：均以前复权收盘价计算
  - 剔除后停止更新，终态统计写入 watchlist_archive，历史快照仍保留
盘中监测另外用腾讯实时快照刷新"最新价/涨跌幅/状态"，与收盘统计互不覆盖。
"""
import datetime as dt

import pandas as pd

import config as C
import data_fetcher as D
import storage as S


def _num(v):
    try:
        if v is None or v == "" or v == "—":
            return None
        f = float(v)
        return f if f == f else None
    except Exception:
        return None


def _pct(now, base):
    if now is None or not base:
        return None
    return round((now / base - 1.0) * 100.0, 2)


def _close_of(df, date_str):
    if df is None or df.empty:
        return None
    hit = df[df["日期"].astype(str) == str(date_str)]
    if not hit.empty:
        return _num(hit.iloc[-1]["收盘"])
    return None


def _base_price(info: dict, df, add_date: str):
    """返回 (基准价, 基准日期, 是否使用信号价)。"""
    explicit = _num(info.get("add_price"))
    if explicit and explicit > 0:
        return explicit, add_date, True
    c = _close_of(df, add_date)
    if c and c > 0:
        return c, add_date, False
    # 入池日非交易日/停牌：取入池后第一根K线
    if df is not None and not df.empty:
        later = df[df["日期"].astype(str) >= str(add_date)]
        if not later.empty:
            v = _num(later.iloc[0]["收盘"])
            if v and v > 0:
                return v, str(later.iloc[0]["日期"]), False
        v = _num(df.iloc[-1]["收盘"])
        if v and v > 0:
            return v, str(df.iloc[-1]["日期"]), False
    return None, add_date, False


def compute_stats(code: str, info: dict, obs_date: str, df=None, realtime=None):
    """计算单只个股截至 obs_date 的跟踪统计，返回 stats dict（失败返回 None）。

    realtime 为腾讯实时快照行（zxj/zdf/实时状态）。收盘复盘时用它补齐尚未落库的
    当日K线，避免"手动池个股状态显示—"或整只个股数据缺失。
    """
    rt = realtime or {}
    rt_px = _num(rt.get("zxj"))
    rt_pct = _num(rt.get("zdf"))
    rt_status = rt.get("实时状态")
    if df is None or df.empty:
        df = D.daily_kline(code, obs_date, lookback_days=C.TRACK_LOOKBACK_DAYS)
    if df is None or df.empty:
        if rt_px:
            df = pd.DataFrame([{"日期": str(obs_date), "收盘": rt_px, "最高": rt_px}])
        else:
            return None
    if "收盘" not in df.columns:
        return None
    add_date = str(info.get("add_date") or obs_date)
    df = df[df["日期"].astype(str) <= str(obs_date)].copy()
    # 实时快照优先：覆盖/补入当日K线，保证状态与涨跌幅同源
    if rt_px:
        hit = df[df["日期"].astype(str) == str(obs_date)]
        if hit.empty:
            df = pd.concat([df, pd.DataFrame([{
                "日期": str(obs_date), "收盘": rt_px, "最高": rt_px}])], ignore_index=True)
        else:
            i = hit.index[-1]
            df.at[i, "收盘"] = rt_px
            hi = _num(df.at[i, "最高"]) if "最高" in df.columns else None
            if "最高" in df.columns:
                df.at[i, "最高"] = max(hi or rt_px, rt_px)
        df = df.sort_values("日期").reset_index(drop=True)
    if df.empty:
        return None
    base, base_date, from_signal = _base_price(info, df, add_date)
    if not base:
        return None
    rows = df[df["日期"].astype(str) >= str(base_date)].copy()
    if rows.empty:
        return None
    closes = [_num(x) for x in rows["收盘"]]
    closes = [x for x in closes if x is not None]
    if not closes:
        return None
    highs = [_num(x) for x in rows["最高"]] if "最高" in rows.columns else closes
    highs = [h if h is not None else c for h, c in zip(highs, closes)] or closes
    peak = closes[0]
    max_dd = 0.0
    for c in closes:
        peak = max(peak, c)
        max_dd = min(max_dd, (c / peak - 1.0) * 100.0)

    limit = D.limit_threshold(code)
    up_days = down_days = 0
    prev = base
    for i, c in enumerate(closes):
        pct = (c / prev - 1.0) * 100.0 if prev else 0.0
        if i > 0:
            if pct >= limit - C.REALTIME_LIMIT_TOL:
                up_days += 1
            elif pct <= -limit + C.REALTIME_LIMIT_TOL:
                down_days += 1
        prev = c
    last = closes[-1]
    use_rt = bool(rt_px and str(rows.iloc[-1]["日期"]) == str(obs_date))
    last_pct = rt_pct if (use_rt and rt_pct is not None) else (
        (last / closes[-2] - 1.0) * 100.0 if len(closes) > 1 else None)
    status = "—"
    if use_rt and rt_status:
        status = rt_status
    elif last_pct is not None:
        if last_pct >= limit - C.REALTIME_LIMIT_TOL:
            status = "涨停"
        elif last_pct <= -limit + C.REALTIME_LIMIT_TOL:
            status = "跌停"
        elif last_pct > 0:
            status = "上涨"
        elif last_pct < 0:
            status = "下跌"

    stats = {
        "基准价": round(base, 2), "基准日期": base_date, "成本来源": "入池信号价" if from_signal else "收盘价",
        "最新日期": str(rows.iloc[-1]["日期"]), "最新价": round(last, 2),
        "最新涨跌%": round(last_pct, 2) if last_pct is not None else None,
        "最新状态": status,
        "持有交易日": max(0, len(closes) - 1),
        "累计收益%": _pct(last, base),
        "区间最高收益%": _pct(max(highs), base) if highs else None,
        "区间最大回撤%": round(max_dd, 2),
        "涨停次数": up_days, "跌停次数": down_days,
        "统计时间": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if use_rt:
        stats["实时更新"] = stats["统计时间"]
    for h in C.TRACK_HORIZONS:
        stats[f"T+{h}收益%"] = _pct(closes[h], base) if len(closes) > h else None
    return stats


def update_tracking(state: dict, obs_date: str, mode: str = "close", klines: dict = None) -> dict:
    """刷新所有在池个股跟踪；klines 可复用主流程已拉取的K线 {code: DataFrame}。"""
    S_ = state.setdefault("tracking", {})
    active = [(p, code, info) for p, code, info in S.iter_watch(state)]
    # 收盘复盘也拉一次实时快照：K线缓存常滞后一天，用它补齐当日收盘状态。
    # 仅复盘当天有效，历史日期回填不能拿今天行情覆盖。
    rt_map = {}
    if str(obs_date) != dt.date.today().strftime("%Y%m%d"):
        rt_map = {}
    else:
        try:
            rt = D.realtime_quotes()
            if rt is not None and not rt.empty and "代码" in rt.columns:
                rt_map = rt.set_index("代码").to_dict("index")
        except Exception:
            rt_map = {}
    need = [c for _, c, _ in active if klines is None or c not in klines]
    if klines is None:
        klines = {}
        if need:
            try:
                klines = D.daily_kline_batch(need, obs_date, lookback_days=C.TRACK_LOOKBACK_DAYS,
                                             workers=C.KLINE_WORKERS)
            except Exception:
                klines = {}
    updated = failed = 0
    for _pool, code, info in active:
        tr = S_.setdefault(code, {})
        if tr.get("status") == "removed":
            continue
        try:
            st = compute_stats(code, info, obs_date, klines.get(code),
                               realtime=rt_map.get(code))
            if not st:
                failed += 1
                continue
            tr.update({"code": code, "name": info.get("名称", code), "pool": _pool,
                       "add_date": info.get("add_date"), "status": "active", "stats": st})
            snaps = tr.setdefault("snapshots", [])
            d = st["最新日期"]
            snap = {"date": d, "close": st["最新价"], "pct": st["累计收益%"], "tag": "收盘"}
            if not any(x.get("date") == d and x.get("tag") == "收盘" for x in snaps):
                snaps.append(snap)
            tr["snapshots"] = snaps[-C.TRACK_SNAPSHOT_MAX:]
            updated += 1
        except Exception:
            failed += 1
    return {"active": len(active), "updated": updated, "failed": failed}


def update_realtime(state: dict, rows: list, date: str = None):
    """盘中用腾讯实时快照刷新在池个股的最新价/涨跌幅/状态（不改动收盘统计）。"""
    date = date or dt.datetime.now().strftime("%Y%m%d")
    qmap = {}
    for r in rows or []:
        c = str(r.get("代码", "")).zfill(6)
        qmap[c] = r
    n = 0
    for _pool, code, info in S.iter_watch(state):
        r = qmap.get(code)
        if not r:
            continue
        tr = state.setdefault("tracking", {}).setdefault(code, {})
        if tr.get("status") == "removed":
            continue
        px = _num(r.get("现价"))
        pct = _num(r.get("涨跌幅"))
        st = tr.setdefault("stats", {})
        base = _num(st.get("基准价"))
        if px is not None and not base:
            # 看板手动加池时可能尚无收盘K线：先用实时价建立成本基准
            st["基准价"] = round(px, 2)
            st["基准日期"] = date
            st["成本来源"] = "入池信号价"
            base = px
        if px is not None:
            st["最新价"] = round(px, 2)
            st["最新日期"] = date
        if pct is not None:
            st["最新涨跌%"] = round(pct, 2)
            st["最新状态"] = r.get("状态") or st.get("最新状态", "—")
        if px is not None and base:
            st["累计收益%"] = _pct(px, base)
        st["实时更新"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        snaps = tr.setdefault("snapshots", [])
        if not any(x.get("date") == date and x.get("tag") == "盘中" for x in snaps):
            snaps.append({"date": date, "close": st.get("最新价"),
                          "pct": st.get("累计收益%"), "tag": "盘中"})
            tr["snapshots"] = snaps[-C.TRACK_SNAPSHOT_MAX:]
        n += 1
    return n
