# -*- coding: utf-8 -*-
"""用户可视化设置：读写 data/user_settings.json。

设置窗口修改的参数，主程序(main.py)与桌面监测(desktop_app.py)共同加载。
自动调参(auto_tune)的结果单独存 tuned_params.json，二者可叠加：
  - user_settings 是"用户手动偏好"，优先级最高
  - tuned_params 是"回测自动结论"，用户设置了同名项则以用户为准
"""
import json
import config as C

SETTINGS_FILE = C.DATA_DIR / "user_settings.json"

# 可在设置窗口调整的参数：key -> (中文名, 类型, 默认取值来源, 说明)
FIELDS = {
    "weights.trend":         ("趋势因子权重", "int", lambda: C.WEIGHTS["trend"], "斜率/均线强度，0-100"),
    "weights.position":      ("高位因子权重", "int", lambda: C.WEIGHTS["position"], "距3月高点位置"),
    "weights.limit_quality": ("封板质量权重", "int", lambda: C.WEIGHTS["limit_quality"], "连板高度/炸板"),
    "weights.theme":         ("题材热度权重", "int", lambda: C.WEIGHTS["theme"], "涨停原因题材数量"),
    "weights.short_momentum":("短期动能权重", "int", lambda: C.WEIGHTS["short_momentum"], "近5日收益/急跌惩罚"),
    "buyable_grades":        ("可买入等级", "list", lambda: list(C.W2S_BUYABLE_GRADES), "弱转强纳入自选的等级(A/B/C/D)"),
    "hold_horizon":          ("持有周期(交易日)", "int", lambda: C.HOLD_HORIZON, "主线持有天数，约20=1个月"),
    "breakdown_filter":      ("破位大跌过滤", "bool", lambda: True, "剔除连续/窗口破位大跌个股"),
    "big_drop_pct":          ("单日大跌阈值%", "float", lambda: C.BIG_DROP_PCT, "单日跌幅≥此值算大跌"),
    "window_drop_count":     ("窗口大跌次数", "int", lambda: C.WINDOW_DROP_COUNT, "窗口内大跌达此次数且累计跌幅达标则剔除"),
    "window_cum_drop_pct":   ("窗口累计跌幅%", "float", lambda: C.WINDOW_CUM_DROP_PCT, "窗口累计跌幅≤此值视为破位"),
    "up_slope_pct":          ("上升趋势斜率%", "float", lambda: C.UP_SLOPE_PCT*100, "60日斜率>此值算上升趋势"),
    "auto_tune_enabled":     ("启用自动调参", "bool", lambda: C.AUTO_TUNE_ENABLED, "主程序自动应用回测调参结论"),
    "refresh_interval_sec":  ("盘中刷新间隔(秒)", "int", lambda: C.REFRESH_INTERVAL_SEC, "盘中监测行情刷新频率"),
    "alert_popup":           ("信号弹窗提醒", "bool", lambda: C.MONITOR_ALERT_POPUP, "弱转强/A杀即时弹窗"),
    "alert_sound":           ("声音提醒", "bool", lambda: C.MONITOR_SOUND, "信号触发时播放提示音"),
    "backtest_lookback":     ("回测交易日数", "int", lambda: C.BACKTEST_LOOKBACK_DAYS, "回测覆盖交易日，越大样本越多"),
}


def defaults() -> dict:
    return {k: v[2]() for k, v in FIELDS.items()}


def load() -> dict:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            user = json.load(f)
    except Exception:
        user = {}
    d = defaults()
    d.update(user or {})
    return d


def load_user_only() -> dict:
    """仅返回用户显式修改过的项（用于判断是否覆盖自动调参）。"""
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save(d: dict):
    C.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def reset():
    save(defaults())


def to_runtime_params() -> dict:
    """把用户设置转换成主程序/分析层可用的运行时参数 dict。"""
    s = load()
    weights = {k: s[f"weights.{k}"] for k in C.WEIGHTS}
    return {
        "weights": weights,
        "buyable_grades": s.get("buyable_grades", list(C.W2S_BUYABLE_GRADES)),
        "hold_horizon": int(s.get("hold_horizon", C.HOLD_HORIZON)),
        "breakdown_filter": bool(s.get("breakdown_filter", True)),
        "big_drop_pct": float(s.get("big_drop_pct", C.BIG_DROP_PCT)),
        "window_drop_count": int(s.get("window_drop_count", C.WINDOW_DROP_COUNT)),
        "window_cum_drop_pct": float(s.get("window_cum_drop_pct", C.WINDOW_CUM_DROP_PCT)),
        "up_slope_pct": float(s.get("up_slope_pct", C.UP_SLOPE_PCT*100)) / 100.0,
        "auto_tune_enabled": bool(s.get("auto_tune_enabled", True)),
        "refresh_interval_sec": int(s.get("refresh_interval_sec", C.REFRESH_INTERVAL_SEC)),
        "alert_popup": bool(s.get("alert_popup", True)),
        "alert_sound": bool(s.get("alert_sound", True)),
        "backtest_lookback": int(s.get("backtest_lookback", C.BACKTEST_LOOKBACK_DAYS)),
    }
