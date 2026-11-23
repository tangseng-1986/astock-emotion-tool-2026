# -*- coding: utf-8 -*-
"""HTML 看板渲染：浅底深字研报风，红涨绿跌。所有 JS 数据用 json.dumps 注入。
新增：参数敏感性可视化 + 图表点击下钻联动股票池。"""
import json
import datetime as dt
import config as C


def _esc(v):
    if v is None:
        return "—"
    return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cls(v):
    try:
        return "up" if float(v) >= 0 else "down"
    except Exception:
        return ""


def _span(v, suffix="%"):
    if v is None:
        return '<span class="na">—</span>'
    return f'<span class="{_cls(v)}">{float(v):+.2f}{suffix}</span>'


def _table(headers, rows):
    if not rows:
        return '<div class="empty">无数据</div>'
    th = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    trs = "".join(f"<tr>{''.join(f'<td>{c}</td>' for c in r)}</tr>" for r in rows)
    return f'<div class="tw"><table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div>'


def _tag(text, kind=""):
    return f'<span class="tag {kind}">{_esc(text)}</span>'


def _status_tag(s):
    k = "tup" if s == "涨停" else ("tdown" if s == "跌停" else ("thigh" if s in ("上涨", "近涨停") else ""))
    return _tag(s, k)


def _grade_tag(g):
    k = {"A": "ga", "B": "gb", "C": "gc", "D": "gd"}.get(g, "")
    return _tag(f"{g}级", k)


def _stat_cards(items):
    return "".join(f'<div class="kpi"><div class="v" style="color:{color}">{value}</div><div class="l">{label}</div></div>'
                   for label, value, color in items)


def _dict_rows(items, fields):
    """构造下钻用行数据（dict 列表）。"""
    return [{k: (v if v is not None else "—") for k, v in zip(fields, row)} for row in items]


def _js_arg(v):
    """Python 值 -> JS 字符串字面量（安全转义）。"""
    return json.dumps("" if v is None else str(v), ensure_ascii=False)


def _add_btn(w, add_date, in_pool):
    """观察池行内「加入自选」按钮：通过本地控制服务写入手动自选池。"""
    if in_pool:
        return '<button class="wbtn" disabled title="该股已在自动/手动自选池">✓已在池</button>'
    args = ",".join(_js_arg(x) for x in (
        w.get("代码", ""), w.get("名称", ""), w.get("所属行业", ""), add_date,
        w.get("trend", ""), w.get("综合评分"), w.get("T日现价") or w.get("price")))
    return f'<button class="wbtn" onclick=\'wlAdd({args})\'>＋加自选</button>'


def _stat_span(stats, key):
    return _span((stats or {}).get(key))


def watch_panel_html(state: dict, buyable=None) -> str:
    """第五项「双自选池 + 持续跟踪」的可刷新片段。

    看板生成时用它渲染静态初值；桌面应用控制服务也用它返回最新 state 的 HTML，
    前端定时替换，避免看板停留在复盘生成那一刻的旧快照。
    """
    tracking = state.get("tracking", {}) or {}
    archive = state.get("watchlist_archive", {}) or {}
    _wl = state.get("watchlist", {}) or {}
    wl_auto = _wl.get("auto", {}) or {}
    wl_manual = _wl.get("manual", {}) or {}
    buyable = set(buyable or [])
    TRK_HEAD = ["代码", "名称", "细分行业", "入池来源", "纳入日期", "持有交易日",
                "累计收益", "区间最高", "最大回撤"] + [f"T+{h}收益" for h in C.TRACK_HORIZONS] + ["最新价", "最新状态", "操作"]

    def _src(v, pool):
        text = _esc(v.get("source", v.get("signal", "—")))
        if pool == "auto":
            grade = str(v.get("等级", "") or "").upper()
            old = v.get("buyable_grades") or []
            if buyable and grade and grade not in buyable:
                text += " " + _tag("旧规则入池", "tdown")
            elif old and buyable and set(old) != buyable:
                text += " " + _tag("规则已变", "")
        return text

    def _trk_rows(items, pool):
        rows = []
        for code, v in sorted(items, key=lambda kv: kv[1].get("add_date", ""), reverse=True):
            tr = tracking.get(code, {}) or {}
            st = tr.get("stats") or {}
            op = (f'<button class="wbtn" onclick=\'wlRemove({_js_arg(pool)},{_js_arg(code)},'
                  f'{_js_arg(v.get("名称", code))})\'>剔除</button>')
            rows.append([
                f'<a class="codelink" href=\'javascript:showDrill({_js_arg("跟踪_"+code)},'
                f'{_js_arg(v.get("名称", code)+" · 入池后走势")})\'>{_esc(code)}</a>',
                _esc(v.get("名称", code)), _esc(v.get("所属行业", "—")),
                _src(v, pool), _esc(v.get("add_date", "—")),
                _esc(st.get("持有交易日", "—")), _stat_span(st, "累计收益%"),
                _stat_span(st, "区间最高收益%"), _stat_span(st, "区间最大回撤%"),
            ] + [_stat_span(st, f"T+{h}收益%") for h in C.TRACK_HORIZONS]
              + [_esc(st.get("最新价", "—")),
                 _status_tag(st.get("最新状态", "—")) if st.get("最新状态") not in (None, "—") else "—",
                 op])
        return rows

    auto_tbl = _table(TRK_HEAD, _trk_rows(list(wl_auto.items()), "auto"))
    manual_tbl = _table(TRK_HEAD, _trk_rows(list(wl_manual.items()), "manual"))
    arch_head = ["代码", "名称", "所属池", "纳入日期", "剔除日期", "剔除原因", "持有交易日",
                 "累计收益"] + [f"T+{h}收益" for h in C.TRACK_HORIZONS] + ["操作"]
    arch_rows = []
    for code, v in sorted(archive.items(), key=lambda kv: kv[1].get("remove_date", ""), reverse=True):
        st = (tracking.get(code, {}).get("final_stats") or v.get("final_stats") or {})
        arch_rows.append([_esc(code), _esc(v.get("名称", code)),
                          _esc(C.POOL_LABELS.get(v.get("pool"), "—")),
                          _esc(v.get("add_date", "—")), _esc(v.get("remove_date", "—")),
                          _esc(v.get("remove_reason", "手动剔除")),
                          _esc(st.get("持有交易日", "—")), _stat_span(st, "累计收益%")]
                         + [_stat_span(st, f"T+{h}收益%") for h in C.TRACK_HORIZONS]
                         + [f'<button class="wbtn" onclick=\'wlArchiveRemove({_js_arg(code)},'
                            f'{_js_arg(v.get("名称", code))})\'>删除归档</button>'])
    arch_tbl = _table(arch_head, arch_rows)
    tracked_n = sum(1 for c in set(wl_auto) | set(wl_manual)
                    if (tracking.get(c, {}).get("stats") or {}).get("累计收益%") is not None)
    cards = _stat_cards([
        ("自动自选池", len(wl_auto), "#c00000"), ("手动自选池", len(wl_manual), "#2b5a9e"),
        ("已有走势统计", tracked_n, "#e08c00"), ("已剔除归档", len(archive), "#0a8f5b"),
    ])
    note = ("自动池由达到可买等级的弱转强信号纳入；手动池由看板第三项「＋加自选」或桌面应用「自选池管理」添加。"
            "两池个股每日更新累计收益、区间最高/回撤与 T+5/T+10/T+20 走势，<b>直至手动剔除</b>；"
            "剔除后终态移入归档，不再继续跟踪；归档记录可在操作列彻底删除。点击代码可查看逐日快照。")
    if buyable:
        note += (f" 当前生效可买等级 <b>{'/'.join(sorted(buyable))}</b>；"
                 "自动池中标注「旧规则入池」的条目按入池时规则保留，不会因调参自动剔除，如需停止请手动剔除。")
    return (f'<div class="kpis">{cards}</div>'
            f'<div class="note">{note}</div>'
            f'<div class="sub">① 自动自选池（弱转强信号，{len(wl_auto)} 只）</div>{auto_tbl}'
            f'<div class="sub">② 手动自选池（看板/桌面手动加入，{len(wl_manual)} 只）</div>{manual_tbl}'
            f'<div class="sub">③ 已剔除归档（{len(archive)} 只，停止跟踪并保存终态）</div>{arch_tbl}')


def render(obs_date, primary, review, signals, state, score, phase, bt=None,
           tune_doc=None, params=None):
    """渲染看板。
    primary：主池。收盘模式=前瞻池（T日收盘统计，供T+1观察）；
             盘中模式=回顾池（T-1待选池 + T日盘中状态）。
    review ：回顾池（仅收盘模式）：T-1收盘构建 -> T日表现，用于第六项跟踪表与信号。
    tune_doc / params：自动调参文档与已生效参数，用于看板"自动调参"面板。
    """
    tune_doc = tune_doc or {}
    params = params or {}
    buyable_txt = "/".join(params.get("buyable_grades", C.W2S_BUYABLE_GRADES) or []) or "—"
    s = primary["sentiment"]
    mode = primary["mode"]
    base = primary["base_date"]
    split = primary["split"]
    weights = primary.get("weights_used", C.WEIGHTS)
    # 真实日期标签：把相对口径 T / T-1 落到具体交易日
    date_md = f"{obs_date[4:6]}-{obs_date[6:8]}"
    base_md = f"{base[4:6]}-{base[6:8]}"
    next_date = primary.get("next_date") or obs_date
    next_md = f"{next_date[4:6]}-{next_date[6:8]}"
    next_cn = f"{next_date[:4]}-{next_date[4:6]}-{next_date[6:8]}"
    # 信号口径日（回顾池的待选池来源日）：弱转强/A杀 = 该日涨跌停 -> T日状态
    sig_base = review["base_date"] if review is not None else base
    sig_base_md = f"{sig_base[4:6]}-{sig_base[6:8]}"
    is_close = review is not None
    # 章节编号：收盘模式含"六、观察池跟踪"，盘中模式无（观察池本身即实时跟踪）
    _CN = ["一", "二", "三", "四", "五", "六", "七", "八"]
    no_track = 6 if is_close else None
    no_cand = 7 if is_close else 6
    no_bt = 8 if is_close else 7

    board = s.get("连板梯队", {})
    js_board = json.dumps([{"name": f"{k}板", "value": v} for k, v in board.items()], ensure_ascii=False)

    sv = [split.get("涨停", {}).get("待选池", 0), split.get("涨停", {}).get("纳入观察池", 0),
          split.get("跌停", {}).get("待选池", 0), split.get("跌停", {}).get("纳入观察池", 0)]
    js_up_base, js_up_keep, js_dn_base, js_dn_keep = sv

    cands = sorted(primary["candidates"], key=lambda x: x["涨跌幅"], reverse=True)
    # 待选池全量：主池口径。收盘=当日涨跌停（供次日观察，无次日状态列）；盘中=T-1涨跌停+T日盘中状态
    if is_close:
        cand_rows = [[_esc(c["代码"]), _esc(c["名称"]),
                      _tag(c["类型"], "tup" if c["类型"] == "涨停" else "tdown"),
                      _span(c["涨跌幅"]), _esc(c["所属行业"]), _esc(c.get("连板数", 1)),
                      _esc(c["trend"]), _esc(c.get("综合评分")),
                      _esc(c.get("剔除原因", "")) or "—"] for c in cands]
        cand_tbl = _table(["代码", "名称", f"{base_md}状态", f"{base_md}涨跌", "细分行业", "连板", "趋势", "评分", "剔除原因"], cand_rows)
    else:
        cand_rows = [[_esc(c["代码"]), _esc(c["名称"]),
                      _tag(c["类型"], "tup" if c["类型"] == "涨停" else "tdown"),
                      _span(c["涨跌幅"]), _esc(c["所属行业"]), _esc(c.get("连板数", 1)),
                      _esc(c["trend"]), _esc(c.get("综合评分")),
                      _status_tag(c.get("T日状态")), _span(c.get("T日涨跌幅")),
                      _esc(c.get("剔除原因", "")) or "—"] for c in cands]
        cand_tbl = _table(["代码", "名称", f"{base_md}状态", f"{base_md}涨跌", "细分行业", "连板", "趋势", "评分", f"{date_md}状态", f"{date_md}涨跌", "剔除原因"], cand_rows)

    # 双自选池：自动（弱转强达标）/ 手动（看板或桌面界面添加）
    _wl = state.get("watchlist", {}) or {}
    wl_auto = _wl.get("auto", {}) or {}
    wl_manual = _wl.get("manual", {}) or {}
    wl_codes = set(wl_auto) | set(wl_manual)

    watch = sorted(primary["watch"], key=lambda x: (x.get("综合评分") or 0), reverse=True)
    # 第三项：收盘=明日观察池（当日收盘统计，无次日状态）；盘中=当前观察池（实时状态）
    if is_close:
        watch_rows = [[_esc(w["代码"]), _esc(w["名称"]),
                       _tag(w["类型"], "tup" if w["类型"] == "涨停" else "tdown"),
                       _span(w["涨跌幅"]), _esc(w["所属行业"]),
                       _tag(w["trend"], "tup" if w["trend"] == "上升趋势" else "thigh"),
                       _esc(w.get("综合评分")), _esc(w["原因标注"]),
                       _add_btn(w, obs_date, w["代码"] in wl_codes)] for w in watch]
        watch_tbl = _table(["代码", "名称", f"{base_md}状态", f"{base_md}涨跌", "细分行业", "趋势", "评分", "原因", "操作"], watch_rows)
    else:
        watch_rows = []
        for w in watch:
            tr = _tag(w["trend"], "tup" if w["trend"] == "上升趋势" else "thigh")
            watch_rows.append([_esc(w["代码"]), _esc(w["名称"]),
                               _tag(w["类型"], "tup" if w["类型"] == "涨停" else "tdown"),
                               _esc(w["所属行业"]), tr, _esc(w.get("综合评分")),
                               _status_tag(w.get("T日状态")), _span(w.get("T日涨跌幅")),
                               _esc(w["原因标注"]),
                               _add_btn(w, obs_date, w["代码"] in wl_codes)])
        watch_tbl = _table(["代码", "名称", f"{base_md}状态", "细分行业", "趋势", "评分", f"{date_md}实时状态", f"{date_md}涨跌幅", "原因", "操作"], watch_rows)

    # 第六项（仅收盘）：观察池跟踪 = T-1收盘构建的观察池在 T 日的实际表现
    # （原"观察池"表与"次日跟踪"表合并为此一张表）
    if is_close:
        track_watch = sorted(review["watch"], key=lambda x: (x.get("综合评分") or 0), reverse=True)
        track_rows = []
        for w in track_watch:
            tr = _tag(w["trend"], "tup" if w["trend"] == "上升趋势" else "thigh")
            track_rows.append([_esc(w["代码"]), _esc(w["名称"]),
                               _tag(w["类型"], "tup" if w["类型"] == "涨停" else "tdown"),
                               _esc(w["所属行业"]), tr, _esc(w.get("综合评分")),
                               _status_tag(w.get("T日状态")), _span(w.get("T日涨跌幅")),
                               _esc(w["原因标注"])])
        track_tbl = _table(["代码", "名称", f"{sig_base_md}状态", "细分行业", "趋势", "评分", f"{date_md}收盘状态", f"{date_md}涨跌幅", "原因"], track_rows)
    else:
        track_tbl = ""


    # ---- 弱转强：趋势分级信号表 ----
    w2s = signals.get("weak_to_strong", [])
    w2s_rows = [[_esc(x["代码"]), _esc(x["名称"]), _esc(x["所属行业"]),
                 _grade_tag(x.get("等级")), _esc(x.get("信号类型", "—")),
                 _esc(x["今状态"]), _esc(x.get("综合评分")), _esc(x.get("趋势", "—")),
                 _esc(x.get("当前趋势", "—")),
                 _tag("纳入" if x.get("纳入自选池") else "观察", "tup" if x.get("纳入自选池") else ""),
                 _esc(x.get("封板时间", "")), _esc(x.get("原因", ""))] for x in w2s]
    w2s_tbl = _table(["代码", "名称", "细分行业", "等级", "趋势类型", "今日", "评分", "洗盘前趋势", "当前趋势", "处理", "封板时间", "原因"], w2s_rows)
    type_count = signals.get("w2s_type_count", {})
    js_w2s_type = json.dumps([{"name": k, "value": v} for k, v in type_count.items()], ensure_ascii=False)

    ak = signals.get("a_kill", [])
    ak_rows = [[_esc(x["代码"]), _esc(x["名称"]), _esc(x["所属行业"]),
                _esc(x["今状态"]), _esc(x.get("综合评分")), _esc(x.get("趋势", "—")),
                _tag(x.get("风险等级", "中"), "tdown" if x.get("风险等级") == "高" else ""),
                _esc(x.get("原因", ""))] for x in ak]
    ak_tbl = _table(["代码", "名称", "细分行业", "今日", "评分", "趋势", "风险等级", "原因"], ak_rows)

    drop_excl = [c for c in cands if c.get("连续大跌")]



    # ---- 双自选池 + 持续跟踪统计（独立函数，供控制服务实时返回最新片段） ----
    watch_panel = watch_panel_html(state, params.get("buyable_grades"))
    tracking = state.get("tracking", {}) or {}   # 下钻明细仍用当前快照

    prem = s.get("昨涨停今日溢价")
    prem_txt = f"{prem:+.2f}%" if prem is not None else "—"
    sc = "#c00000" if (score or 0) >= 60 else ("#e08c00" if (score or 0) >= 42 else "#0a8f5b")
    breadth = s.get("盘中广度", {})
    mode_cn = "盘中实时" if mode == "intraday" else "收盘复盘"

    emo_items = [
        (f"{base_md}涨停", s.get("涨停家数", 0), "#c00000"),
        (f"{base_md}跌停", s.get("跌停家数", 0), "#0a8f5b"),
        ("炸板率", f"{s.get('炸板率',0)*100:.1f}%", "#e08c00"),
        ("最高连板", f"{s.get('最高连板',1)}板", "#2b5a9e"),
        ("打板溢价", prem_txt, "#c00000" if (prem or 0) >= 0 else "#0a8f5b"),
        ("待选/观察", f"{primary['candidate_count']}/{primary['watch_count']}", "#2b5a9e"),
        ("连续大跌剔除", sum(v.get("连续大跌剔除", 0) for v in split.values()), "#0a8f5b"),
    ]
    emo_cards = _stat_cards(emo_items)
    breadth_html = ""
    if breadth:
        breadth_html = f"""<div class="note"><b>盘中市场广度（{_esc(breadth.get('数据时点'))}）</b>：
        实时涨停 <span class="up">{breadth.get('实时涨停')}</span> ·
        实时跌停 <span class="down">{breadth.get('实时跌停')}</span> ·
        上涨 {breadth.get('实时上涨')} · 下跌 {breadth.get('实时下跌')}（样本 {breadth.get('总样本')}，腾讯实时行情）</div>"""

    w_cards = _stat_cards([(k, f"{v}", "#2b5a9e") for k, v in weights.items()])

    # ============ 下钻数据：图表点击 -> 股票池 ============
    name_map = {c["代码"]: c["名称"] for c in cands}
    # 主池下钻字段：收盘=当日状态+次日(待观察)；盘中=T-1状态+T日盘中
    if is_close:
        F = ["代码", "名称", "细分行业", "趋势", "评分", f"{base_md}状态", f"{next_md}状态", f"{next_md}涨跌", "原因"]
    else:
        F = ["代码", "名称", "细分行业", "趋势", "评分", f"{base_md}状态", f"{date_md}状态", f"{date_md}涨跌", "原因"]
    def _cr(items):
        return _dict_rows([[c["代码"], c["名称"], c["所属行业"], c["trend"], c.get("综合评分"),
                            c["类型"], c.get("T日状态", "待观察"), c.get("T日涨跌幅"),
                            c.get("剔除原因") or c.get("原因标注", "")] for c in items], F)
    drill = {}
    for kind in ("涨停", "跌停"):
        drill[f"待选池_{kind}"] = _cr([c for c in cands if c["类型"] == kind])
        drill[f"观察池_{kind}"] = _cr([c for c in watch if c["类型"] == kind])
    drill["连续大跌剔除"] = _cr(drop_excl)
    # 自选池持续跟踪快照（入池后逐日/盘中）
    TF = ["日期", "快照类型", "价格", "累计收益%"]
    for _code, _tr in tracking.items():
        drill[f"跟踪_{_code}"] = _dict_rows(
            [[x.get("date"), x.get("tag"), x.get("close"), x.get("pct")]
             for x in (_tr.get("snapshots") or [])], TF)
    # 连板梯队只统计 T-1 涨停股的连板数；跌停股的"连板数"实为连续跌停天数，
    # 必须限定类型=涨停，否则点击"3板"会混入连跌3天的跌停股。
    for b in board:
        drill[f"连板_{b}板"] = _cr([c for c in cands
                                    if c["类型"] == "涨停" and int(c.get("连板数", 1) or 1) == int(b)])
    F_sig = ["代码", "名称", "细分行业", "趋势", "评分", f"{sig_base_md}→{date_md}", f"{date_md}状态", f"{date_md}涨跌", "原因"]
    for t in type_count:
        drill[f"信号_{t}"] = _dict_rows(
            [[x["代码"], x["名称"], x["所属行业"], x.get("趋势", "—"), x.get("综合评分"),
              "跌停→涨停", "涨停", "—", x.get("建议", "")] for x in w2s if x.get("信号类型") == t], F_sig)
    # 回测样本下钻
    if bt and "error" not in bt:
        samples = bt.get("signal_samples", [])
        HZ = list(C.BACKTEST_HORIZONS)
        SF = ["代码", "日期", "趋势", "等级", "评分", "情绪分", "连续大跌"] + [f"T+{k}" for k in HZ]
        def _sr(items):
            return _dict_rows([[x["code"], x["date"], x["trend"], x["grade"], x["score"],
                                x["emo"], "是" if x["consec"] else "否"]
                               + [x.get(f"T+{k}") for k in HZ] for x in items], SF)
        drill["回测_全部信号"] = _sr(samples)
        for trend, st in bt.get("trend_split", {}).items():
            drill[f"回测趋势_{trend}"] = _sr([x for x in samples if x["trend"] == trend])
        med = bt.get("emotion_split", {}).get("中位情绪分", 50)
        drill["回测情绪_高"] = _sr([x for x in samples if x["emo"] >= med])
        drill["回测情绪_低"] = _sr([x for x in samples if x["emo"] < med])
        drill["回测连续大跌_是"] = _sr([x for x in samples if x["consec"]])
        drill["回测连续大跌_否"] = _sr([x for x in samples if not x["consec"]])
        codes_by_th = {}
        for d in bt.get("threshold_sensitivity", []):
            codes_by_th[str(d["门槛"])] = set(d.get("样本代码", []))
        for th, codes in codes_by_th.items():
            drill[f"回测门槛_{th}"] = _sr([x for x in samples if x["code"] in codes])
        for name, v in bt.get("weight_sensitivity", {}).items():
            drill[f"回测权重_{name}"] = _sr([x for x in samples if x["code"] in set(v.get("过门槛样本代码", []))])
    js_drill = json.dumps(drill, ensure_ascii=False, default=str)

    # ============ 回测可视化数据 ============
    # 先给所有 JS 变量兜底默认值，确保回测失败/无回测分支也能正常渲染（避免 UnboundLocalError）
    HZ = list(C.BACKTEST_HORIZONS)
    js_sens_w = js_sens_th = js_trend = "[]"
    js_hz = json.dumps(HZ, ensure_ascii=False)
    js_primary = HZ[0] if HZ else 5
    _pair = {f"T+{k}平均收益%": None for k in HZ}
    _pair.update({f"T+{k}胜率%": None for k in HZ})
    _empty_pair = [dict(_pair), dict(_pair)]
    js_emo = json.dumps(_empty_pair, ensure_ascii=False)
    js_consec = json.dumps(_empty_pair, ensure_ascii=False)
    bt_html = ""
    if bt and "error" not in bt:
        ov, aks, es = bt["overall"], bt["akill"], bt["emotion_split"]
        sens, tsens, tsplit = bt.get("weight_sensitivity", {}), bt.get("threshold_sensitivity", []), bt.get("trend_split", {})
        cd = bt.get("consecutive_drop", {})
        HZ = list(C.BACKTEST_HORIZONS)
        def _mini(st):
            parts = [f"样本 {st.get('样本数',0)}"]
            for k in HZ:
                parts.append(f"T+{k} <b class='{_cls(st.get(f'T+{k}平均收益%'))}'>{st.get(f'T+{k}平均收益%')}%</b>(胜{st.get(f'T+{k}胜率%')}%)")
            return " · ".join(parts)
        # 权重敏感性表（每个方案列出各周期过门槛收益/胜率）
        sens_rows = []
        for k, v in sens.items():
            row = [_esc(k), v.get("样本数", 0), _esc(v.get("过门槛样本"))]
            for h in HZ:
                row += [_esc(v.get(f"过门槛T+{h}%")), _esc(v.get(f"过门槛T+{h}胜率%"))]
            sens_rows.append(row)
        sens_headers = ["权重方案", "信号样本", "过门槛样本"]
        for h in HZ:
            sens_headers += [f"T+{h}收益%", f"T+{h}胜率%"]
        sens_tbl = _table(sens_headers, sens_rows)
        # 趋势分级表
        trend_rows = []
        for k, v in tsplit.items():
            row = [_esc(k), _esc(v.get("等级", "—")), v.get("样本数", 0)]
            for h in HZ:
                row += [_esc(v.get(f"T+{h}平均收益%")), _esc(v.get(f"T+{h}胜率%"))]
            trend_rows.append(row)
        trend_headers = ["趋势类型", "等级", "样本数"]
        for h in HZ:
            trend_headers += [f"T+{h}收益%", f"T+{h}胜率%"]
        trend_tbl = _table(trend_headers, trend_rows)
        # 评分门槛表
        th_rows = []
        for d in tsens:
            row = [d["门槛"], d["样本数"]]
            for h in HZ:
                row += [_esc(d.get(f"T+{h}平均收益%")), _esc(d.get(f"T+{h}胜率%"))]
            th_rows.append(row)
        th_headers = ["评分门槛", "过门槛样本"]
        for h in HZ:
            th_headers += [f"T+{h}收益%", f"T+{h}胜率%"]
        th_tbl = _table(th_headers, th_rows)

        js_sens_w = json.dumps([{"name": k, "n": v.get("样本数", 0), "kept": v.get("过门槛样本", 0),
                                 **{f"all{k2}": v.get(f"全样本T+{k2}%") for k2 in HZ},
                                 **{f"ret{k2}": v.get(f"过门槛T+{k2}%") for k2 in HZ},
                                 **{f"win{k2}": v.get(f"过门槛T+{k2}胜率%") for k2 in HZ}}
                                for k, v in sens.items()], ensure_ascii=False)
        js_sens_th = json.dumps([{"name": f"{d['门槛']}分", "n": d["样本数"],
                                  **{f"ret{k2}": d.get(f"T+{k2}平均收益%") for k2 in HZ},
                                  **{f"win{k2}": d.get(f"T+{k2}胜率%") for k2 in HZ}}
                                 for d in tsens], ensure_ascii=False)
        js_trend = json.dumps([{"name": k, "grade": v.get("等级", "—"), "n": v.get("样本数", 0),
                                **{f"ret{k2}": v.get(f"T+{k2}平均收益%") for k2 in HZ},
                                **{f"win{k2}": v.get(f"T+{k2}胜率%") for k2 in HZ}}
                               for k, v in tsplit.items()], ensure_ascii=False)
        js_emo = json.dumps([{"name": "高情绪", **es.get("高情绪", {})},
                             {"name": "低情绪", **es.get("低情绪", {})}], ensure_ascii=False, default=str)
        js_consec = json.dumps([{"name": "连续大跌后反转", **cd.get("连续大跌后反转", {})},
                                {"name": "非连续大跌反转", **cd.get("非连续大跌反转", {})}], ensure_ascii=False, default=str)
        # JS 侧使用的周期列表与主周期
        js_hz = json.dumps(HZ, ensure_ascii=False)
        js_primary = HZ[0]

        bt_html = f"""
        <div class="card">
          <h2>{_CN[no_bt-1]}、回测校验与参数敏感性（点击图表可下钻个股）</h2>
          <div class="note">回测区间 {_esc(bt['period'])} · 交易日 {bt['trade_days']} · 股票宇宙 {bt['universe_size']} 只。K线涨跌幅近似判定涨跌停，用于参数相对比较。</div>
          <div class="sub">基准表现</div>
          <div class="btline">弱转强：{_mini(ov)}</div>
          <div class="btline">A杀（应偏弱以验证风险提示）：{_mini(aks)}</div>

          <div class="sub">① 趋势分级敏感性 — 验证"趋势为主"的分类是否有区分度</div>
          <div id="chartTrend" class="chart" style="height:260px;"></div>
          {trend_tbl}

          <div class="sub">② 评分门槛敏感性 — 验证门槛设置合理性</div>
          <div id="chartThreshold" class="chart" style="height:260px;"></div>
          {th_tbl}

          <div class="sub">③ 情绪分位敏感性（中位情绪分 {es.get('中位情绪分')}）</div>
          <div id="chartEmo" class="chart" style="height:240px;"></div>
          <div class="btline">高情绪：{_mini(es.get('高情绪',{}))}<br>低情绪：{_mini(es.get('低情绪',{}))}</div>

          <div class="sub">④ 连续大跌过滤敏感性 — 验证剔除规则是否合理</div>
          <div id="chartConsec" class="chart" style="height:240px;"></div>
          <div class="btline">连续大跌后反转：{_mini(cd.get('连续大跌后反转',{}))}<br>非连续大跌反转：{_mini(cd.get('非连续大跌反转',{}))}</div>

          <div class="sub">⑤ 权重方案敏感性（评分门槛 {C.SCORE_WATCH_MIN} 分）</div>
          <div id="chartWeight" class="chart" style="height:280px;"></div>
          {sens_tbl}
          <div class="note">{_esc(bt.get('caveat',''))}</div>
        </div>"""
    elif bt and "error" in bt:
        bt_html = f'<div class="card"><h2>{_CN[no_bt-1]}、回测校验</h2><div class="empty">回测不可用：{_esc(bt["error"])}</div></div>'
    else:
        # 无回测时仍需给出结构完整的空数据，避免 f-string 内索引越界
        js_sens_w = js_sens_th = js_trend = "[]"
        js_hz = json.dumps(list(C.BACKTEST_HORIZONS), ensure_ascii=False)
        js_primary = C.BACKTEST_HORIZONS[0]
        _pair = {f"T+{k}平均收益%": None for k in C.BACKTEST_HORIZONS}
        _pair.update({f"T+{k}胜率%": None for k in C.BACKTEST_HORIZONS})
        _empty_pair = [dict(_pair), dict(_pair)]
        js_emo = json.dumps(_empty_pair, ensure_ascii=False)
        js_consec = json.dumps(_empty_pair, ensure_ascii=False)

    # ============ 自动调参面板（参数敏感性闭环） ============
    tuned_p = tune_doc.get("params", {}) or {}
    decisions = tune_doc.get("decisions", []) or []
    tuned_at = tune_doc.get("tuned_at", "未调参")
    based = tune_doc.get("based_on", {}) or {}
    if tuned_p:
        buyable_html = "".join(f'<span class="tag ga">{_esc(g)}</span> ' for g in tuned_p.get("buyable_grades", [])) or "—"
        w = tuned_p.get("weights", {})
        w_html = " ｜ ".join(f"{_esc(k)} {v}" for k, v in w.items())
        dec_html = "".join(f"<li>{_esc(d)}</li>" for d in decisions) or "<li>暂无决策记录</li>"
        gs = tuned_p.get("_grade_scores", {}) or {}
        gs_rows = [[_esc(g), _esc(v.get("trend", "—")), v.get("samples", 0),
                    _esc(v.get("ret")), _esc(v.get("win")),
                    "✓" if v.get("sample_ok") else "样本不足"]
                   for g, v in sorted(gs.items())]
        gs_tbl = _table(["等级", "趋势", "样本数", f"T+{C.HOLD_HORIZON}收益%", f"T+{C.HOLD_HORIZON}胜率%", "样本充足"], gs_rows) if gs_rows else '<div class="empty">暂无分级回测数据</div>'
        tune_html = f"""
        <div class="card">
          <h2>自动调参 · 敏感性闭环（基于实际走势定期更新）</h2>
          <div class="note">最近调参：{_esc(tuned_at)} ｜ 依据区间：{_esc(based.get('period','—'))} ｜ 信号样本：{based.get('samples','—')}。持续跟踪实际走势，每周自动重算；参数经置信度折扣与平滑，避免过拟合。</div>
          <div class="btline"><b>当前生效参数</b>：可买等级 {buyable_html} ｜ 持有周期 T+{tuned_p.get('hold_horizon', C.HOLD_HORIZON)} ｜ 评分门槛 {tuned_p.get('score_watch_min','—')} ｜ 破位过滤 {'开' if tuned_p.get('breakdown_filter') else '关'}</div>
          <div class="btline"><b>生效权重</b>：{_esc(w_html)}</div>
          <div class="sub">调参决策依据</div>
          <ul class="tight">{dec_html}</ul>
          <div class="sub">分级后验与主周期表现</div>
          {gs_tbl}
          <div class="note">以上由回测敏感性分析结果自动转化而来，用于模型参数相对比较，不构成投资建议。</div>
        </div>"""
    else:
        tune_html = """
        <div class="card">
          <h2>自动调参 · 敏感性闭环</h2>
          <div class="empty">尚未生成调参结果。运行 <code>python main.py --backtest --tune</code> 或在桌面应用中点击「盘后复盘」后生效。</div>
        </div>"""

    # 直达按钮（本地控制服务：设置窗口 / 盘中监测 / 盘后复盘）
    # 优先读取控制服务落盘的实际端口（占用时自动+1），否则用配置默认端口
    ctrl_port = C.CONTROL_PORT
    try:
        with open(C.CONTROL_PORT_FILE, "r", encoding="utf-8") as _pf:
            ctrl_port = json.load(_pf).get("port", C.CONTROL_PORT)
    except Exception:
        pass
    ctrl_base = f"http://{C.CONTROL_HOST}:{ctrl_port}"
    quick_html = f"""
    <div class="card" style="padding:14px 22px;">
      <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;">
        <b style="margin-right:6px;">⚙ 工具控制</b>
        <button class="qbtn" onclick="ctrl('settings')">参数设置窗口</button>
        <button class="qbtn" onclick="ctrl('monitor')">启动盘中监测</button>
        <button class="qbtn" onclick="ctrl('review')">盘后复盘+调参</button>
        <button class="qbtn" onclick="ctrl('watchlist')">自选池管理</button>
        <span id="ctrlMsg" style="color:var(--sub);font-size:12px;"></span>
      </div>
      <div class="note">按钮需桌面应用运行时可用（首次请双击桌面「A股情绪监测」图标启动）。所有数据本地存储，仅行情联网。</div>
    </div>"""

    gen = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date_cn = f"{obs_date[:4]}-{obs_date[4:6]}-{obs_date[6:8]}"
    base_cn = f"{base[:4]}-{base[4:6]}-{base[6:8]}"
    bt_flag = "true" if (bt and "error" not in bt) else "false"

    # ============ 模式相关的展示片段 ============
    if is_close:
        header_meta = (f"统计日：{date_cn}（收盘）｜ 待选池来源：{date_cn} 涨停+跌停 ｜ "
                       f"观察池供 {next_cn} 使用 ｜ 生成：{gen}")
        note_sentiment = (f"情绪温度由 {date_md} 收盘涨停/跌停家数、炸板率、最高连板与打板溢价合成；"
                          f"第三项观察池基于 {date_md} 收盘统计，供 {next_md} 观察。"
                          f"分数越高越接近主升/高潮，越低越接近冰点。")
        split_card_title = f"待选池 → 观察池 分类统计（{date_md}收盘 → 供{next_md}观察）"
        board_card_title = f"连板梯队（{date_md}收盘，断层=退潮信号）"
        section3_card = f"""<div class="card">
  <h2>三、明日观察池（{date_md}收盘统计，供{next_md}观察）</h2>
  <div class="note">{date_md} 涨停+跌停个股（剔除ST）经近3月趋势过滤（上升趋势/高位震荡）且未出现连续{C.CONSEC_DROP_DAYS}日大跌者留存，<b>{next_md} 开盘后重点跟踪</b>。评分含短期动能因子（惩罚急跌）。原因标注中"同花顺"为第三方来源需核实。</div>
  {watch_tbl}
</div>"""
        section6_card = f"""<div class="card">
  <h2>{_CN[no_track-1]}、观察池跟踪（{sig_base_md}收盘构建 → {date_md}收盘表现）</h2>
  <div class="note">{sig_base_md} 收盘统计入选观察池的个股在 {date_md} 的实际表现（原"观察池"与"次日跟踪"两表合并）。反手涨停即弱转强信号（见第二项），跌停/A杀注意风险。</div>
  {track_tbl}
</div>"""
        section7_card = f"""<div class="card">
  <h2>{_CN[no_cand-1]}、待选池全量（{date_md} 涨停+跌停，已剔除ST，供{next_md}观察）</h2>
  {cand_tbl}
</div>"""
        note_usage = (f"本报告为 <b>{date_cn} 收盘统计</b>：待选池/观察池来自 {date_cn} 涨跌停（第三项供 {next_cn} 观察）；"
                      f"第二项信号与第六项跟踪为 {sig_base_md}→{date_cn} 的状态转移与表现。")
    else:
        header_meta = f"观察日：{date_cn} ｜ 待选池来源日：{base_cn} ｜ 生成：{gen}"
        note_sentiment = (f"情绪温度由 {base_md} 涨停/跌停家数、炸板率、最高连板与打板溢价合成；"
                          f"观察池叠加 {date_md} 盘中实时状态。分数越高越接近主升/高潮，越低越接近冰点。")
        split_card_title = "待选池 → 观察池 分类统计"
        board_card_title = f"连板梯队（{base_md}收盘，断层=退潮信号）"
        section3_card = f"""<div class="card">
  <h2>三、观察池（{base_md}收盘构建，{date_md}盘中实时跟踪）</h2>
  <div class="note">近3月趋势为"上升趋势/高位震荡"且未出现连续{C.CONSEC_DROP_DAYS}日大跌者留存。评分含短期动能因子（惩罚急跌）。原因标注中"同花顺"为第三方来源需核实。</div>
  {watch_tbl}
</div>"""
        section6_card = ""  # 盘中模式：观察池本身即实时跟踪，无独立跟踪节
        section7_card = f"""<div class="card">
  <h2>{_CN[no_cand-1]}、待选池全量（{base_md} 涨停+跌停，已剔除ST）</h2>
  {cand_tbl}
</div>"""
        note_usage = (f"盘中模式：观察日 {date_cn} 为「今天」；待选池来自 {base_cn} 涨跌停；"
                      f"观察池叠加 {date_cn} 盘中实时状态（收盘后运行切换为收盘复盘口径）。")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>A股市场情绪强弱分析 · {date_cn}</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
:root{{--up:#c00000;--down:#0a8f5b;--ink:#1f2733;--sub:#5b6675;--line:#e6e9ef;--bg:#f5f7fa;--card:#fff;--accent:#2b5a9e;}}
*{{box-sizing:border-box;}}
body{{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;line-height:1.6;}}
.wrap{{max-width:1300px;margin:0 auto;padding:28px 20px 60px;}}
header h1{{font-size:26px;margin:0 0 6px;}}
.meta{{color:var(--sub);font-size:13px;margin-bottom:20px;}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px 22px;margin-bottom:18px;box-shadow:0 1px 3px rgba(20,30,50,.04);}}
.card h2{{font-size:17px;margin:0 0 14px;padding-left:10px;border-left:4px solid var(--accent);}}
.sub{{font-size:14px;font-weight:600;margin:16px 0 6px;color:#34405a;}}
.hero{{display:flex;gap:22px;flex-wrap:wrap;align-items:center;}}
.score-box{{flex:0 0 200px;text-align:center;}}
.score-num{{font-size:52px;font-weight:700;line-height:1;}}
.phase{{display:inline-block;margin-top:8px;padding:4px 14px;border-radius:20px;background:#eef2f9;color:var(--accent);font-weight:600;font-size:14px;}}
.kpis{{flex:1;display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px;}}
.kpi{{background:#fafbfd;border:1px solid var(--line);border-radius:10px;padding:12px 14px;}}
.kpi .v{{font-size:22px;font-weight:700;}}
.kpi .l{{font-size:12px;color:var(--sub);margin-top:2px;}}
.up{{color:var(--up);font-weight:600;}}
.down{{color:var(--down);font-weight:600;}}
.na{{color:#9aa4b2;}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:18px;}}
@media(max-width:900px){{.grid2{{grid-template-columns:1fr;}}}}
.chart{{width:100%;height:300px;cursor:pointer;}}
.tw{{overflow-x:auto;max-height:520px;overflow-y:auto;}}
table{{border-collapse:collapse;width:100%;font-size:13px;white-space:nowrap;}}
th,td{{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;}}
th{{position:sticky;top:0;background:#f0f3f8;font-weight:600;color:#34405a;z-index:1;}}
tbody tr:hover{{background:#fafbfd;}}
.tag{{display:inline-block;padding:1px 8px;border-radius:5px;font-size:12px;font-weight:600;}}
.tup{{background:#fdecec;color:var(--up);}}
.tdown{{background:#e7f5ee;color:var(--down);}}
.thigh{{background:#fff3e0;color:#b26a00;}}
.ga{{background:#fdecec;color:#c00000;}}
.gb{{background:#fff3e0;color:#b26a00;}}
.gc{{background:#eef2f9;color:#2b5a9e;}}
.gd{{background:#eef7f2;color:#0a8f5b;}}
.alert{{border-radius:10px;padding:14px 16px;margin-bottom:12px;font-size:14px;}}
.alert-strong{{background:#fdecec;border-left:4px solid var(--up);}}
.alert-weak{{background:#e7f5ee;border-left:4px solid var(--down);}}
.alert b{{font-size:15px;}}
.empty{{color:#9aa4b2;padding:22px;text-align:center;background:#fafbfd;border-radius:8px;}}
.note{{font-size:12px;color:var(--sub);margin-top:10px;}}
.clickhint{{font-size:12px;color:var(--accent);margin-top:6px;}}
.btline{{background:#fafbfd;border:1px solid var(--line);border-radius:8px;padding:10px 14px;font-size:13px;margin-bottom:8px;}}
.splitbar{{display:flex;gap:26px;flex-wrap:wrap;margin-top:6px;}}
.devider{{color:var(--sub);}}
.disclaimer{{background:#fff8e6;border:1px solid #f0dfa8;border-radius:10px;padding:14px 16px;color:#7a5b00;font-size:13px;margin-top:24px;}}
ul.tight{{margin:8px 0 0;padding-left:20px;}}
ul.tight li{{margin:4px 0;}}
.mask{{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(15,23,42,.45);z-index:99;align-items:center;justify-content:center;padding:24px;}}
.modal{{background:#fff;border-radius:12px;width:min(1120px,96vw);max-height:86vh;display:flex;flex-direction:column;box-shadow:0 20px 60px rgba(0,0,0,.25);}}
.modal-h{{display:flex;justify-content:space-between;align-items:center;padding:16px 20px;border-bottom:1px solid var(--line);}}
.modal-h b{{font-size:16px;}}
.modal-h span{{cursor:pointer;font-size:24px;color:var(--sub);line-height:1;}}
.modal-b{{overflow:auto;padding:14px 20px;}}
.qbtn{{border:1px solid var(--accent);background:#fff;color:var(--accent);border-radius:8px;padding:7px 14px;font-size:13px;cursor:pointer;font-weight:600;}}
.qbtn:hover{{background:var(--accent);color:#fff;}}
.wbtn{{border:1px solid var(--accent);background:#f4f8ff;color:var(--accent);border-radius:5px;padding:3px 9px;font-size:12px;cursor:pointer;white-space:nowrap;}}
.wbtn:hover{{background:var(--accent);color:#fff;}}
.wbtn:disabled{{border-color:#ccd3de;background:#f4f5f7;color:#9aa4b2;cursor:default;}}
.codelink{{color:var(--accent);font-weight:600;text-decoration:none;}}
.codelink:hover{{text-decoration:underline;}}
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>A股市场情绪强弱分析看板 <span style="font-size:14px;color:var(--accent);">· {mode_cn}</span></h1>
  <div class="meta">{header_meta}</div>
</header>

{quick_html}

<div class="card">
  <h2>一、情绪温度计 · 首屏结论</h2>
  <div class="hero">
    <div class="score-box">
      <div class="score-num" style="color:{sc}">{score if score is not None else '—'}</div>
      <div style="color:var(--sub);font-size:13px;">情绪温度 / 100（{base_md} 收盘口径）</div>
      <div class="phase">{phase}</div>
    </div>
    <div class="kpis">{emo_cards}</div>
  </div>
  {breadth_html}
  <div class="note">{note_sentiment}</div>
</div>

<div class="grid2">
  <div class="card">
    <h2>{split_card_title}</h2>
    <div class="splitbar">
      <div class="kpi" style="flex:1;"><div class="l">涨停股 待选→观察</div><div class="v up">{split.get('涨停',{}).get('待选池',0)} <span class="devider">→</span> {split.get('涨停',{}).get('纳入观察池',0)}</div><div class="l">纳入率 {split.get('涨停',{}).get('纳入率',0)}% · 连续大跌剔除 {split.get('涨停',{}).get('连续大跌剔除',0)}</div></div>
      <div class="kpi" style="flex:1;"><div class="l">跌停股 待选→观察</div><div class="v down">{split.get('跌停',{}).get('待选池',0)} <span class="devider">→</span> {split.get('跌停',{}).get('纳入观察池',0)}</div><div class="l">纳入率 {split.get('跌停',{}).get('纳入率',0)}% · 连续大跌剔除 {split.get('跌停',{}).get('连续大跌剔除',0)}</div></div>
    </div>
    <div id="chartSplit" class="chart" style="height:240px;"></div>
    <div class="clickhint">↗ 点击柱子查看对应股票池明细</div>
  </div>
  <div class="card">
    <h2>{board_card_title}</h2>
    <div id="chartBoard" class="chart" style="height:240px;"></div>
    <div class="clickhint">↗ 点击扇形查看该连板高度个股</div>
  </div>
</div>

<div class="card">
  <h2>二、信号提醒（趋势优先分级）</h2>
  <div class="alert alert-strong">
    <b>情绪转强 · 弱转强分级（{sig_base_md} 跌停 → {date_md} 反手涨停）</b>
    <div class="note"><b>以跌停洗盘前的趋势分级</b>：A 高位平台反包 / B 上升趋势洗盘 / C 弱势反抽或中线修复 / D 下跌反转。当前生效可买等级：<b>{buyable_txt}</b>（由回测自动调参或设置窗口决定），仅这些等级纳入自动自选池，其余仅观察。{'盘中为实时快照，收盘前可能变化。' if mode=='intraday' else ''}</div>
  </div>
  <div id="chartW2sType" class="chart" style="height:220px;"></div>
  <div class="clickhint">↗ 点击图形查看该类型信号个股</div>
  {w2s_tbl}
  <div class="alert alert-weak" style="margin-top:18px;">
    <b>情绪转弱 · A杀风险（{sig_base_md} 涨停 → {date_md} 跌停，不纳入自选池）</b>
    <div class="note">A杀越多说明高位接力亏钱效应越强，应降低打板预期；高位股A杀风险等级标记为"高"。</div>
  </div>
  {ak_tbl}
</div>

{section3_card}

<div class="card">
  <h2>四、当前权重（可用 --weights 调整）</h2>
  <div class="kpis">{w_cards}</div>
  <div class="note">趋势=斜率/均线；高位=距3月高点；封板质量=连板/炸板；题材=涨停原因热门度；短期动能=近5日收益（连续大跌腰斩）。改权重后重跑，用{_CN[no_bt-1]}、回测敏感性对比。</div>
</div>

<div class="card">
  <h2>五、自选池（自动 / 手动）· 入池后持续跟踪</h2>
  <div id="watchPanel" data-sync="live">{watch_panel}</div>
</div>

{section6_card}

{section7_card}

{bt_html}
{tune_html}

<div class="card" style="background:#fafbfd;">
  <h2>指标口径与使用说明</h2>
  <ul class="tight">
    <li><b>时序</b>：{note_usage}</li>
    <li><b>趋势优先分级</b>：弱转强在全待选池判定（避免漏掉下跌反转的真信号），按<b>跌停洗盘前的趋势</b>分 A(高位震荡)/B(上升趋势)/C(震荡偏弱)/D(下跌趋势) 四级；当前可买等级为 <b>{buyable_txt}</b>（自动调参/设置窗口可覆盖），表中同时列出洗盘前趋势与当前趋势供对照。</li>
    <li><b>连续大跌剔除</b>：近{C.MOMENTUM_LOOKBACK}日内单日跌幅≥{C.BIG_DROP_PCT}% 且连续{C.CONSEC_DROP_DAYS}日者，视为短期破位，从观察池剔除。</li>
    <li><b>评分五因子</b>：趋势/高位/封板质量/题材/短期动能；连续大跌对动能因子打 {C.CONSEC_DROP_PENALTY} 折。</li>
    <li><b>双自选池</b>：自动池由达到可买等级的弱转强信号纳入，手动池由观察池「＋加自选」或桌面应用添加；两池均可手动剔除，剔除后停止跟踪并归档；点击跟踪表代码查看逐日快照。<b>桌面应用运行时第五项每15秒自动同步最新状态</b>（未运行则显示生成时快照）。</li>
    <li><b>图表下钻</b>：点击任一图表（分类统计、连板梯队、信号类型、5组敏感性图）即弹出对应股票池明细，便于实操定位个股。</li>
    <li><b>回测</b>：由K线涨跌幅近似判定涨跌停，回放信号并统计后验收益，做趋势分级/评分门槛/情绪分位/连续大跌/权重方案五类敏感性分析；为相对比较，非精确成交回测。</li>
    <li><b>数据来源</b>：行情动态获取（东方财富/腾讯），涨停原因同花顺（非一手需核实），不含训练数据中的历史数值。</li>
  </ul>
</div>

<div class="disclaimer">
<b>免责声明</b>：以上内容基于公开数据和量化分析，仅供参考，不构成投资建议。市场有风险，投资需谨慎。任何投资决策应结合个人风险承受能力、资金状况和投资目标独立判断，必要时咨询持牌专业机构。过往表现不预示未来收益。
</div>
</div>

<div class="mask" id="drillMask">
  <div class="modal">
    <div class="modal-h"><b id="drillTitle">股票池明细</b><span onclick="closeDrill(event)">×</span></div>
    <div class="modal-b" id="drillBody"></div>
  </div>
</div>

<script>
var DRILL = {js_drill};
var HAS_BT = {bt_flag};
function esc(v){{return String(v==null?'—':v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}}
function showDrill(key, title){{
  var rows = DRILL[key] || [];
  var h = '';
  if (!rows.length) {{ h = '<div class="empty">该分组暂无个股</div>'; }}
  else {{
    var cols = Object.keys(rows[0]);
    h = '<div class="tw"><table><thead><tr>' + cols.map(function(c){{return '<th>'+esc(c)+'</th>';}}).join('') + '</tr></thead><tbody>';
    rows.forEach(function(r){{ h += '<tr>' + cols.map(function(c){{return '<td>'+esc(r[c])+'</td>';}}).join('') + '</tr>'; }});
    h += '</tbody></table></div><div class="note">共 ' + rows.length + ' 只</div>';
  }}
  document.getElementById('drillTitle').textContent = title || key;
  document.getElementById('drillBody').innerHTML = h;
  document.getElementById('drillMask').style.display = 'flex';
}}
function closeDrill(e){{ document.getElementById('drillMask').style.display = 'none'; }}
document.getElementById('drillMask').addEventListener('click', function(e){{
  if (e.target === this) {{ this.style.display = 'none'; }}
}});
function ctrl(action){{
  var msg = document.getElementById('ctrlMsg');
  msg.textContent = '正在请求 ' + action + ' ...';
  fetch('{ctrl_base}/api/' + action, {{mode:'cors'}})
    .then(function(r){{return r.json();}})
    .then(function(j){{msg.textContent = j.msg || ('已触发:' + action);}})
    .catch(function(){{msg.textContent = '桌面应用未运行，请先启动「A股情绪监测」';}});
}}

var CTRL_BASE = '{ctrl_base}';
// 自选池/跟踪数据以桌面应用 state.json 为准：定时拉取最新片段，避免看板停在生成时刻的旧快照
function syncWatchPanel(){{
  fetch(CTRL_BASE + '/api/watchlist/panel', {{mode:'cors', cache:'no-store'}})
    .then(function(r){{return r.json();}})
    .then(function(j){{
      var el = document.getElementById('watchPanel');
      if (j.ok && j.data && j.data.html && el) {{ el.innerHTML = j.data.html; }}
    }})
    .catch(function(){{ /* 桌面应用未启动时保留生成时的快照 */ }});
}}
function startWatchSync(){{
  syncWatchPanel();
  setInterval(syncWatchPanel, 15000);
  window.addEventListener('focus', syncWatchPanel);
}}
startWatchSync();
function wlQuery(p){{
  return Object.keys(p).map(function(k){{
    return encodeURIComponent(k) + '=' + encodeURIComponent(p[k] == null ? '' : p[k]);
  }}).join('&');
}}
function wlRequest(path, p, onOk){{
  var msg = document.getElementById('ctrlMsg');
  msg.textContent = '正在处理自选池 ...';
  fetch(CTRL_BASE + path + '?' + wlQuery(p), {{mode:'cors'}})
    .then(function(r){{return r.json();}})
    .then(function(j){{
      msg.textContent = j.msg || '操作完成';
      if (j.ok && onOk) {{ onOk(j); }}
      else if (!j.ok) {{ alert(j.msg || '操作失败'); }}
    }})
    .catch(function(){{msg.textContent = '桌面应用未运行，请先启动「A股情绪监测」';}});
}}
function wlAdd(code, name, industry, addDate, trend, score, price){{
  wlRequest('/api/watchlist/add', {{pool:'manual', code:code, name:name, industry:industry,
    date:addDate, trend:trend, score:score, price:price}}, function(){{
    var bs = document.querySelectorAll('.wbtn');
    for (var i=0; i<bs.length; i++) {{
      var oc = bs[i].getAttribute('onclick') || '';
      if (oc.indexOf(code) >= 0 && !bs[i].disabled) {{ bs[i].disabled = true; bs[i].textContent = '✓已在池'; }}
    }}
    syncWatchPanel();
  }});
}}
function wlRemove(pool, code, name){{
  if (!window.confirm('确认剔除 ' + name + '（' + code + '）？\\n剔除后将停止持续跟踪，当前走势写入归档。')) return;
  wlRequest('/api/watchlist/remove', {{pool:pool, code:code, reason:'看板手动剔除'}}, function(){{ syncWatchPanel(); }});
}}
function wlArchiveRemove(code, name){{
  if (!window.confirm('确认删除 ' + name + '（' + code + '）的归档记录？\\n该操作不可恢复，将同时清除其跟踪记录。')) return;
  wlRequest('/api/archive/remove', {{code:code}}, function(){{ syncWatchPanel(); }});
}}

var chartSplit = echarts.init(document.getElementById('chartSplit'));
chartSplit.setOption({{
  tooltip: {{trigger:'axis'}},
  legend: {{data:['待选池','纳入观察池']}},
  grid: {{left:50,right:20,top:36,bottom:28}},
  xAxis: {{type:'category', data:['涨停股','跌停股']}},
  yAxis: {{type:'value'}},
  series: [
    {{name:'待选池', type:'bar', data:[{js_up_base}, {js_dn_base}], itemStyle:{{color:'#9fb3cc'}}}},
    {{name:'纳入观察池', type:'bar', data:[{js_up_keep}, {js_dn_keep}], itemStyle:{{color:'#2b5a9e'}}}}
  ]
}});
chartSplit.on('click', function(p){{
  var kind = p.name === '涨停股' ? '涨停' : '跌停';
  var key = (p.seriesName === '纳入观察池' ? '观察池_' : '待选池_') + kind;
  showDrill(key, p.seriesName + ' · ' + p.name);
}});

var chartBoard = echarts.init(document.getElementById('chartBoard'));
chartBoard.setOption({{
  tooltip: {{trigger:'item', formatter:'{{b}}: {{c}}只 ({{d}}%)'}},
  series: [{{type:'pie', radius:['42%','72%'], data:{js_board}, label:{{formatter:'{{b}}: {{c}}只'}}}}]
}});
chartBoard.on('click', function(p){{ showDrill('连板_'+p.name, p.name+'个股'); }});

var chartW2sType = echarts.init(document.getElementById('chartW2sType'));
chartW2sType.setOption({{
  tooltip: {{trigger:'item', formatter:'{{b}}: {{c}}只 ({{d}}%)'}},
  series: [{{type:'pie', radius:['35%','65%'], data:{js_w2s_type},
    label:{{formatter:'{{b}}: {{c}}只'}},
    itemStyle:{{color:function(p){{return ['#c00000','#e08c00','#2b5a9e','#0a8f5b'][p.dataIndex%4];}}}}}}]
}});
chartW2sType.on('click', function(p){{ showDrill('信号_'+p.name, p.name+' · 弱转强信号'); }});

if (HAS_BT) {{
  var HZ = {js_hz};                 // 回测周期数组，如 [5,10,20]
  var PRIMARY = {js_primary};       // 主周期（柱图默认高亮）
  var RET_COLORS = ['#c00000','#e08c00','#2b5a9e'];
  function retSeries(arr, keyPrefix, type){{
    return HZ.map(function(k, i){{
      return {{name:'T+'+k+'收益%', type:type||'bar',
        data:arr.map(function(x){{return x[keyPrefix+k];}}),
        itemStyle:{{color:RET_COLORS[i%RET_COLORS.length]}}}};
    }});
  }}
  function winSeries(arr, keyPrefix, yIdx){{
    return HZ.map(function(k, i){{
      return {{name:'T+'+k+'胜率%', type:'line', yAxisIndex:yIdx||0,
        data:arr.map(function(x){{return x[keyPrefix+k];}}),
        lineStyle:{{type:i===0?'solid':'dashed', width:i===0?2:1.2}},
        itemStyle:{{color:['#7b1fa2','#00838f','#558b2f'][i%3]}}}};
    }});
  }}

  var tdata = {js_trend};
  var chartTrend = echarts.init(document.getElementById('chartTrend'));
  chartTrend.setOption({{
    tooltip: {{trigger:'axis'}}, legend:{{data:HZ.map(function(k){{return 'T+'+k+'收益%';}}).concat(HZ.map(function(k){{return 'T+'+k+'胜率%';}})), type:'scroll'}},
    grid:{{left:55,right:50,top:52,bottom:60}},
    xAxis:{{type:'category', data:tdata.map(function(x){{return x.name;}}), axisLabel:{{interval:0,rotate:15}}}},
    yAxis:[{{type:'value',name:'收益%'}},{{type:'value',name:'胜率%'}}],
    series: retSeries(tdata,'ret','bar').concat(winSeries(tdata,'win',1))
  }});
  chartTrend.on('click', function(p){{ showDrill('回测趋势_'+p.name, p.name+' · 弱转强回测样本'); }});

  var thdata = {js_sens_th};
  var chartThreshold = echarts.init(document.getElementById('chartThreshold'));
  chartThreshold.setOption({{
    tooltip: {{trigger:'axis'}}, legend:{{data:['过门槛样本数'].concat(HZ.map(function(k){{return 'T+'+k+'收益%';}})), type:'scroll'}},
    grid:{{left:55,right:50,top:52,bottom:28}},
    xAxis:{{type:'category', data:thdata.map(function(x){{return x.name;}})}},
    yAxis:[{{type:'value',name:'样本/收益'}},{{type:'value',name:'胜率%'}}],
    series:[{{name:'过门槛样本数',type:'bar',data:thdata.map(function(x){{return x.n;}}),itemStyle:{{color:'#9fb3cc'}}}}]
      .concat(retSeries(thdata,'ret','line'))
  }});
  chartThreshold.on('click', function(p){{ showDrill('回测门槛_'+p.name.replace('分',''), p.name+' · 过门槛信号'); }});

  // 情绪/连续大跌数据键名为 "T+5平均收益%"（带"平均"），需单独构造
  function avgSeries(arr, yIdx){{
    return HZ.map(function(k, i){{
      return {{name:'T+'+k+'收益%',type:'bar',data:arr.map(function(x){{return x['T+'+k+'平均收益%'];}}),itemStyle:{{color:RET_COLORS[i%RET_COLORS.length]}}}};
    }});
  }}
  function winRateSeries(arr, yIdx){{
    return HZ.map(function(k, i){{
      return {{name:'T+'+k+'胜率%',type:'line',yAxisIndex:yIdx||0,data:arr.map(function(x){{return x['T+'+k+'胜率%'];}}),itemStyle:{{color:['#7b1fa2','#00838f','#558b2f'][i%3]}}}};
    }});
  }}
  var emoData = {js_emo};
  var chartEmo = echarts.init(document.getElementById('chartEmo'));
  chartEmo.setOption({{
    tooltip: {{trigger:'axis'}}, legend:{{data:HZ.map(function(k){{return 'T+'+k+'收益%';}}).concat(HZ.map(function(k){{return 'T+'+k+'胜率%';}})), type:'scroll'}},
    grid:{{left:55,right:20,top:52,bottom:28}},
    xAxis:{{type:'category', data:['高情绪','低情绪']}},
    yAxis:[{{type:'value',name:'收益%'}},{{type:'value',name:'胜率%'}}],
    series: avgSeries(emoData,0).concat(winRateSeries(emoData,1))
  }});
  chartEmo.on('click', function(p){{ showDrill('回测情绪_'+(p.name==='高情绪'?'高':'低'), p.name+' · 弱转强信号'); }});

  var conData = {js_consec};
  var chartConsec = echarts.init(document.getElementById('chartConsec'));
  chartConsec.setOption({{
    tooltip: {{trigger:'axis'}}, legend:{{data:HZ.map(function(k){{return 'T+'+k+'收益%';}}), type:'scroll'}},
    grid:{{left:55,right:20,top:52,bottom:28}},
    xAxis:{{type:'category', data:['连续大跌后反转','非连续大跌反转']}},
    yAxis:[{{type:'value',name:'收益%'}},{{type:'value',name:'胜率%'}}],
    series: avgSeries(conData,0).concat(winRateSeries(conData,1))
  }});
  chartConsec.on('click', function(p){{ showDrill('回测连续大跌_'+(p.name.indexOf('连续大跌后')>=0?'是':'否'), p.name+' · 弱转强信号'); }});

  var wdata = {js_sens_w};
  var chartWeight = echarts.init(document.getElementById('chartWeight'));
  chartWeight.setOption({{
    tooltip: {{trigger:'axis'}}, legend:{{data:HZ.map(function(k){{return '过门槛T+'+k+'%';}}).concat(HZ.map(function(k){{return 'T+'+k+'胜率%';}})), type:'scroll'}},
    grid:{{left:60,right:20,top:52,bottom:60}},
    xAxis:{{type:'category', data:wdata.map(function(x){{return x.name;}}), axisLabel:{{interval:0,rotate:15}}}},
    yAxis:{{type:'value'}},
    series: HZ.map(function(k, i){{
      return {{name:'过门槛T+'+k+'%',type:'bar',data:wdata.map(function(x){{return x['ret'+k];}}),itemStyle:{{color:RET_COLORS[i%RET_COLORS.length]}}}};
    }}).concat(HZ.map(function(k, i){{
      return {{name:'T+'+k+'胜率%',type:'line',data:wdata.map(function(x){{return x['win'+k];}}),itemStyle:{{color:['#7b1fa2','#00838f','#558b2f'][i%3]}}}};
    }}))
  }});
  chartWeight.on('click', function(p){{ showDrill('回测权重_'+p.name, p.name+' · 过门槛信号'); }});
}}

window.addEventListener('resize', function(){{
  chartSplit.resize(); chartBoard.resize(); chartW2sType.resize();
  if (HAS_BT) {{ ['chartTrend','chartThreshold','chartEmo','chartConsec','chartWeight'].forEach(function(id){{
    echarts.getInstanceByDom(document.getElementById(id)).resize();
  }}); }}
}});
</script>
</body>
</html>
"""
    C.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = C.REPORT_DIR / f"sentiment_{obs_date}.html"
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    return str(out)
