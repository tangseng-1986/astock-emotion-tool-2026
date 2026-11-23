# -*- coding: utf-8 -*-
"""A股市场情绪强弱分析工具 - 主入口。

时序口径：待选池=T-1涨跌停；观察池=待选池趋势过滤后叠加T日盘中实时状态。

用法：
  python main.py                          # 自动：盘中实时 / 收盘复盘
  python main.py --mode close             # 收盘复盘模式
  python main.py --mode intraday          # 强制盘中实时模式
  python main.py --date 20261118          # 指定观察日复盘
  python main.py --backtest               # 附带运行回测
  python main.py --weights '{"trend":50}' # 覆盖权重
"""
import argparse
import json
import os
import datetime as dt

os.environ.setdefault("TQDM_DISABLE", "1")
try:
    import akshare.utils.tqdm as _ak_tqdm
    _ak_tqdm.get_tqdm = lambda *a, **k: (lambda it, **kw: it)
except Exception:
    pass

import config as C
import data_fetcher as D
import analyzer as A
import storage as S
import tracking as TRK
import report as R


def _resolve_mode(mode_arg: str, date_arg: str) -> str:
    if mode_arg in ("intraday", "close"):
        return mode_arg
    if date_arg:
        return "close"
    now = dt.datetime.now()
    t = now.hour * 60 + now.minute
    return "intraday" if (9 * 60 + 30) <= t < (15 * 60) else "close"


def _resolve_date(mode: str, date_arg: str) -> str:
    if date_arg:
        return D.normalize_date(date_arg)
    today = dt.date.today().strftime("%Y%m%d")
    if mode == "intraday":
        cal = D.trade_calendar()
        return today if (today in cal or not cal) else D.latest_trade_date()
    return D.latest_trade_date()


def run(obs_date: str = None, mode: str = "auto", do_backtest: bool = False,
        weights: dict = None, track: bool = True, do_tune: bool = False,
        use_tuned: bool = True):
    # track 参数已废弃（v5 起第六项跟踪表由回顾池现算），保留以兼容旧命令
    mode = _resolve_mode(mode, obs_date)
    obs_date = _resolve_date(mode, obs_date)
    print(f"[1/6] 观察日 T={obs_date}  模式={mode}")
    state = S.load()

    # 参数优先级：命令行 --weights > 设置窗口(user_settings) > 自动调参(tuned_params) > config默认
    import auto_tune as T
    import settings as CFG
    user = CFG.load_user_only()  # 用户在设置窗口显式改过的项
    tuned = T.load_tuned_params() if (use_tuned and user.get("auto_tune_enabled", True)) else {}
    if do_tune and do_backtest:
        print("[调参] 基于回测结果自动重调参 ...")

    eff_weights = None
    buyable = None
    tuned_params = tuned or {}
    if tuned_params:
        eff_weights = tuned_params.get("weights")
        buyable = tuned_params.get("buyable_grades")
    # 设置窗口的权重覆盖自动调参
    user_weights = {k: user[f"weights.{k}"] for k in C.WEIGHTS if f"weights.{k}" in user}
    if user_weights:
        eff_weights = {**C.WEIGHTS, **user_weights}
    if "buyable_grades" in user:
        buyable = user["buyable_grades"]
    if weights:
        eff_weights = weights  # 命令行最高优先级

    params = dict(tuned_params)
    if buyable:
        params["buyable_grades"] = buyable
    if "hold_horizon" in user:
        params["hold_horizon"] = user["hold_horizon"]
    if eff_weights:
        print(f"[参数] 生效权重={eff_weights}，可买等级={params.get('buyable_grades')}")

    # 让用户设置的破位阈值临时作用于 config（本次进程）
    if "big_drop_pct" in user:
        C.BIG_DROP_PCT = float(user["big_drop_pct"])
    if "window_drop_count" in user:
        C.WINDOW_DROP_COUNT = int(user["window_drop_count"])
    if "window_cum_drop_pct" in user:
        C.WINDOW_CUM_DROP_PCT = float(user["window_cum_drop_pct"])
    if "up_slope_pct" in user:
        C.UP_SLOPE_PCT = float(user["up_slope_pct"]) / 100.0
    if "backtest_lookback" in user:
        C.BACKTEST_LOOKBACK_DAYS = int(user["backtest_lookback"])

    if mode == "close":
        # 主池（前瞻）：T 日收盘统计 -> 供 T+1 观察（待选池全量/分类统计/温度计/连板梯队）
        print("[2/6] 构建主池（当日收盘统计，供下一交易日观察）+ 回顾池（T-1构建->T表现）...")
        primary = A.build_pools(obs_date, mode="close", weights=eff_weights, forward=True)
        review = A.build_pools(obs_date, mode="close", weights=eff_weights)
        print(f"      主池：待选 {primary['candidate_count']} 只 -> 明日观察池 {primary['watch_count']} 只（供 {primary['next_date']}）")
        for k, v in primary["split"].items():
            print(f"        {k}: 待选{v['待选池']} -> 观察{v['纳入观察池']} ({v['纳入率']}%)")
        print(f"      回顾池：{review['base_date']} 构建 {review['candidate_count']} 只，今日留存观察 {review['watch_count']} 只")
    else:
        print("[2/6] 构建待选池(T-1) -> 观察池(T日盘中状态) ...")
        primary = A.build_pools(obs_date, mode="intraday", weights=eff_weights)
        review = None
        print(f"      待选池 {primary['candidate_count']} 只 / 观察池 {primary['watch_count']} 只")
        for k, v in primary["split"].items():
            print(f"        {k}: 待选{v['待选池']} -> 观察{v['纳入观察池']} ({v['纳入率']}%)")

    print("[3/6] 识别弱转强/A杀信号（趋势分级）...")
    signals = A.detect_signals(review if review is not None else primary, params=params)
    buyable_txt = "/".join(params.get("buyable_grades", C.W2S_BUYABLE_GRADES))
    print(f"      弱转强 {len(signals['weak_to_strong'])} 只（{buyable_txt}级可买 {signals.get('w2s_buyable',0)} 只）"
          f" / A杀 {len(signals['a_kill'])} 只")
    print(f"      弱转强趋势类型分布: {signals.get('w2s_type_count', {})}")

    score, phase = A.sentiment_score(primary["sentiment"], weights)
    primary["sentiment"]["_score"] = score
    primary["sentiment"]["_phase"] = phase

    # 先让达标弱转强信号进入自动自选池，再统一刷新全部在池个股的跟踪统计
    state = S.update_state(state, obs_date, primary, signals)
    try:
        trk = TRK.update_tracking(state, obs_date, mode=mode)
        print(f"[跟踪] 在池 {trk['active']} 只，更新 {trk['updated']} 只，"
              f"数据不足 {trk['failed']} 只（自动/手动自选池持续跟踪至剔除）")
    except Exception as e:
        print(f"[跟踪] 更新失败（不影响主流程）: {e}")
    S.save(state)

    bt = None
    tune_doc = T._read(C.TUNED_PARAMS_FILE, {}) if use_tuned else {}
    if do_backtest:
        print("[5/6] 运行回测（周期扩大，主线T+20）...")
        import backtest as B
        bt = B.run_backtest(obs_date, weights=eff_weights)
        if "error" not in bt:
            o = bt["overall"]
            hz_desc = " / ".join(
                f"T+{k} {o.get(f'T+{k}平均收益%')}%(胜{o.get(f'T+{k}胜率%')}%)"
                for k in C.BACKTEST_HORIZONS)
            print(f"      区间 {bt['period']}，弱转强样本 {o['样本数']} 个，{hz_desc}")
            # 回测即调参依据：按周期自动重算并落盘，下一次运行生效
            if use_tuned and (do_tune or T.should_retune()):
                td = T.tune(bt)
                if not td.get("error") and not td.get("skipped"):
                    tune_doc = td
                    print(f"[调参] 已更新参数：可买等级={td['params']['buyable_grades']}")
        else:
            print("      回测失败:", bt["error"])
    else:
        print("[5/6] 跳过回测（--backtest 开启）")

    print("[6/6] 生成 HTML 看板 ...")
    out = R.render(obs_date, primary, review, signals, state, score, phase, bt, tune_doc, params)
    print(f"      输出: {out}")

    payload = {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "obs_date": obs_date, "mode": mode, "pools": primary,
        "review_pools": review, "signals": signals,
        "sentiment_score": score, "sentiment_phase": phase,
        "watchlist": state.get("watchlist", {}),          # {"auto":{...},"manual":{...}}
        "watchlist_archive": state.get("watchlist_archive", {}),
        "tracking": state.get("tracking", {}),
        "backtest": bt,
        "tuned_params": tune_doc,
        "effective_params": params,
        "data_sources": {
            "待选池涨跌停/K线": "akshare(东方财富)",
            "盘中实时状态": "腾讯证券实时行情",
            "涨停原因": "同花顺 dataapi（非一手来源，需核实）",
        },
    }
    jpath = C.DATA_DIR / f"result_{obs_date}.json"
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    print(f"      数据: {jpath}")
    return payload


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--mode", default="auto", choices=["auto", "intraday", "close"])
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--weights", default=None, help='JSON, e.g. \'{"trend":50,"theme":10}\'')
    ap.add_argument("--no-track", action="store_true")
    ap.add_argument("--tune", action="store_true", help='强制基于回测重算自动调参')
    ap.add_argument("--no-tuned", action="store_true", help='禁用自动调参参数，用config默认值')
    args = ap.parse_args()
    w = json.loads(args.weights) if args.weights else None
    run(args.date, args.mode, args.backtest, w, track=not args.no_track,
        do_tune=args.tune, use_tuned=not args.no_tuned)
