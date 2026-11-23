# -*- coding: utf-8 -*-
"""跨日状态持久化：自动/手动双自选池、持续跟踪、剔除归档、信号历史、每日摘要。

v7 变化：
  - watchlist 由扁平 dict 升级为 {"auto": {...}, "manual": {...}}，旧 state 自动迁移
  - 新增 tracking（在池个股逐日走势统计）与 watchlist_archive（剔除后的终态归档）
  - "上一观察池今日表现"仍由 analyzer 的回顾池现算，不依赖跨日状态
"""
import datetime as dt
import json
import config as C


def _default_state():
    return {
        "last_date": None,
        "watchlist": {p: {} for p in C.WATCH_POOLS},  # pool -> code -> info
        "watchlist_archive": {},   # code -> 剔除时的快照与终态统计
        "tracking": {},            # code -> {info, stats, snapshots, status}
        "signals_history": [],
        "daily": {},
    }


def load() -> dict:
    try:
        with open(C.STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        return _default_state()
    d = _default_state()
    for k, v in d.items():
        st.setdefault(k, v)
    st.pop("prev_watch", None)  # 旧版本遗留字段
    # 清理历史重复信号（旧版本反复复盘会把同一条写几十次）
    seen, clean = set(), []
    for h in st.get("signals_history") or []:
        key = (h.get("date"), h.get("type"), h.get("code"))
        if key in seen:
            continue
        seen.add(key)
        clean.append(h)
    st["signals_history"] = clean

    # ---- 旧扁平 watchlist -> 双池迁移 ----
    wl = st.get("watchlist") or {}
    if wl and not any(isinstance(v, dict) and set(v.keys()) <= set(C.WATCH_POOLS) for v in [wl]):
        migrated = {p: {} for p in C.WATCH_POOLS}
        for code, info in wl.items():
            if not isinstance(info, dict):
                continue
            pool = info.get("pool", "manual" if info.get("manual") else "auto")
            if pool not in C.WATCH_POOLS:
                pool = "auto"
            rec = dict(info)
            rec["pool"] = pool
            rec.setdefault("add_date", st.get("last_date") or dt.date.today().strftime("%Y%m%d"))
            migrated[pool][code] = rec
        st["watchlist"] = migrated
    else:
        for p in C.WATCH_POOLS:
            st["watchlist"].setdefault(p, {})
    return st


def save(state: dict):
    C.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(C.STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, default=str)


def today_str() -> str:
    return dt.date.today().strftime("%Y%m%d")


def iter_watch(state: dict, pool: str = None):
    """遍历在池个股：yield (pool, code, info)。"""
    pools = (pool,) if pool in C.WATCH_POOLS else C.WATCH_POOLS
    for p in pools:
        for code, info in (state.get("watchlist", {}).get(p) or {}).items():
            yield p, str(code).zfill(6), info


def find_pool(state: dict, code: str):
    code = str(code).zfill(6)
    for p in C.WATCH_POOLS:
        if code in (state.get("watchlist", {}).get(p) or {}):
            return p
    return None


def add_watch(state: dict, pool: str, code: str, info: dict = None, date: str = None):
    """加入自选池；同一只股票只保留在一个池（若原在另一池则迁移，跟踪记录保留）。

    返回 (ok, msg, record)。
    """
    if pool not in C.WATCH_POOLS:
        return False, f"未知自选池：{pool}", None
    code = str(code or "").strip().zfill(6)
    if len(code) != 6 or not code.isdigit():
        return False, "股票代码需为6位数字", None
    info = dict(info or {})
    state.setdefault("watchlist", {p: {} for p in C.WATCH_POOLS})
    for p in C.WATCH_POOLS:
        state["watchlist"].setdefault(p, {})

    # 跨池迁移：保留原入池日期/成本，避免跟踪统计被重置
    for p in C.WATCH_POOLS:
        if p != pool and code in state["watchlist"][p]:
            old = state["watchlist"][p].pop(code)
            info = {**old, **info}
    existing = state["watchlist"][pool].get(code, {})
    rec = {**existing, **info}
    rec.update({"pool": pool, "add_date": info.get("add_date") or existing.get("add_date") or date or today_str()})
    rec.setdefault("名称", code)
    rec.setdefault("所属行业", "—")
    rec.setdefault("source", "手动添加")
    if date and "add_date" not in info and not existing.get("add_date"):
        rec["add_date"] = date
    state["watchlist"][pool][code] = rec

    tr = state.setdefault("tracking", {}).setdefault(code, {})
    tr.update({"code": code, "name": rec.get("名称", code), "pool": pool,
               "add_date": rec["add_date"], "status": "active"})
    return True, f"{rec.get('名称', code)}（{code}）已加入{C.POOL_LABELS[pool]}", rec


def remove_watch(state: dict, pool: str, code: str, reason: str = "手动剔除", date: str = None):
    """手动剔除：从在池移除，保存剔除时终态到归档，并停止后续自动跟踪。

    返回 (ok, msg, archive_entry)。
    """
    code = str(code).strip().zfill(6)
    rec = (state.get("watchlist", {}).get(pool) or {}).pop(code, None)
    if rec is None:
        return False, f"{code} 不在{C.POOL_LABELS.get(pool, '自选池')}中", None
    tr = state.setdefault("tracking", {}).get(code, {})
    entry = {**rec, "pool": pool, "remove_date": date or state.get("last_date") or today_str(),
             "remove_reason": reason,
             "final_stats": json.loads(json.dumps(tr.get("stats") or {}, default=str))}
    state.setdefault("watchlist_archive", {})[code] = entry
    if tr:
        tr["status"] = "removed"
        tr["remove_date"] = entry["remove_date"]
    return True, f"{rec.get('名称', code)}（{code}）已从{C.POOL_LABELS[pool]}剔除，走势已归档", entry


def remove_archive(state: dict, code: str):
    """从已剔除归档中彻底删除记录（同时清理对应跟踪数据）。"""
    code = str(code or "").strip().zfill(6)
    entry = (state.get("watchlist_archive") or {}).pop(code, None)
    if entry is None:
        return False, f"{code} 不在归档中", None
    (state.get("tracking") or {}).pop(code, None)
    return True, f"{entry.get('名称', code)}（{code}）的归档记录已删除", entry


def update_state(state: dict, obs_date: str, today_pools: dict, signals: dict) -> dict:
    """用当日结果推进状态：弱转强达标信号自动进入自动自选池；其余只记信号历史。"""
    # 同一交易日重复复盘不应把相同信号反复写入历史（曾经一次复盘写入几十条）
    seen = {(h.get("date"), h.get("type"), h.get("code"))
            for h in state.get("signals_history", [])}

    def _log_signal(sig_type, s, detail):
        key = (obs_date, sig_type, s.get("代码"))
        if key in seen:
            return
        seen.add(key)
        state["signals_history"].append({
            "date": obs_date, "type": sig_type, "code": s.get("代码"),
            "name": s.get("名称"), "detail": detail,
        })

    for s in signals.get("weak_to_strong", []):
        _log_signal("弱转强", s, s.get("信号", ""))
        if not s.get("纳入自选池", False):
            continue
        add_watch(state, "auto", s["代码"], {
            "名称": s.get("名称", ""), "所属行业": s.get("所属行业", "—"),
            "add_date": obs_date, "source": s.get("信号", "弱转强打板信号"),
            "signal": s.get("信号", ""), "等级": s.get("等级", ""),
            "信号类型": s.get("信号类型", ""), "综合评分": s.get("综合评分"),
            "trend": s.get("趋势", ""), "add_price": s.get("纳入价格"),
            "持有周期": s.get("持有周期"), "buyable_grades": s.get("可买等级"),
        }, date=obs_date)
    for s in signals.get("a_kill", []):
        _log_signal("A杀", s, s.get("提示", ""))

    state["daily"][obs_date] = {
        "sentiment": today_pools["sentiment"],
        "candidate_count": today_pools["candidate_count"],
        "watch_count": today_pools["watch_count"],
        "split": today_pools["split"],
        "strong_count": len(signals.get("weak_to_strong", [])),
        "strong_buyable": signals.get("w2s_buyable", 0),
        "w2s_type_count": signals.get("w2s_type_count", {}),
        "akill_count": len(signals.get("a_kill", [])),
        "watch_auto": len(state["watchlist"].get("auto", {})),
        "watch_manual": len(state["watchlist"].get("manual", {})),
    }
    state["last_date"] = obs_date
    save(state)
    return state
