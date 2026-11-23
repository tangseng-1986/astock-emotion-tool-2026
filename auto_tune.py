# -*- coding: utf-8 -*-
"""自动调参引擎 v6：把回测的参数敏感性结论闭环应用到主程序。

设计原则（避免过拟合）：
  1. 主线周期 = config.HOLD_HORIZON（T+20，约1个月），兼顾 T+5/T+10。
  2. 分级可买等级：用"期望值 = 平均收益% × 胜率%"并按样本数做置信度折扣，
     维护每级评分的 EMA 平滑（config.AUTO_TUNE_SMOOTH），样本不足的等级不据此决策。
  3. 权重方案：从回测的方案对比中选主周期期望最高的方案，逐项平滑到当前权重并归一化。
  4. 评分门槛：门槛扫描收益差异 < SCORE_INSENSITIVE_EPS 视为"不敏感"，
     则将门槛降到最低有效值（避免门槛误杀信号），并记录理由。
  5. 连续大跌/情绪过滤：按回测后验差异决定是否启用。
  6. 每次调参写入 tuned_params.json（主程序自动加载）与 tune_history.json（效果跟踪）。

所有结果仅用于模型参数相对比较，不构成投资建议。
"""
import json
import datetime as dt

import numpy as np

import config as C


SCORE_INSENSITIVE_EPS = 2.0   # 门槛间收益差<2%视为不敏感
MIN_REL_GAIN = 0.0             # 期望值需>0才纳入可买


def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _read(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)


def _ema(old, new, a=None):
    """指数平滑：new值权重 a。"""
    a = C.AUTO_TUNE_SMOOTH if a is None else a
    if old is None:
        return float(new)
    return round(float(old) * (1 - a) + float(new) * a, 4)


def _stat_expect(stat: dict, horizon: int) -> tuple:
    """由 _stats 结果算 (期望值, 样本数, 收益, 胜率)。样本不足做置信折扣。"""
    if not stat:
        return 0.0, 0, None, None
    n = int(stat.get("样本数", 0))
    eff = stat.get("各周期有效样本", {}).get(f"T+{horizon}", 0)
    ret = stat.get(f"T+{horizon}平均收益%")
    win = stat.get(f"T+{horizon}胜率%")
    if ret is None or win is None or eff <= 0:
        return 0.0, n, ret, win
    # 置信折扣：有效样本越多越接近原始期望
    conf = min(1.0, eff / max(C.MIN_GRADE_SAMPLES, 1))
    expect = (ret * win / 100.0) * conf
    return round(expect, 3), n, ret, win


def _normalize_weights(w: dict) -> dict:
    total = sum(float(v) for v in w.values()) or 1
    return {k: round(float(v) / total * 100, 1) for k, v in w.items()}


def load_tuned_params() -> dict:
    """主程序调用：加载已调参结果；未启用/不存在则返回空 dict。"""
    if not getattr(C, "AUTO_TUNE_ENABLED", True):
        return {}
    p = _read(C.TUNED_PARAMS_FILE, {})
    return p.get("params", {}) if isinstance(p, dict) else {}


def should_retune() -> bool:
    """是否到了定期重调参时间（按自然日间隔）。"""
    if not getattr(C, "AUTO_TUNE_ENABLED", True):
        return False
    p = _read(C.TUNED_PARAMS_FILE, {})
    ts = p.get("tuned_at", "")
    if not ts:
        return True
    try:
        last = dt.datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return True
    return (dt.datetime.now() - last).days >= getattr(C, "AUTO_TUNE_INTERVAL_DAYS", 7)


def derive_params(bt: dict) -> dict:
    """从回测结果推导参数建议（不含平滑，纯结论）。"""
    hz = C.HOLD_HORIZON
    decisions = []
    params = {"hold_horizon": hz}

    # ---- 1) 分级可买等级 ----
    grade_scores = {}
    for trend, meta in C.W2S_Trend_TYPES.items():
        stat = bt.get("trend_split", {}).get(trend, {})
        exp, n, ret, win = _stat_expect(stat, hz)
        grade = meta["grade"]
        grade_scores[grade] = {
            "trend": trend, "label": meta["label"], "expect": exp,
            "samples": n, "ret": ret, "win": win,
            "sample_ok": n >= C.MIN_GRADE_SAMPLES,
        }
    # 结合短周期期望，避免单一周期偶然
    combined = {}
    for g, v in grade_scores.items():
        exp20 = v["expect"]
        exp5 = _stat_expect(bt.get("trend_split", {}).get(v["trend"], {}), 5)[0]
        combined[g] = round(0.3 * exp5 + 0.7 * exp20, 3)  # 中线为主
    # 只启用"样本充足 且 期望>0"的等级（样本不足者不据此决策，避免小样本误导）
    candidates = {g: s for g, s in grade_scores.items()
                  if s["sample_ok"] and combined[g] > MIN_REL_GAIN}
    insufficient = [g for g, s in grade_scores.items() if not s["sample_ok"] and combined[g] > MIN_REL_GAIN]
    if candidates:
        buyable = tuple(sorted(candidates.keys()))
        params["buyable_grades"] = buyable
        decisions.append(
            f"分级可买={'/'.join(buyable)}；主周期T+{hz}期望(收益×胜率×置信)："
            + "，".join(f"{g}级{combined[g]:.1f}(样本{grade_scores[g]['samples']})" for g in sorted(combined))
        )
        if insufficient:
            decisions.append(f"样本不足的等级暂不纳入（需≥{C.MIN_GRADE_SAMPLES}样本）："
                             + "、".join(f"{g}级(样本{grade_scores[g]['samples']})" for g in insufficient))
    else:
        params["buyable_grades"] = C.W2S_BUYABLE_GRADES
        decisions.append("各等级样本不足或期望非正，维持默认可买等级 " + "/".join(C.W2S_BUYABLE_GRADES))
    params["_grade_scores"] = grade_scores
    params["_grade_combined"] = combined

    # ---- 2) 评分门槛敏感性 ----
    th = bt.get("threshold_sensitivity", [])
    vals = [(r.get("门槛"), r.get(f"T+{hz}平均收益%")) for r in th
            if r.get(f"T+{hz}平均收益%") is not None]
    rets = [v for _, v in vals]
    if len(rets) >= 2:
        spread = max(rets) - min(rets)
        if spread < SCORE_INSENSITIVE_EPS:
            params["score_watch_min"] = 0
            params["score_signal_min"] = 0
            decisions.append(f"评分门槛不敏感（各门槛T+{hz}收益极差{spread:.1f}%<{SCORE_INSENSITIVE_EPS}%），"
                             "取消门槛过滤，避免误杀信号")
        else:
            best = max(vals, key=lambda x: x[1])
            params["score_watch_min"] = int(best[0])
            params["score_signal_min"] = int(best[0])
            decisions.append(f"评分门槛敏感（收益极差{spread:.1f}%），选最优门槛={best[0]}")

    # ---- 3) 权重方案 ----
    ws = bt.get("weight_sensitivity", {})
    scheme_score = {}
    for name, r in ws.items():
        exp, _, _, _ = _stat_expect(
            {"样本数": r.get("过门槛样本", 0),
             f"T+{hz}平均收益%": r.get(f"过门槛T+{hz}%"),
             f"T+{hz}胜率%": r.get(f"过门槛T+{hz}胜率%")}, hz)
        scheme_score[name] = exp
    # 自定义权重始终保留供参考
    named = {k: v for k, v in scheme_score.items() if k != "自定义权重"}
    if named:
        best_scheme = max(named, key=lambda k: named[k])
        params["_best_scheme"] = best_scheme
        params["_scheme_scores"] = scheme_score
        decisions.append(f"权重方案主周期期望最高：{best_scheme}（{named[best_scheme]:.1f}）")

    # ---- 4) 连续大跌过滤 ----
    cd = bt.get("consecutive_drop", {})
    yes = _stat_expect(cd.get("连续大跌后反转", {}), hz)[0]
    no = _stat_expect(cd.get("非连续大跌反转", {}), hz)[0]
    params["breakdown_filter"] = True  # 用户明确的风控要求：破位股剔除观察池，不因回测波动放宽
    decisions.append(f"连续大跌破位过滤：强制保留（用户风控要求）；回测参考=破位组期望{yes:.1f} vs 非破位{no:.1f}，"
                     "本期不做自动放宽")

    # ---- 5) 情绪分位过滤（低情绪样本充足时才启用） ----
    es = bt.get("emotion_split", {})
    low_n = es.get("低情绪", {}).get("样本数", 0)
    hi = _stat_expect(es.get("高情绪", {}), hz)[0]
    lo = _stat_expect(es.get("低情绪", {}), hz)[0]
    params["emotion_filter"] = bool(low_n >= C.TUNE_MIN_SAMPLE and hi > lo)
    decisions.append(f"情绪分位过滤={'启用' if params['emotion_filter'] else '不启用'}"
                     f"（高情绪期望{hi:.1f} vs 低情绪{lo:.1f}，低情绪样本{low_n}）")

    # ---- 6) 回测效果摘要（历史跟踪用） ----
    ov = bt.get("overall", {})
    params["_effect"] = {
        "period": bt.get("period"), "samples": ov.get("样本数"),
        f"T+{hz}收益": ov.get(f"T+{hz}平均收益%"),
        f"T+{hz}胜率": ov.get(f"T+{hz}胜率%"),
        "overall_expect": _stat_expect(ov, hz)[0],
    }
    params["decisions"] = decisions  # 供 tune() 写入文档
    return params


def apply_smoothing(suggest: dict, prev: dict) -> dict:
    """把建议参数与上一次结果平滑，输出最终 params（含可下钻的证据字段）。"""
    out = {}
    # 权重：按最优方案给目标，再平滑
    scheme = suggest.get("_best_scheme")
    target_w = None
    if scheme and scheme in {"趋势优先", "题材优先", "动能优先", "当前权重"}:
        target_w = {
            "趋势优先": {"trend": 50, "position": 10, "limit_quality": 15, "theme": 10, "short_momentum": 15},
            "题材优先": {"trend": 15, "position": 10, "limit_quality": 15, "theme": 45, "short_momentum": 15},
            "动能优先": {"trend": 15, "position": 10, "limit_quality": 15, "theme": 10, "short_momentum": 50},
            "当前权重": dict(C.WEIGHTS),
        }[scheme]
    prev_w = (prev or {}).get("weights", {})
    if target_w:
        blended = {k: _ema(prev_w.get(k, C.WEIGHTS[k]), target_w[k]) for k in C.WEIGHTS}
        out["weights"] = _normalize_weights(blended)
    else:
        out["weights"] = _normalize_weights(prev_w or C.WEIGHTS)

    # 可买等级：用每级期望 EMA 维护分数，取期望>0 的等级（平滑体现在分数）
    prev_scores = (prev or {}).get("_grade_score_ema", {})
    combined = suggest.get("_grade_combined", {})
    ema_scores = {}
    for g, v in combined.items():
        ema_scores[g] = _ema(prev_scores.get(g), v)
    out["_grade_score_ema"] = ema_scores
    buyable = tuple(sorted(g for g, s in ema_scores.items() if s > MIN_REL_GAIN))
    if not buyable:
        buyable = C.W2S_BUYABLE_GRADES
    out["buyable_grades"] = list(buyable)

    # 门槛：整数，直接平滑
    for key in ("score_watch_min", "score_signal_min"):
        if key in suggest:
            out[key] = int(round(_ema((prev or {}).get(key, C.SCORE_WATCH_MIN), suggest[key])))

    # 可买等级只允许"本次回测样本充足且期望为正"的等级，EMA仅用于分数平滑，不引入历史等级
    sample_ok = {g for g, s in suggest.get("_grade_scores", {}).items() if s.get("sample_ok")}
    buyable = tuple(sorted(g for g in buyable if g in sample_ok))
    if not buyable:
        buyable = tuple(g for g in C.W2S_BUYABLE_GRADES if g in sample_ok) or C.W2S_BUYABLE_GRADES
    out["buyable_grades"] = list(buyable)

    out["hold_horizon"] = int(suggest.get("hold_horizon", C.HOLD_HORIZON))
    # 连续大跌过滤是用户明确的风控要求（剔除短期破位股），不因回测波动而自动放宽
    out["breakdown_filter"] = True
    out["emotion_filter"] = bool(suggest.get("emotion_filter", False))
    # 证据字段（不参与平滑）
    for k in ("_grade_scores", "_grade_combined", "_scheme_scores", "_best_scheme", "_effect"):
        if k in suggest:
            out[k] = suggest[k]
    return out


def tune(bt: dict, force: bool = False) -> dict:
    """主调参入口：从回测结果推导->平滑->落盘->记录历史。返回最终 tuned 文档。"""
    if "error" in bt:
        return {"error": bt["error"]}
    suggest = derive_params(bt)
    n = bt.get("overall", {}).get("样本数", 0)
    if n < C.TUNE_MIN_SAMPLE and not force:
        return {"skipped": True, "reason": f"样本不足({n}<{C.TUNE_MIN_SAMPLE})，跳过调参"}
    prev_doc = _read(C.TUNED_PARAMS_FILE, {})
    final_params = apply_smoothing(suggest, prev_doc.get("params", {}))

    now = _now()
    doc = {
        "version": 6,
        "tuned_at": now,
        "based_on": {"period": bt.get("period"), "samples": n,
                     "trade_days": bt.get("trade_days"), "universe": bt.get("universe_size")},
        "params": final_params,
        "decisions": suggest.get("decisions", []),
        "suggest": {k: v for k, v in suggest.items() if k.startswith("_") and k != "_grade_scores"},
        "caveat": "自动调参基于K线近似回测的相对比较，样本与置信度已折扣，仅供参考，不构成投资建议。",
    }
    _write(C.TUNED_PARAMS_FILE, doc)

    # 历史跟踪（保留最近30次）
    hist = _read(C.TUNE_HISTORY_FILE, [])
    hist.append({
        "tuned_at": now, "period": bt.get("period"),
        "params": {k: v for k, v in final_params.items() if not k.startswith("_")},
        "effect": final_params.get("_effect", {}),
        "decisions": suggest.get("decisions", []),
    })
    _write(C.TUNE_HISTORY_FILE, hist[-30:])
    return doc


def tune_history() -> list:
    return _read(C.TUNE_HISTORY_FILE, [])


if __name__ == "__main__":
    import sys
    import backtest as B
    end = sys.argv[1] if len(sys.argv) > 1 else None
    print("[auto_tune] 运行回测作为调参依据 ...")
    bt = B.run_backtest(end, verbose=True)
    doc = tune(bt, force=True)
    if "error" in doc or doc.get("skipped"):
        print("[auto_tune]", doc)
    else:
        print(f"[auto_tune] 已写入 {C.TUNED_PARAMS_FILE}")
        print(f"[auto_tune] 可买等级: {doc['params']['buyable_grades']}")
        print(f"[auto_tune] 权重: {doc['params']['weights']}")
        for d in doc["decisions"]:
            print("  -", d)
