# -*- coding: utf-8 -*-
"""数据获取层：涨跌停池、涨停原因、历史K线、交易日历。

数据源：
  - 涨停池/跌停池/炸板池/昨涨停池/历史K线：akshare（东方财富）
  - 涨停原因 reason_type / 连板描述 high_days：同花顺 dataapi（非一手来源，标注"需核实"）
所有行情数字均为运行时动态获取，不硬编码。
"""
import time
import datetime as dt
import hashlib
import pandas as pd
import requests
import akshare as ak
from concurrent.futures import ThreadPoolExecutor, as_completed

import config as C

_KLINE_CACHE = C.DATA_DIR / "kline_cache"
_KLINE_CACHE.mkdir(parents=True, exist_ok=True)


def _retry(fn, *args, _retries=None, _sleep=0.3, **kwargs):
    last = None
    retries = _retries or C.MAX_RETRY
    for i in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa
            last = e
            time.sleep(_sleep * (i + 1))
    raise last


def is_excluded(name: str) -> bool:
    """剔除 ST / *ST / 退市 / 退字股。"""
    if not isinstance(name, str):
        return False
    n = name.upper().replace(" ", "")
    return ("ST" in n) or ("退" in n) or n.startswith("*")


def normalize_date(d: str) -> str:
    """'YYYYMMDD' / 'YYYY-MM-DD' -> 'YYYYMMDD'。"""
    return d.replace("-", "").strip()[:8]


# ---------------- 交易日历 ----------------
_CAL_CACHE = {"dates": None}


def trade_calendar() -> list:
    if _CAL_CACHE["dates"] is not None:
        return _CAL_CACHE["dates"]
    try:
        fn = getattr(ak, "calendar_trade_date_hist_sina", None) or getattr(ak, "tool_trade_date_hist_sina")
        df = _retry(fn)
        dates = sorted(normalize_date(str(x)) for x in df["trade_date"].astype(str))
        _CAL_CACHE["dates"] = dates
        return dates
    except Exception:
        _CAL_CACHE["dates"] = []
        return []


def latest_trade_date(ref: str = None) -> str:
    """返回最近的"已收盘"交易日。
    - 指定 ref：返回 <= ref 的最近交易日。
    - 未指定：若在交易日 15:00 前运行（盘中），回退到上一交易日，避免把盘中数据当成收盘数据。
    """
    now = dt.datetime.now()
    if ref:
        ref = normalize_date(ref)
    else:
        # 15:00 收盘，留 10 分钟数据落库缓冲；15:10 前按上一交易日处理
        cutoff = now.replace(hour=15, minute=10, second=0, microsecond=0)
        ref = (now.date() if now >= cutoff else now.date() - dt.timedelta(days=1)).strftime("%Y%m%d")
    cal = trade_calendar()
    if cal:
        past = [d for d in cal if d <= ref]
        if past:
            return past[-1]
    # 兜底：按周末回填
    d = dt.datetime.strptime(ref, "%Y%m%d").date()
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d.strftime("%Y%m%d")


def prev_trade_date(date: str, n: int = 1) -> str:
    """date 之前第 n 个交易日。"""
    date = normalize_date(date)
    cal = trade_calendar()
    if cal:
        past = [d for d in cal if d < date]
        return past[-n] if len(past) >= n else (past[0] if past else date)
    d = dt.datetime.strptime(date, "%Y%m%d").date() - dt.timedelta(days=1)
    cnt = 0
    while cnt < n:
        if d.weekday() < 5:
            cnt += 1
        if cnt < n:
            d -= dt.timedelta(days=1)
    return d.strftime("%Y%m%d")


def next_trade_date(date: str) -> str:
    """date 之后第 1 个交易日（用于"供下一交易日观察"的标注）。"""
    date = normalize_date(date)
    cal = trade_calendar()
    if cal:
        fut = [d for d in cal if d > date]
        if fut:
            return fut[0]
    d = dt.datetime.strptime(date, "%Y%m%d").date() + dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d.strftime("%Y%m%d")


# ---------------- 涨跌停池 ----------------
# 同一进程内按日期缓存：收盘复盘要构建"前瞻池+回顾池"，同一日期的池子只拉一次
_POOL_CACHE = {}


def limit_up_pool(date: str) -> pd.DataFrame:
    date = normalize_date(date)
    key = ("up", date)
    if key not in _POOL_CACHE:
        df = _retry(ak.stock_zt_pool_em, date=date)
        _POOL_CACHE[key] = _clean_pool(df, "up")
    return _POOL_CACHE[key]


def limit_down_pool(date: str) -> pd.DataFrame:
    date = normalize_date(date)
    key = ("down", date)
    if key not in _POOL_CACHE:
        df = _retry(ak.stock_zt_pool_dtgc_em, date=date)
        _POOL_CACHE[key] = _clean_pool(df, "down")
    return _POOL_CACHE[key]


def broken_pool(date: str) -> pd.DataFrame:
    """炸板池，用于炸板率。"""
    try:
        df = _retry(ak.stock_zt_pool_zbgc_em, date=normalize_date(date))
        return _clean_pool(df, "up")
    except Exception:
        return pd.DataFrame()


def prev_limit_up_pool(date: str) -> pd.DataFrame:
    """昨日涨停股今日表现，用于"打板次日溢价/高位股反馈"。"""
    try:
        df = _retry(ak.stock_zt_pool_previous_em, date=normalize_date(date))
        return _clean_pool(df, "up")
    except Exception:
        return pd.DataFrame()


def _clean_pool(df: pd.DataFrame, kind: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    # 上游接口偶尔返回重复索引；布尔掩码 .loc 对齐时会把同一行重复取出。
    # 先重置为唯一索引，从源头杜绝"观察池出现重复个股"。
    df = df.copy().reset_index(drop=True)
    df["代码"] = df["代码"].astype(str).str.zfill(6)
    df["_excluded"] = df["名称"].apply(is_excluded)
    df["_kind"] = kind
    return df


# ---------------- 涨停原因（同花顺，非一手来源） ----------------
def ths_reasons(date: str) -> dict:
    """返回 {code: {'reason':..., 'high_days':..., 'tag':...}}。失败返回空字典。"""
    date = normalize_date(date)
    out = {}
    page = 1
    try:
        while True:
            params = {
                "page": page, "limit": 200,
                "field": "199112,10,9001,330329,133971,133970,1968584,3475914,9002,9003",
                "filter": "HS,GEM2STAR", "order_field": "330329",
                "order_type": 0, "date": date,
            }
            r = requests.get(C.THS_REASON_URL, params=params,
                             headers=C.THS_HEADERS, timeout=C.REQUEST_TIMEOUT)
            j = r.json()
            data = j.get("data") or {}
            info = data.get("info") or []
            if not info:
                break
            for it in info:
                code = str(it.get("code", "")).zfill(6)
                out[code] = {
                    "reason": it.get("reason_type") or "—",
                    "high_days": it.get("high_days") or "",
                    "tag": it.get("change_tag") or "",
                    "suc_rate": it.get("limit_up_suc_rate"),
                    "open_num": it.get("open_num"),
                }
            pg = data.get("page") or {}
            if page >= int(pg.get("count", 1) or 1):
                break
            page += 1
            time.sleep(0.2)
    except Exception:
        pass
    return out


# ---------------- 历史K线 ----------------
def _local_code(code: str) -> str:
    """6位代码 -> 带市场前缀（腾讯/新浪用）。"""
    code = str(code).zfill(6)
    if code.startswith(("6", "9")):
        return "sh" + code
    if code.startswith(("0", "2", "3")):
        return "sz" + code
    if code.startswith(("4", "8", "5")):
        return "bj" + code
    return "sh" + code


def _std_kline(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """把不同数据源的K线统一成 日期/收盘/最高 列。"""
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    rename = {"date": "日期", "Date": "日期", "day": "日期",
              "close": "收盘", "high": "最高", "open": "开盘", "low": "最低"}
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    if "日期" in df:
        df["日期"] = df["日期"].astype(str).str.replace("-", "").str[:8]
    keep = [c for c in ["日期", "开盘", "收盘", "最高", "最低", "成交量"] if c in df.columns]
    df = df[keep]
    if "收盘" not in df.columns:
        return pd.DataFrame()
    df = df.sort_values("日期").reset_index(drop=True)
    return df


def daily_kline(code: str, end_date: str, lookback_days: int = None,
                use_cache: bool = True) -> pd.DataFrame:
    """前复权日线：腾讯优先 -> 东财兜底（带磁盘缓存）。失败返回空 DataFrame。
    腾讯接口按代码缓存，与 end_date 无关，避免同股重复抓取。
    """
    lookback_days = lookback_days or C.TREND_LOOKBACK_DAYS
    end = normalize_date(end_date)
    key = hashlib.md5(f"{code}_{end}_{lookback_days}".encode()).hexdigest()[:16]
    cache_file = _KLINE_CACHE / f"{code}_{key}.csv"
    # 缓存新鲜度校验：盘后首次拉取时行情源可能还没更新到当日，会把"只到T-1"的
    # K线永久缓存，导致之后每次复盘都漏算T日涨跌（如连续跌停少算一天）。
    # 发现缓存最后一根日期早于 end_date 时强制重拉一次；同一进程内不重复重拉。
    refreshed = getattr(daily_kline, "_refreshed", None)
    if refreshed is None:
        refreshed = daily_kline._refreshed = set()
    if use_cache and cache_file.exists():
        try:
            cached = pd.read_csv(cache_file, dtype={"日期": str})
            stale = (not cached.empty and "日期" in cached.columns
                     and normalize_date(str(cached["日期"].iloc[-1])) < end
                     and key not in refreshed)
            if not stale:
                return cached
            refreshed.add(key)
        except Exception:
            pass
    start_dt = dt.datetime.strptime(end, "%Y%m%d").date() - dt.timedelta(days=lookback_days)
    start = start_dt.strftime("%Y%m%d")
    df = pd.DataFrame()

    # 主源：腾讯（限流宽松，稳定）
    try:
        raw = _retry(ak.stock_zh_a_hist_tx, symbol=_local_code(code),
                     start_date=start, end_date=end, _retries=2, _sleep=0.2)
        df = _std_kline(raw, "tx")
    except Exception:
        pass
    # 兜底：东财
    if df.empty:
        try:
            raw = _retry(ak.stock_zh_a_hist_em, symbol=code, period="daily",
                         start_date=start, end_date=end, adjust="qfq", _retries=1, _sleep=0.3)
            df = _std_kline(raw, "em")
        except Exception:
            pass

    if not df.empty:
        try:
            df.to_csv(cache_file, index=False)
        except Exception:
            pass
    return df


def daily_kline_batch(codes: list, end_date: str, lookback_days: int = None,
                      workers: int = 4) -> dict:
    """并发拉取多只K线（低并发避免限流），返回 {code: DataFrame}。"""
    results = {}
    codes = list(dict.fromkeys(codes))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(daily_kline, c, end_date, lookback_days): c for c in codes}
        for fut in as_completed(futs):
            c = futs[fut]
            try:
                results[c] = fut.result()
            except Exception:
                results[c] = pd.DataFrame()
    return results


def stock_name_map(codes: list) -> dict:
    """代码->名称补查（用于跟踪不在当日池中的个股）。通过K线的股票代码列无名称，
    这里用 akshare 实时快照兜底，失败则由调用方沿用已有名称。"""
    return {}


# ---------------- 盘中实时行情（腾讯源，避开东财限流） ----------------
_RT_CACHE = {"df": None, "ts": 0}


def limit_threshold(code: str) -> float:
    """按板块返回涨跌停幅度(%)。"""
    c = str(code).zfill(6)
    if c.startswith(("30", "68")):
        return C.LIMIT_GEM
    if c.startswith(("8", "4", "9")):
        return C.LIMIT_BJ
    return C.LIMIT_MAIN


def realtime_quotes(max_age_sec: int = 20) -> pd.DataFrame:
    """腾讯全市场实时快照，返回带 6位代码/涨跌幅zdf/最新价zxj/status 的 DataFrame。
    带短时缓存，避免同一次运行重复拉取。"""
    now = time.time()
    if _RT_CACHE["df"] is not None and (now - _RT_CACHE["ts"]) < max_age_sec:
        return _RT_CACHE["df"]
    try:
        raw = _retry(ak.stock_zh_a_spot_tx, _retries=2, _sleep=0.4)
    except Exception:
        return pd.DataFrame()
    if raw is None or raw.empty:
        return pd.DataFrame()
    df = raw.copy()
    df["代码"] = df["code"].astype(str).str[-6:].str.zfill(6)
    for c in ["zdf", "zxj", "zf", "hsl", "zd", "turnover", "volume"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    # 实时状态判定：优先用"现价==涨停/跌停价"精确封板判定，回退到涨跌幅阈值
    pct_field = C.LIMIT_MAIN  # 阈值兜底用

    def _status(r):
        z = r.get("zdf")
        if pd.isna(z):
            return "无数据"
        zxj, zd = r.get("zxj"), r.get("zd")
        th = limit_threshold(r["代码"])
        ratio = th / 100.0
        # 由 涨跌额 反推昨收，进而算涨跌停价
        if pd.notna(zxj) and pd.notna(zd):
            prev = float(zxj) - float(zd)
            if prev > 0:
                up_price = round(prev * (1 + ratio), 2)
                dn_price = round(prev * (1 - ratio), 2)
                if abs(float(zxj) - up_price) <= 0.011:
                    return "涨停"
                if abs(float(zxj) - dn_price) <= 0.011:
                    return "跌停"
        # 兜底：涨跌幅接近阈值（容差内）
        if z >= th - C.REALTIME_LIMIT_TOL:
            return "涨停"
        if z <= -th + C.REALTIME_LIMIT_TOL:
            return "跌停"
        return "上涨" if z > 0 else ("下跌" if z < 0 else "平盘")
    df["实时状态"] = df.apply(_status, axis=1)
    _RT_CACHE["df"] = df
    _RT_CACHE["ts"] = now
    return df


def realtime_of(code: str) -> dict:
    """取单只个股实时快照。"""
    df = realtime_quotes()
    if df.empty:
        return {}
    row = df[df["代码"] == str(code).zfill(6)]
    if row.empty:
        return {}
    r = row.iloc[0]
    return {
        "最新价": r.get("zxj"), "涨跌幅": r.get("zdf"), "振幅": r.get("zf"),
        "换手率": r.get("hsl"), "状态": r.get("实时状态"),
        "量比": r.get("lb"), "更新时间": dt.datetime.now().strftime("%H:%M:%S"),
    }


def realtime_market_breadth() -> dict:
    """用实时快照统计全市场涨跌停家数、涨跌家数（盘中情绪温度的市场广度）。"""
    df = realtime_quotes()
    if df.empty:
        return {}
    st = df["实时状态"].value_counts()
    return {
        "实时涨停": int(st.get("涨停", 0)),
        "实时跌停": int(st.get("跌停", 0)),
        "实时上涨": int(st.get("上涨", 0)),
        "实时下跌": int(st.get("下跌", 0)),
        "总样本": int(len(df)),
        "数据时点": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------------- 指定交易日统一行情口径 ----------------
def _status_by_pct(code: str, pct) -> str:
    """由涨跌幅推导涨跌停/涨跌状态（仅用于没有权威池/实时快照的兜底）。"""
    if pct is None or pd.isna(pct):
        return "无数据"
    th = limit_threshold(code)
    if float(pct) >= th - 0.15:
        return "涨停"
    if float(pct) <= -th + 0.15:
        return "跌停"
    return "上涨" if float(pct) > 0 else ("下跌" if float(pct) < 0 else "平盘")


def _live_snapshot_usable(date: str, mode: str) -> bool:
    """实时快照是否可代表指定日期。
    - 盘中模式：快照即当前交易日行情，可直接用。
    - 收盘模式：仅指定日期为今天且已过 15:10（收盘数据落库）时才用；
      历史日期必须用当天的涨跌停池或K线，不能拿今天的快照冒充。
    """
    if mode == "intraday":
        return True
    date = normalize_date(date)
    now = dt.datetime.now()
    if date != now.strftime("%Y%m%d"):
        return False
    return now.hour > 15 or (now.hour == 15 and now.minute >= 10)


def day_quotes(codes: list, date: str, mode: str = "close") -> dict:
    """返回指定交易日 T 的权威行情，保证【状态/涨跌幅/现价】同源一致。

    优先级：
      1) 收盘模式：T 日真实涨跌停池（状态与涨跌幅均为交易所收盘口径）
      2) 实时快照：盘中模式恒用；收盘模式仅当天 15:10 后使用
      3) K线兜底：仅当最后一根K线日期 == 指定日期才采用，
         避免腾讯K线尚未更新时误把 T-1 的涨跌幅当作 T 日（历史 bug 根因）
    """
    codes = list(dict.fromkeys(str(c).zfill(6) for c in codes))
    wanted = set(codes)
    date = normalize_date(date)
    out = {}

    # --- 1) 收盘池 ---
    pool_available = False
    if mode == "close":
        try:
            up, down = limit_up_pool(date), limit_down_pool(date)
        except Exception:
            up, down = pd.DataFrame(), pd.DataFrame()
        pool_available = bool(len(up) or len(down))
        for df, status in ((up, "涨停"), (down, "跌停")):
            if df is None or df.empty:
                continue
            for _, r in df.iterrows():
                code = str(r["代码"]).zfill(6)
                if code not in wanted:
                    continue
                out[code] = {
                    "涨跌幅": round(float(r["涨跌幅"]), 2) if pd.notna(r.get("涨跌幅")) else None,
                    "现价": float(r["最新价"]) if pd.notna(r.get("最新价")) else None,
                    "状态": status, "来源": "收盘涨跌停池",
                }

    # --- 2) 实时快照 ---
    rt_map = None
    if _live_snapshot_usable(date, mode):
        rt = realtime_quotes()
        if not rt.empty:
            rt_map = rt.set_index("代码")
    if rt_map is not None:
        for code in codes:
            if code in out:
                continue  # 涨跌停池优先（收盘口径）
            if code not in rt_map.index:
                continue
            r = rt_map.loc[code]
            pct = round(float(r["zdf"]), 2) if pd.notna(r.get("zdf")) else None
            # 收盘模式且池可用时，状态以涨跌幅推导，避免快照瞬时状态与池口径打架
            status = _status_by_pct(code, pct) if (mode == "close" and pool_available) \
                else r.get("实时状态", _status_by_pct(code, pct))
            out[code] = {
                "涨跌幅": pct,
                "现价": float(r["zxj"]) if pd.notna(r.get("zxj")) else None,
                "状态": status,
                "来源": "盘中实时(腾讯)" if mode == "intraday" else "收盘实时(腾讯)",
            }

    # --- 3) K线兜底（严格校验日期，绝不使用过期K线） ---
    missing = [c for c in codes if c not in out]
    if missing:
        klines = daily_kline_batch(missing, date, lookback_days=10)
        for code in missing:
            k = klines.get(code)
            if k is None or k.empty or len(k) < 2:
                continue
            if normalize_date(str(k["日期"].iloc[-1])) != date:
                continue  # K线尚未更新到指定日：宁可留空，也不拿 T-1 冒充 T
            price = float(k["收盘"].iloc[-1])
            pct = round((price / float(k["收盘"].iloc[-2]) - 1) * 100, 2)
            out[code] = {
                "涨跌幅": pct, "现价": price,
                "状态": _status_by_pct(code, pct), "来源": "收盘K线",
            }

    for code in codes:
        out.setdefault(code, {"涨跌幅": None, "现价": None, "状态": "无数据", "来源": "无数据"})
    return out
