# -*- coding: utf-8 -*-
"""A股情绪监测 · 本地桌面应用 v6

功能：
  1. 盘中监测：定时刷新腾讯实时行情，扫描观察池+自选池，弱转强/A杀即时弹窗+声音提醒，
     按综合评分给出"最优股"排名；自动判断交易时段，非交易时段提示。
  2. 盘后复盘：一键运行 main.py（收盘统计+回测+自动调参），生成 HTML 看板并打开。
  3. 参数设置：tkinter 可视化调整权重/阈值/等级/刷新间隔等，保存到本地 user_settings.json。
  4. 本地控制服务：127.0.0.1:18765 提供 /api/settings|monitor|review|watchlist/add|watchlist/remove|...，
     供看板 HTML 的"工具控制 / ＋加自选 / 剔除"按钮一键直达。
  5. 自选池管理：自动池（弱转强信号）/ 手动池（看板或界面添加）分开展示，均可手动剔除；
     在池个股持续跟踪累计收益、区间回撤和 T+5/T+10/T+20，剔除后归入归档。
  6. 系统托盘：关闭窗口 = 最小化到托盘（后台监测继续运行），左键打开主界面，
     右键菜单含"打开主界面/启动监测/停止监测/盘后复盘/退出程序"；
     只有右键"退出程序"才真正结束进程（与可转债提醒工具一致）。

所有数据本地存储（data/），仅行情获取需联网。
"""
import os
import sys
import json
import time
import threading
import datetime as dt
import webbrowser
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

os.environ.setdefault("TQDM_DISABLE", "1")

import config as C
import data_fetcher as D
import settings as CFG
import storage as S
import tracking as TRK
import report as RP

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, scrolledtext
except ImportError:
    tk = None

APP_TITLE = "A股情绪监测 · 本地工作台 v6"
_hide_console = sys.platform == "win32"


# ==================== 控制台/日志 ====================
class Log:
    def __init__(self, cb=None):
        self.cb = cb

    def __call__(self, msg):
        ts = dt.datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        try:
            print(line)
        except Exception:
            pass
        if self.cb:
            try:
                self.cb(line)
            except Exception:
                pass


LOG = Log()
_STATE_LOCK = threading.RLock()   # 控制接口 / 监测线程 / GUI 共用 state.json，避免互相覆盖


def _current_buyable():
    """看板实时片段用的可买等级：以最近一次复盘实际生效的参数为准。"""
    try:
        dates = sorted(f for f in os.listdir(C.DATA_DIR)
                       if f.startswith("result_") and f.endswith(".json"))
        if dates:
            res = json.load(open(C.DATA_DIR / dates[-1], "r", encoding="utf-8"))
            g = (res.get("effective_params") or {}).get("buyable_grades")
            if g:
                return g
    except Exception:
        pass
    return None


def _is_trading_time() -> bool:
    now = dt.datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return (9 * 60 + 25) <= t <= (15 * 60 + 5)


def _sound():
    try:
        import winsound
        winsound.Beep(880, 180)
        time.sleep(0.08)
        winsound.Beep(1180, 220)
    except Exception:
        pass


# ==================== 盘中监测引擎 ====================
class Monitor:
    """定时扫描实时行情，检测信号，维护最优股排名。"""

    def __init__(self, on_update=None, on_signal=None):
        self.on_update = on_update
        self.on_signal = on_signal
        self.running = False
        self.thread = None
        self.alerted = set()       # 已提醒过的 (code, status, date) 去重
        self.watch_codes = {}      # code -> {名称,来源,类型,评分} 监测标的
        self.last_scan = None
        self.rows = []             # 当前展示行

    def load_watchlist(self):
        """读取自动/手动双自选池 + 最近结果的主池观察池，作为监测标的。"""
        targets = {}
        try:
            state = S.load()
            for pool in C.WATCH_POOLS:  # 手动池后加载，同名代码以手动池信息为准
                for code, info in (state.get("watchlist", {}).get(pool) or {}).items():
                    targets[code] = {"名称": info.get("名称", code),
                                     "来源": C.POOL_LABELS.get(pool, "自选池"),
                                     "类型": info.get("信号类型", "—"),
                                     "评分": info.get("综合评分")}
        except Exception:
            pass
        # 最近的观察池（前瞻池）
        try:
            dates = sorted(f for f in os.listdir(C.DATA_DIR) if f.startswith("result_") and f.endswith(".json"))
            if dates:
                res = json.load(open(C.DATA_DIR / dates[-1], "r", encoding="utf-8"))
                for w in (res.get("pools", {}).get("watch") or []):
                    targets.setdefault(w["代码"], {"名称": w["名称"], "来源": "观察池",
                                                   "类型": w.get("类型", "—"), "评分": w.get("综合评分")})
        except Exception:
            pass
        self.watch_codes = targets
        if not targets:
            LOG("监测标的为空：本地无自选池/观察池数据，请先运行一次盘后复盘")
        return targets

    def scan_once(self) -> dict:
        """单次扫描：拉取实时行情，评估监测标的，返回本轮结果。"""
        s = CFG.to_runtime_params()
        if not self.watch_codes:
            self.load_watchlist()
        quotes = D.realtime_quotes()
        breadth = D.realtime_market_breadth()
        result = {"time": dt.datetime.now().strftime("%H:%M:%S"),
                  "breadth": breadth, "rows": [], "signals": [], "trading": _is_trading_time()}
        if quotes.empty:
            result["error"] = "实时行情获取失败（网络或数据源问题）"
            self.rows = []
            return result
        qmap = quotes.set_index("代码")
        today = dt.datetime.now().strftime("%Y%m%d")
        for code, meta in self.watch_codes.items():
            if code not in qmap.index:
                continue
            r = qmap.loc[code]
            pct = float(r["zdf"]) if str(r.get("zdf", "")).replace(".", "", 1).lstrip("-").isdigit() else None
            status = r.get("实时状态", "—")
            hsl = r.get("hsl")
            row = {"代码": code, "名称": meta["名称"], "来源": meta["来源"],
                   "现价": r.get("zxj"), "涨跌幅": pct, "状态": status,
                   "换手率": hsl, "评分": meta.get("评分")}
            result["rows"].append(row)
            # 信号检测：弱转强(涨停)/A杀(跌停)，且本轮未提醒过
            if status in ("涨停", "跌停") and s["alert_popup"]:
                key = (code, status, today)
                if key not in self.alerted:
                    self.alerted.add(key)
                    sig = {"代码": code, "名称": meta["名称"], "状态": status,
                           "涨跌幅": pct, "来源": meta["来源"]}
                    result["signals"].append(sig)
        # 双自选池持续跟踪：把实时价/状态写入跟踪记录（收盘统计不覆盖）
        try:
            with _STATE_LOCK:
                st = S.load()
                if TRK.update_realtime(st, result["rows"], today):
                    S.save(st)
        except Exception as e:
            LOG(f"自选池实时跟踪更新失败：{e}")
        # 最优股排名：可买等级对应的观察池 + 已封板的强势股优先
        result["rows"].sort(key=lambda x: (
            x["状态"] == "涨停", x["涨跌幅"] if x["涨跌幅"] is not None else -999,
            x["评分"] or 0), reverse=True)
        self.rows = result["rows"]
        self.last_scan = result
        return result

    def loop(self):
        while self.running:
            try:
                res = self.scan_once()
                if self.on_update:
                    self.on_update(res)
                for sig in res.get("signals", []):
                    if self.on_signal:
                        self.on_signal(sig)
                    if CFG.to_runtime_params()["alert_sound"]:
                        _sound()
            except Exception as e:
                LOG(f"监测扫描异常: {e}")
            interval = max(10, CFG.to_runtime_params()["refresh_interval_sec"])
            for _ in range(interval):
                if not self.running:
                    break
                time.sleep(1)

    def start(self):
        if self.running:
            return
        self.load_watchlist()
        self.running = True
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()
        LOG(f"盘中监测已启动，标的 {len(self.watch_codes)} 只，间隔 {CFG.to_runtime_params()['refresh_interval_sec']}s")

    def stop(self):
        self.running = False
        LOG("盘中监测已停止")


# ==================== 本地控制服务（看板按钮直达） ====================
class ControlService:
    def __init__(self, app):
        self.app = app

    def start(self):
        app = self.app

        class Handler(BaseHTTPRequestHandler):
            def _cors(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

            def _json(self, msg):
                self._cors()
                self.wfile.write(json.dumps({"ok": True, "msg": msg}, ensure_ascii=False).encode("utf-8"))

            def _result(self, ok, msg, data=None):
                self._cors()
                body = {"ok": bool(ok), "msg": msg}
                if data is not None:
                    body["data"] = data
                self.wfile.write(json.dumps(body, ensure_ascii=False, default=str).encode("utf-8"))

            def do_OPTIONS(self):
                self._json("ok")

            def do_GET(self):
                parsed = urlparse(self.path)
                path = parsed.path
                q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                if path == "/api/settings":
                    app.after(0, app.open_settings)
                    self._json("已打参数设置窗口")
                elif path == "/api/monitor":
                    app.after(0, app.start_monitor)
                    self._json("已启动盘中监测")
                elif path == "/api/review":
                    app.after(0, lambda: app.run_review(auto_tune=True))
                    self._json("已启动盘后复盘+调参，请稍候")
                elif path == "/api/open_report":
                    app.after(0, app.open_report)
                    self._json("已打开看板")
                elif path == "/api/watchlist":
                    app.after(0, app.open_watchlist)
                    self._json("已打开自选池管理")
                elif path in ("/api/watchlist/add", "/api/watchlist/remove"):
                    code = q.get("code", "")
                    pool = q.get("pool", "manual")
                    with _STATE_LOCK:
                        st = S.load()
                        if path.endswith("/add"):
                            info = {k: q.get(k, "") for k in
                                    ("name", "industry", "trend", "source", "signal", "grade")}
                            info = {"名称": info["name"] or code, "所属行业": info["industry"] or "—",
                                    "trend": info["trend"], "source": info["source"] or "看板手动添加",
                                    "signal": info["signal"], "等级": info["grade"],
                                    "add_price": q.get("price"), "综合评分": q.get("score")}
                            ok, msg, _ = S.add_watch(st, pool, code, info, date=q.get("date"))
                        else:
                            ok, msg, _ = S.remove_watch(st, pool, code,
                                                        reason=q.get("reason") or "手动剔除")
                        if ok:
                            S.save(st)
                    if ok:
                        app.after(0, app.on_watchlist_changed)
                    self._result(ok, msg)
                elif path == "/api/watchlist/list":
                    st = S.load()
                    self._result(True, "ok", {"watchlist": st.get("watchlist", {}),
                                              "tracking": st.get("tracking", {}),
                                              "archive": st.get("watchlist_archive", {})})
                elif path == "/api/archive/remove":
                    with _STATE_LOCK:
                        st = S.load()
                        ok, msg, _ = S.remove_archive(st, q.get("code", ""))
                        if ok:
                            S.save(st)
                    if ok:
                        app.after(0, app.on_watchlist_changed)
                    self._result(ok, msg)
                elif path == "/api/watchlist/panel":
                    # 看板第五项定时拉取：与桌面界面/state.json 同源，杜绝静态快照不同步
                    with _STATE_LOCK:
                        st = S.load()
                        html = RP.watch_panel_html(st, _current_buyable())
                    self._result(True, "ok", {"html": html})
                elif path == "/api/hide":
                    app.after(0, app.hide_to_tray)
                    self._json("已收进系统托盘（后台继续运行）")
                elif path == "/api/show":
                    app.after(0, app.show_window)
                    self._json("已恢复主界面")
                elif path == "/api/quit":
                    app.after(0, app.quit_app)
                    self._json("正在退出程序")
                elif path == "/api/status":
                    self._json(f"运行中｜监测={'开' if app.monitor.running else '关'}"
                               f"｜窗口={'托盘' if getattr(app, '_hidden', False) else '显示'}")
                elif path == "/api/monitor/status":
                    self._result(True, "ok", {"running": app.monitor.running,
                                              "targets": len(app.monitor.watch_codes),
                                              "rows": len(app.monitor.rows),
                                              "last_scan": app.monitor.last_scan.get("time") if app.monitor.last_scan else None})
                elif path == "/api/monitor/reload":
                    n = len(app.monitor.load_watchlist())
                    self._result(True, f"已刷新监测标的 {n} 只", {"targets": n})
                else:
                    self._cors()
                    self.wfile.write(json.dumps({"ok": False, "msg": "unknown"}).encode())

            def log_message(self, *args):
                pass

        # 端口被占用时自动向后试探最多20个端口，成功后落盘供看板读取
        srv = None
        used_port = None
        for attempt in range(20):
            port = C.CONTROL_PORT + attempt
            try:
                srv = ThreadingHTTPServer((C.CONTROL_HOST, port), Handler)
                used_port = port
                break
            except OSError:
                continue
        if srv:
            try:
                C.DATA_DIR.mkdir(parents=True, exist_ok=True)
                with open(C.CONTROL_PORT_FILE, "w", encoding="utf-8") as pf:
                    json.dump({"port": used_port}, pf)
            except Exception:
                pass
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            LOG(f"本地控制服务已启动 http://{C.CONTROL_HOST}:{used_port}")
        else:
            LOG(f"控制服务启动失败：{C.CONTROL_PORT}~{C.CONTROL_PORT+19} 均被占用")


# ==================== 盘后复盘 ====================
def run_review_process(auto_tune=True, log_cb=None):
    """运行完整收盘复盘+回测+自动调参。
    源码环境：子进程调用 main.py（隔离、稳定）；
    EXE冻结环境：进程内直接调用 main.run()（无法用 EXE 带脚本参数）。
    """
    if getattr(sys, "frozen", False):
        import io
        import traceback
        from contextlib import redirect_stdout
        import main as M
        buf = io.StringIO()
        logpath = C.DATA_DIR / "review_log.txt"
        code = 0
        try:
            with open(logpath, "w", encoding="utf-8") as lf:
                lf.write(f"复盘开始 {dt.datetime.now():%Y-%m-%d %H:%M:%S}\n")
                lf.flush()
                with redirect_stdout(buf):
                    M.run(mode="close", do_backtest=True, do_tune=auto_tune)
                lf.write(buf.getvalue() + "\n")
                lf.write(f"复盘完成 {dt.datetime.now():%Y-%m-%d %H:%M:%S}\n")
        except Exception as e:
            code = 1
            try:
                with open(logpath, "a", encoding="utf-8") as lf:
                    lf.write(buf.getvalue() + "\n异常：" + str(e) + "\n" + traceback.format_exc())
            except Exception:
                pass
        return code, buf.getvalue()
    cmd = [sys.executable, str(C.APP_SOURCE_DIR / "main.py"), "--mode", "close", "--backtest"]
    if auto_tune:
        cmd.append("--tune")
    env = os.environ.copy()
    env["TQDM_DISABLE"] = "1"
    proc = subprocess.run(cmd, cwd=str(C.APP_SOURCE_DIR), capture_output=True, text=True, env=env, timeout=1800)
    out = proc.stdout + "\n" + proc.stderr
    return proc.returncode, out


# ==================== GUI ====================
class App(tk.Tk if tk else object):
    def __init__(self):
        if not tk:
            raise RuntimeError("tkinter 不可用")
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1080x720")
        self.minsize(900, 600)
        self._hidden = False       # 是否已收进系统托盘
        self._quitting = False
        self.tray = None
        self.monitor = Monitor(on_update=self.on_monitor_update, on_signal=self.on_signal)
        self._build_ui()
        n0 = len(self.monitor.load_watchlist())
        self.lbl_status.config(text=f"状态：待启动｜标的 {n0} 只",
                               foreground="#0a8f5b" if n0 else "#c00")
        self.after(0, lambda: LOG(f"应用已启动，监测标的 {n0} 只。盘中自动监测，盘后一键复盘。"))
        ControlService(self).start()
        # 关闭窗口 = 最小化到托盘，不退出；只有托盘右键"退出程序"才真正结束
        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self._init_tray()
        # 若启动时处于交易时段，自动开启监测
        if _is_trading_time():
            self.after(1500, self.start_monitor)

    # ---------- 系统托盘（关闭窗口后台运行 / 右键退出） ----------
    def _init_tray(self):
        try:
            import tray_win
            if not tray_win.supported():
                LOG("系统托盘不可用，关闭窗口将直接退出程序")
                return

            # 托盘回调运行在托盘线程里，tkinter 不是线程安全的：
            # 所有界面/业务动作必须经 after(0, ...) 切回主线程执行。
            def on_main(fn, *args):
                def go():
                    try:
                        self.after(0, lambda: fn(*args))
                    except Exception as e:
                        LOG(f"托盘动作调度失败：{e}")
                return go

            items = [
                (1001, "打开主界面", on_main(self.show_window)),
                (1002, "启动盘中监测", on_main(self.start_monitor)),
                (1003, "停止盘中监测", on_main(self.stop_monitor)),
                (1004, "盘后复盘（含回测+调参）", on_main(self.run_review, True)),
                (1007, "自选池管理", on_main(self.open_watchlist)),
                (1005, None, None),
                (1006, "退出程序", on_main(self.quit_app)),
            ]
            tray = tray_win.TrayIcon(
                title="A股情绪监测",
                tip="A股情绪监测 · 后台运行中（左键打开，右键菜单）",
                menu_items=items,
                on_left_click=on_main(self.show_window))
            tray.set_logger(LOG)
            if tray.start():
                self.tray = tray
                LOG("已驻留系统托盘：关闭窗口不会退出，右键托盘图标可退出程序")
            else:
                LOG("托盘创建失败，关闭窗口将直接退出（功能不受影响）")
        except Exception as e:
            LOG(f"托盘初始化异常：{e}")

    def hide_to_tray(self):
        """点窗口 X：收进托盘，后台监测/控制服务继续运行。"""
        if self.tray is None:
            self.quit_app()
            return
        try:
            self.withdraw()
        except Exception:
            pass
        self._hidden = True
        try:
            self.tray.show_balloon(
                "A股情绪监测 · 后台运行中",
                "窗口已收进托盘，盘中监测继续。右键托盘图标可打开界面或退出程序。")
        except Exception:
            pass
        LOG("窗口已最小化到系统托盘，后台继续运行（右键托盘图标可退出）")

    def show_window(self):
        """从托盘恢复主界面。"""
        try:
            self.deiconify()
            self.lift()
            self.focus_force()
        except Exception:
            pass
        self._hidden = False

    def quit_app(self):
        """真正退出：停监测 -> 删托盘图标 -> 销毁窗口。仅托盘右键/看板退出按钮调用。"""
        if self._quitting:
            return
        self._quitting = True
        LOG("正在退出程序…")
        try:
            self.monitor.stop()
        except Exception:
            pass
        try:
            if self.tray:
                self.tray.stop()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass
        LOG("[退出] 已停止")

    def _build_ui(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=10)
        self.tab_monitor = ttk.Frame(nb)
        self.tab_watch = ttk.Frame(nb)
        self.tab_review = ttk.Frame(nb)
        self.tab_settings = ttk.Frame(nb)
        self.tab_log = ttk.Frame(nb)
        nb.add(self.tab_monitor, text="盘中监测")
        nb.add(self.tab_watch, text="自选池管理")
        nb.add(self.tab_review, text="盘后复盘")
        nb.add(self.tab_settings, text="参数设置")
        nb.add(self.tab_log, text="运行日志")
        self._build_monitor_tab()
        self._build_watchlist_tab()
        self._build_review_tab()
        self._build_settings_tab()
        self._build_log_tab()

    # ---------- 盘中监测页 ----------
    def _build_monitor_tab(self):
        f = self.tab_monitor
        top = ttk.Frame(f)
        top.pack(fill="x", pady=6)
        ttk.Button(top, text="▶ 启动监测", command=self.start_monitor).pack(side="left", padx=4)
        ttk.Button(top, text="■ 停止监测", command=self.stop_monitor).pack(side="left", padx=4)
        ttk.Button(top, text="⟳ 立即扫描", command=self.manual_scan).pack(side="left", padx=4)
        ttk.Button(top, text="刷新标的池", command=self.reload_targets).pack(side="left", padx=4)
        self.lbl_status = ttk.Label(top, text="状态：未启动", foreground="#888")
        self.lbl_status.pack(side="left", padx=12)

        self.lbl_breadth = ttk.Label(f, text="市场广度：—", font=("Microsoft YaHei", 10))
        self.lbl_breadth.pack(fill="x", pady=2)

        cols = ("代码", "名称", "来源", "现价", "涨跌幅", "状态", "换手率", "评分")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=20)
        widths = {"代码": 90, "名称": 120, "来源": 90, "现价": 80, "涨跌幅": 90, "状态": 80, "换手率": 80, "评分": 70}
        for c in cols:
            self.tree.heading(c, text=c)
            self.tree.column(c, width=widths[c], anchor="center")
        vs = ttk.Scrollbar(f, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        # 红涨绿跌
        self.tree.tag_configure("up", foreground="#c00000")
        self.tree.tag_configure("down", foreground="#0a8f5b")
        self.tree.tag_configure("limitup", foreground="#fff", background="#c00000")

    def on_monitor_update(self, res):
        def apply():
            if res.get("error"):
                self.lbl_status.config(text="状态：" + res["error"], foreground="#c00")
                return
            b = res.get("breadth", {})
            self.lbl_breadth.config(text=(
                f"市场广度：涨停 {b.get('实时涨停','—')} ｜ 跌停 {b.get('实时跌停','—')} ｜ "
                f"上涨 {b.get('实时上涨','—')} ｜ 下跌 {b.get('实时下跌','—')} "
                f"｜ 扫描 {res['time']} {'｜盘中' if res['trading'] else '｜非交易时段'}"))
            n_target = len(self.monitor.watch_codes)
            n_show = len(res["rows"])
            if n_target == 0:
                self.lbl_status.config(text="状态：监测中｜标的 0 只（请先盘后复盘或手动加自选）",
                                       foreground="#c00")
            else:
                self.lbl_status.config(text=f"状态：监测中｜标的 {n_target} 只｜显示 {n_show} 只",
                                       foreground="#0a8f5b")
            for item in self.tree.get_children():
                self.tree.delete(item)
            for r in res["rows"]:
                pct = r["涨跌幅"]
                tag = "limitup" if r["状态"] == "涨停" else ("up" if (pct or 0) > 0 else ("down" if (pct or 0) < 0 else ""))
                self.tree.insert("", "end", values=(
                    r["代码"], r["名称"], r["来源"], r["现价"],
                    f"{pct:+.2f}%" if pct is not None else "—", r["状态"],
                    f"{r['换手率']}%" if r.get("换手率") is not None else "—",
                    r.get("评分") or "—"), tags=(tag,))
            # 自选池管理页的统计也随每轮扫描刷新，避免界面停在旧快照
            if getattr(self, "watch_tree", None) is not None:
                self.refresh_watchlist()
        self.after(0, apply)

    def on_signal(self, sig):
        def show():
            kind = "弱转强·封板（情绪转强）" if sig["状态"] == "涨停" else "A杀·跌停（情绪转弱风险）"
            msg = (f"{sig['名称']}（{sig['代码']}）\n"
                   f"来源：{sig['来源']}\n"
                   f"状态：{sig['状态']}　涨跌幅：{sig['涨跌幅']}%\n"
                   f"时间：{dt.datetime.now().strftime('%H:%M:%S')}")
            if CFG.to_runtime_params()["alert_popup"]:
                messagebox.showinfo(f"信号提醒 · {kind}", msg)
            LOG(f"信号：{sig['名称']} {sig['状态']} {sig['涨跌幅']}%")
        self.after(0, show)

    def start_monitor(self):
        self.monitor.start()

    def stop_monitor(self):
        self.monitor.stop()
        self.lbl_status.config(text="状态：已停止", foreground="#888")

    def manual_scan(self):
        threading.Thread(target=self._scan_then_update, daemon=True).start()

    def _scan_then_update(self):
        res = self.monitor.scan_once()
        self.on_monitor_update(res)
        for sig in res.get("signals", []):
            self.on_signal(sig)

    def reload_targets(self):
        n = len(self.monitor.load_watchlist())
        LOG(f"已刷新监测标的：{n} 只")
        if n:
            self.lbl_status.config(text=f"状态：标的池已刷新｜标的 {n} 只", foreground="#0a8f5b")
            messagebox.showinfo("标的池", f"已加载监测标的 {n} 只（自选池+最近观察池）")
        else:
            self.lbl_status.config(text="状态：标的 0 只（无自选池/观察池数据）", foreground="#c00")
            messagebox.showwarning(
                "标的池为空",
                "未找到自选池或观察池数据。\n请先运行一次「盘后复盘」生成观察池，"
                "或在「自选池管理」页手动添加个股。")

    # ---------- 自选池管理页（自动/手动双池 + 持续跟踪） ----------
    def _build_watchlist_tab(self):
        f = self.tab_watch
        top = ttk.Frame(f)
        top.pack(fill="x", pady=8, padx=6)
        ttk.Label(top, text="代码").pack(side="left", padx=3)
        self.wl_code = ttk.Entry(top, width=10)
        self.wl_code.pack(side="left", padx=3)
        ttk.Label(top, text="名称").pack(side="left", padx=3)
        self.wl_name = ttk.Entry(top, width=14)
        self.wl_name.pack(side="left", padx=3)
        ttk.Label(top, text="行业").pack(side="left", padx=3)
        self.wl_industry = ttk.Entry(top, width=14)
        self.wl_industry.pack(side="left", padx=3)
        ttk.Label(top, text="加入").pack(side="left", padx=3)
        self.wl_pool = ttk.Combobox(top, width=10, state="readonly",
                                    values=[C.POOL_LABELS[p] for p in C.WATCH_POOLS])
        self.wl_pool.current(1 if len(C.WATCH_POOLS) > 1 else 0)  # 默认手动自选池
        self.wl_pool.pack(side="left", padx=3)
        ttk.Button(top, text="＋ 加入自选", command=self.add_watch_from_ui).pack(side="left", padx=6)
        ttk.Button(top, text="⟳ 刷新", command=self.refresh_watchlist).pack(side="left", padx=3)
        ttk.Button(top, text="✕ 剔除选中", command=self.remove_watch_from_ui).pack(side="left", padx=3)
        ttk.Button(top, text="✕ 删除归档", command=self.remove_archive_from_ui).pack(side="left", padx=3)

        ttk.Label(f, text="在池个股（每日统计至剔除，红色为上涨、绿色为下跌）",
                  font=("Microsoft YaHei", 10, "bold")).pack(anchor="w", padx=6)
        cols = ("池", "代码", "名称", "行业", "来源", "纳入日期", "持有交易日",
                "累计收益", "区间最高", "最大回撤", "T+5", "T+10", "T+20", "最新价", "最新状态")
        widths = {"池": 95, "代码": 85, "名称": 100, "行业": 110, "来源": 180, "纳入日期": 95,
                  "持有交易日": 80, "累计收益": 85, "区间最高": 85, "最大回撤": 85,
                  "T+5": 80, "T+10": 80, "T+20": 80, "最新价": 80, "最新状态": 80}
        self.watch_tree = ttk.Treeview(f, columns=cols, show="headings", height=13)
        for c in cols:
            self.watch_tree.heading(c, text=c)
            self.watch_tree.column(c, width=widths[c], anchor="center")
        self.watch_tree.tag_configure("up", foreground="#c00000")
        self.watch_tree.tag_configure("down", foreground="#0a8f5b")
        ys = ttk.Scrollbar(f, orient="vertical", command=self.watch_tree.yview)
        xs = ttk.Scrollbar(f, orient="horizontal", command=self.watch_tree.xview)
        self.watch_tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.watch_tree.pack(fill="both", expand=True, padx=6, pady=4)
        ys.pack(side="right", fill="y")
        xs.pack(side="bottom", fill="x")

        ttk.Label(f, text="已剔除归档（停止跟踪，保留终态走势）",
                  font=("Microsoft YaHei", 10, "bold")).pack(anchor="w", padx=6, pady=(8, 0))
        acols = ("代码", "名称", "所属池", "纳入日期", "剔除日期", "剔除原因", "持有交易日", "累计收益")
        self.arch_tree = ttk.Treeview(f, columns=acols, show="headings", height=6)
        for c in acols:
            self.arch_tree.heading(c, text=c)
            self.arch_tree.column(c, width=110, anchor="center")
        self.arch_tree.pack(fill="x", padx=6, pady=4)
        self.refresh_watchlist()

    @staticmethod
    def _pct_txt(v):
        try:
            return f"{float(v):+.2f}%" if v not in (None, "", "—") else "—"
        except Exception:
            return "—"

    def refresh_watchlist(self):
        try:
            st = S.load()
            tracking = st.get("tracking", {}) or {}
            for item in self.watch_tree.get_children():
                self.watch_tree.delete(item)
            for pool in C.WATCH_POOLS:
                items = (st.get("watchlist", {}).get(pool) or {}).items()
                for code, info in sorted(items, key=lambda kv: kv[1].get("add_date", ""), reverse=True):
                    stats = (tracking.get(code, {}).get("stats") or {})
                    cum = self._pct_txt(stats.get("累计收益%"))
                    tag = "up" if str(cum).startswith("+") else ("down" if str(cum).startswith("-") else "")
                    self.watch_tree.insert("", "end", tags=(tag,), values=(
                        C.POOL_LABELS[pool], code, info.get("名称", code), info.get("所属行业", "—"),
                        info.get("source", info.get("signal", "—")), info.get("add_date", "—"),
                        stats.get("持有交易日", "—"), cum,
                        self._pct_txt(stats.get("区间最高收益%")), self._pct_txt(stats.get("区间最大回撤%")),
                        self._pct_txt(stats.get("T+5收益%")), self._pct_txt(stats.get("T+10收益%")),
                        self._pct_txt(stats.get("T+20收益%")), stats.get("最新价", "—"),
                        stats.get("最新状态", "—")))
            for item in self.arch_tree.get_children():
                self.arch_tree.delete(item)
            for code, info in sorted((st.get("watchlist_archive") or {}).items(),
                                     key=lambda kv: kv[1].get("remove_date", ""), reverse=True):
                stats = (tracking.get(code, {}).get("final_stats") or info.get("final_stats") or {})
                self.arch_tree.insert("", "end", values=(
                    code, info.get("名称", code), C.POOL_LABELS.get(info.get("pool"), "—"),
                    info.get("add_date", "—"), info.get("remove_date", "—"),
                    info.get("remove_reason", "手动剔除"), stats.get("持有交易日", "—"),
                    self._pct_txt(stats.get("累计收益%"))))
        except Exception as e:
            LOG(f"刷新自选池失败：{e}")

    def add_watch_from_ui(self):
        code = self.wl_code.get().strip().zfill(6)
        if len(code) != 6 or not code.isdigit():
            messagebox.showwarning("输入错误", "请输入6位股票代码")
            return
        label = self.wl_pool.get()
        pool = next((p for p, l in C.POOL_LABELS.items() if l == label), "manual")
        with _STATE_LOCK:
            st = S.load()
            ok, msg, _ = S.add_watch(st, pool, code, {
                "名称": self.wl_name.get().strip() or code,
                "所属行业": self.wl_industry.get().strip() or "—",
                "source": "界面手动添加", "add_date": dt.date.today().strftime("%Y%m%d"),
            })
            if ok:
                S.save(st)
        if ok:
            self.wl_code.delete(0, "end")
            self.wl_name.delete(0, "end")
            self.wl_industry.delete(0, "end")
            self.refresh_watchlist()
            self.monitor.load_watchlist()
            LOG(f"自选池：{msg}，已纳入盘中监测标的")
        else:
            messagebox.showwarning("添加失败", msg)

    def remove_watch_from_ui(self):
        sel = self.watch_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在表格中选中要剔除的个股")
            return
        if not messagebox.askyesno("确认剔除", f"确认剔除选中的 {len(sel)} 只个股？\n剔除后停止跟踪并将当前走势写入归档。"):
            return
        with _STATE_LOCK:
            st = S.load()
            for iid in sel:
                vals = self.watch_tree.item(iid, "values")
                label = str(vals[0])
                pool = next((p for p, l in C.POOL_LABELS.items() if l == label), "manual")
                code = str(vals[1]).zfill(6)
                ok, msg, _ = S.remove_watch(st, pool, code, reason="界面手动剔除")
                if ok:
                    LOG(f"自选池：{msg}")
            S.save(st)
        self.refresh_watchlist()
        n = len(self.monitor.load_watchlist())
        self.lbl_status.config(text=f"状态：标的池已更新｜标的 {n} 只",
                               foreground="#0a8f5b" if n else "#c00")

    def remove_archive_from_ui(self):
        sel = self.arch_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在归档表中选中要删除的记录")
            return
        if not messagebox.askyesno("确认删除归档",
                                   f"确认彻底删除选中的 {len(sel)} 条归档记录？\n该操作不可恢复，同时清除对应跟踪数据。"):
            return
        with _STATE_LOCK:
            st = S.load()
            for iid in sel:
                code = str(self.arch_tree.item(iid, "values")[0]).zfill(6)
                ok, msg, _ = S.remove_archive(st, code)
                if ok:
                    LOG(f"归档：{msg}")
            S.save(st)
        self.refresh_watchlist()

    def open_watchlist(self):
        self.show_window()
        self.nametowidget(self.tab_watch.winfo_parent()).select(self.tab_watch)
        self.refresh_watchlist()

    def on_watchlist_changed(self):
        """看板按钮触发的加池/剔除：回到主线程刷新界面与监测标的。"""
        def apply():
            self.refresh_watchlist()
            n = len(self.monitor.load_watchlist())
            self.lbl_status.config(text=f"状态：标的池已更新｜标的 {n} 只",
                                   foreground="#0a8f5b" if n else "#c00")
            LOG(f"自选池已更新，界面与盘中监测标的已同步（{n} 只）")
        self.after(0, apply)

    # ---------- 盘后复盘页 ----------
    def _build_review_tab(self):
        f = self.tab_review
        intro = ("盘后复盘：运行当日收盘统计 → 生成观察池 → 回测(T+5/10/20) → 自动调参 → 生成 HTML 看板。\n"
                 "回测基于扩大后的周期（约1年），每周按实际走势更新参数。全部本地运行，仅行情联网。")
        ttk.Label(f, text=intro, wraplength=980, justify="left").pack(anchor="w", pady=10, padx=6)
        btns = ttk.Frame(f)
        btns.pack(fill="x", pady=8)
        ttk.Button(btns, text="▶ 开始盘后复盘（含回测+调参）", command=lambda: self.run_review(True)).pack(side="left", padx=4)
        ttk.Button(btns, text="仅复盘（不调参）", command=lambda: self.run_review(False)).pack(side="left", padx=4)
        ttk.Button(btns, text="打开最新看板", command=self.open_report).pack(side="left", padx=4)
        self.review_text = scrolledtext.ScrolledText(f, height=18, wrap="word")
        self.review_text.pack(fill="both", expand=True, padx=6, pady=6)
        self.review_text.insert("1.0", "复盘输出将显示在此。点击上方按钮开始。\n")

    def run_review(self, auto_tune=True):
        def work():
            self.review_text.insert("end", "\n===== 开始盘后复盘 =====\n")
            self.review_text.see("end")
            try:
                code, out = run_review_process(auto_tune)
                def done():
                    self.review_text.insert("end", out[-6000:])
                    self.review_text.see("end")
                    if code == 0:
                        self.review_text.insert("end", "\n✅ 复盘完成，看板已生成。\n")
                        # 复盘生成了新的观察池/状态：立即刷新盘中监测标的
                        n_target = len(self.monitor.load_watchlist())
                        self.lbl_status.config(text=f"状态：复盘已更新标的池｜标的 {n_target} 只",
                                               foreground="#0a8f5b")
                        LOG(f"复盘完成，监测标的已刷新：{n_target} 只")
                        if self._hidden:
                            # 窗口收在托盘时不弹模态框，避免后台被看不见的对话框卡住
                            LOG("盘后复盘完成，看板已生成（窗口在托盘）")
                            try:
                                if self.tray:
                                    self.tray.show_balloon("盘后复盘完成", "看板已生成，左键托盘图标打开主界面")
                            except Exception:
                                pass
                        else:
                            # 不用模态询问：弹窗不关闭会阻塞 tkinter 事件循环，
                            # 导致看板按钮/托盘动作/控制服务全部失效。
                            self.review_text.insert("end", "\n看板已生成，可点「打开最新看板」查看。\n")
                            LOG("盘后复盘完成，看板已生成（可点「打开最新看板」）")
                    else:
                        self.review_text.insert("end", f"\n❌ 复盘失败（退出码{code}），请查看日志。\n")
                self.after(0, done)
            except Exception as e:
                self.after(0, lambda: self.review_text.insert("end", f"\n❌ 异常：{e}\n"))
        threading.Thread(target=work, daemon=True).start()

    def open_report(self):
        try:
            dates = sorted(f for f in os.listdir(C.REPORT_DIR) if f.startswith("sentiment_") and f.endswith(".html"))
            if not dates:
                messagebox.showwarning("无看板", "尚未生成看板，请先运行盘后复盘。")
                return
            path = (C.REPORT_DIR / dates[-1]).resolve()
            webbrowser.open("file:///" + str(path).replace("\\", "/"))
            LOG(f"打开看板：{path.name}")
        except Exception as e:
            messagebox.showerror("错误", str(e))

    # ---------- 参数设置页 ----------
    def _build_settings_tab(self):
        f = self.tab_settings
        ttk.Label(f, text="可视化参数设置（保存后对主程序与盘中监测生效）",
                  font=("Microsoft YaHei", 11, "bold")).pack(anchor="w", pady=8, padx=6)
        canvas = tk.Canvas(f)
        scroll = ttk.Scrollbar(f, orient="vertical", command=canvas.yview)
        self.settings_frame = ttk.Frame(canvas)
        self.settings_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.settings_frame, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self._setting_vars = {}
        self._render_settings_fields()

        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=8, padx=6)
        ttk.Button(bar, text="💾 保存设置", command=self.save_settings).pack(side="left", padx=4)
        ttk.Button(bar, text="↺ 恢复默认", command=self.reset_settings).pack(side="left", padx=4)
        ttk.Button(bar, text="应用并重启监测", command=self.apply_settings_restart).pack(side="left", padx=4)

    def _render_settings_fields(self):
        for w in self.settings_frame.winfo_children():
            w.destroy()
        self._setting_vars = {}
        cur = CFG.load()
        row = 0
        for key, (label, typ, _, desc) in CFG.FIELDS.items():
            ttk.Label(self.settings_frame, text=label, font=("Microsoft YaHei", 10, "bold")).grid(row=row, column=0, sticky="w", padx=8, pady=(8, 0))
            ttk.Label(self.settings_frame, text=desc, foreground="#888").grid(row=row+1, column=0, sticky="w", padx=8)
            val = cur.get(key)
            if typ == "bool":
                var = tk.BooleanVar(value=bool(val))
                ttk.Checkbutton(self.settings_frame, variable=var).grid(row=row, column=1, sticky="w", padx=8)
            elif typ == "list":
                var = tk.StringVar(value=",".join(val or []))
                e = ttk.Entry(self.settings_frame, textvariable=var, width=18)
                e.grid(row=row, column=1, sticky="w", padx=8)
            else:
                var = tk.StringVar(value=str(val))
                e = ttk.Entry(self.settings_frame, textvariable=var, width=18)
                e.grid(row=row, column=1, sticky="w", padx=8)
            self._setting_vars[key] = (var, typ)
            row += 2

    def open_settings(self):
        nb = self.nametowidget(self.tab_settings.winfo_parent())
        nb.select(self.tab_settings)

    def save_settings(self):
        cur = CFG.load()
        for key, (var, typ) in self._setting_vars.items():
            raw = var.get()
            try:
                if typ == "int":
                    cur[key] = int(float(raw))
                elif typ == "float":
                    cur[key] = float(raw)
                elif typ == "bool":
                    cur[key] = bool(raw)
                elif typ == "list":
                    cur[key] = [x.strip().upper() for x in raw.replace("，", ",").split(",") if x.strip()]
            except Exception:
                messagebox.showerror("输入错误", f"参数 {key} 的值不合法：{raw}")
                return
        CFG.save(cur)
        messagebox.showinfo("已保存", "设置已保存到本地，对主程序与盘中监测生效。")
        LOG("用户设置已保存")

    def reset_settings(self):
        if messagebox.askyesno("确认", "恢复所有参数为默认值？"):
            CFG.reset()
            self._render_settings_fields()
            LOG("设置已恢复默认")

    def apply_settings_restart(self):
        self.save_settings()
        if self.monitor.running:
            self.monitor.stop()
            self.monitor.start()
        messagebox.showinfo("已应用", "设置已应用，盘中监测已按新参数重启。")

    # ---------- 日志页 ----------
    def _build_log_tab(self):
        f = self.tab_log
        self.log_text = scrolledtext.ScrolledText(f, height=24, wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=8, pady=8)
        LOG.cb = self._append_log

    def _append_log(self, line):
        def do():
            self.log_text.insert("end", line + "\n")
            self.log_text.see("end")
        self.after(0, do)


def main():
    if not tk:
        print("需要 tkinter 才能运行桌面应用。请使用系统 Python 3.10 或打包后的 EXE。")
        sys.exit(1)
    if C.ensure_seed_data():
        LOG("首次启动：已载入内置初始数据（观察池/自选池/调参）")
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
