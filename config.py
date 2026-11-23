# -*- coding: utf-8 -*-
"""全局配置：路径、参数阈值、策略口径、可调权重。所有阈值集中于此，便于实盘调优。

v6 变化：
  - 连续大跌剔除增强为"连续N日大跌 OR 近窗口累计大跌"（修复阳光乳业类5日内两次跌停漏剔）
  - 回测周期扩大（90->250交易日）以获得充足的分级样本
  - 持有主线切到 T+20（中线约1个月），趋势分级可买等级可由自动调参覆盖
  - 新增自动调参(auto_tune)、盘中监测(desktop_app)、看板直达控制服务配置
"""
import shutil
import sys
from pathlib import Path

def _app_dir() -> Path:
    """源码运行=项目目录；PyInstaller冻结=EXE所在目录（数据落地EXE同级，真正本地存储）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

BASE_DIR = _app_dir()
APP_SOURCE_DIR = Path(__file__).resolve().parent  # 源码/打包内资源目录
DATA_DIR = BASE_DIR / "data"
REPORT_DIR = BASE_DIR / "reports"
STATE_FILE = DATA_DIR / "state.json"          # 跨日持久化：自选池/信号历史/每日快照
TUNED_PARAMS_FILE = DATA_DIR / "tuned_params.json"   # 自动调参输出（主程序自动加载）
TUNE_HISTORY_FILE = DATA_DIR / "tune_history.json"   # 调参历史与效果跟踪
KLINE_CACHE = DATA_DIR / "kline_cache"
KLINE_CACHE.mkdir(parents=True, exist_ok=True)


def ensure_seed_data():
    """EXE 首次启动：本地 data 为空时，从打包内置的 seed_data 复制初始快照。

    避免刚打开工具盘中监测"标的 0 只"——首次运行也应有上一次收盘生成的
    观察池/自选池作为监测标的。已有本地数据时绝不覆盖用户记录。
    """
    seed = APP_SOURCE_DIR / "seed_data"
    if not seed.is_dir():
        return False
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    copied = False
    files = []
    if not (DATA_DIR / "state.json").exists():
        files.append(seed / "state.json")
    if not any(DATA_DIR.glob("result_*.json")):
        files.extend(sorted(seed.glob("result_*.json")))
    for name in ("tuned_params.json", "tune_history.json"):
        if not (DATA_DIR / name).exists():
            files.append(seed / name)
    for src in files:
        if src.is_file():
            shutil.copy2(src, DATA_DIR / src.name)
            copied = True
    return copied

# ---------- 数据源 ----------
THS_REASON_URL = "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
THS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://data.10jqka.com.cn/",
}
REQUEST_TIMEOUT = 20
MAX_RETRY = 3
KLINE_WORKERS = 4          # K线并发数，过高会触发接口限流

# ---------- 时序口径（核心） ----------
BASE_DAY_OFFSET = 1        # 待选池相对观察日回退的交易日数（T-1）

# ---------- 趋势过滤（近3个月） ----------
TREND_LOOKBACK_DAYS = 300    # 拉取自然日范围（扩大以支撑长周期回测）
TREND_MIN_BARS = 45           # 少于该K线数量视为次新股/数据不足，剔除
MA_SHORT = 20
MA_MID = 60
UP_SLOPE_PCT = 0.04       # 60日斜率需 >4% 才算上升趋势；否则高位横盘归入"高位震荡"
UP_PRICE_ABOVE_MA60 = 1.00
UP_MA20_ABOVE_MA60 = 0.98
HIGH_ZONE_PCT = 0.85
HIGH_MA60_FLOOR = 0.93
HIGH_MAX_DRAWDOWN = 0.18
DOWN_SLOPE_PCT = -0.05
DOWN_PRICE_BELOW_MA60 = 0.97

# ---------- 连续大跌剔除（短期破位风险）v6增强 ----------
BIG_DROP_PCT = 5.0          # 单日跌幅>=该值视为"大跌"
CONSEC_DROP_DAYS = 2        # 连续N日大跌 -> 剔除观察池
DROP_WINDOW_DAYS = 5        # 累计大跌判定窗口（近N个交易日）
WINDOW_DROP_COUNT = 2       # 窗口内大跌次数>=该值 且 累计跌幅达标 -> 剔除
WINDOW_CUM_DROP_PCT = -8.0  # 窗口内累计跌幅<=-8% -> 视为破位（阳光乳业-9.58%命中）
MOMENTUM_LOOKBACK = 5       # 短期动能回看天数

# ---------- 涨跌停判定（按板块） ----------
LIMIT_MAIN = 9.8       # 主板 10%
LIMIT_GEM = 19.8       # 创业板/科创板 20%
LIMIT_BJ = 29.8        # 北交所 30%
REALTIME_LIMIT_TOL = 0.5   # 实时涨停判定容差

# ---------- 可调权重（回测可校验、用户可手调） ----------
WEIGHTS = {
    "trend": 30,        # 趋势强度（斜率 / 均线位置）
    "position": 15,     # 高位位置（距3月高点）
    "limit_quality": 20,  # 封板质量（连板高度 / 封板时间 / 炸板）
    "theme": 15,        # 题材热度（涨停原因题材数量 / 连板梯队）
    "short_momentum": 20,  # 短期动能（近5日收益，惩罚急跌/连续大跌）
}
SCORE_WATCH_MIN = 45    # 综合评分进入观察池的门槛（回测显示门槛不敏感，自动调参会校准）
SCORE_SIGNAL_MIN = 0    # 弱转强信号的最低评分（0=不按评分过滤信号，避免门槛误杀）

# 短期动能评分映射：近5日收益 -8% -> 0分, +10% -> 100分
MOM_LOSS = -8.0
MOM_GAIN = 10.0
CONSEC_DROP_PENALTY = 0.5   # 连续大跌时短期动能因子打折系数（0.5=腰斩）

# ---------- 情绪指标阈值 ----------
HIGH_BOARD_LEVEL = 4
A_KILL_MAX_DD = -0.08

# ---------- 弱转强打板信号 ----------
WEAK_TO_STRONG_WINDOW = 1
# 弱转强按趋势分级（趋势为主）：不同趋势类型的操作价值与建议
# 注意：buy 为"初始默认"，运行时会被 auto_tune 的回测结论覆盖（tuned_params.json）
W2S_Trend_TYPES = {
    "高位震荡": {"grade": "A", "label": "高位平台反包型", "action": "高位平台整理后跌停洗盘再反包，重点观察", "buy": True},
    "上升趋势": {"grade": "B", "label": "上升趋势洗盘型", "action": "趋势未破的跌停洗盘反手涨停，可关注", "buy": True},
    "震荡偏弱": {"grade": "C", "label": "弱势反抽/中线修复型", "action": "中线持有(约1个月)胜率占优，回测确认后纳入中线关注，严设止损", "buy": False},
    "下跌趋势": {"grade": "D", "label": "下跌反转型", "action": "下跌趋势中的反抽，非典型弱转强，仅观察不纳入", "buy": False},
}
W2S_DEFAULT_TYPE = {"grade": "C", "label": "未知趋势型", "action": "趋势数据不足，谨慎观察", "buy": False}
W2S_BUYABLE_GRADES = ("A", "B")   # 默认可买等级（会被自动调参覆盖）

# ---------- 持有周期（中线为主） ----------
HOLD_HORIZON = 20          # 主持有周期 T+20（约1个月），看板与调参以此为主线
BACKTEST_HORIZONS = (5, 10, 20)   # 回测节点：兼顾短/中线，主线T+20（保持三档兼容看板图表）

# ---------- 回测（周期扩大，样本更充足） ----------
BACKTEST_LOOKBACK_DAYS = 250   # 回放最近250个交易日（约1年）
BACKTEST_UNIVERSE_DATES = 45   # 用最近45个交易日的涨停池构建股票宇宙
BACKTEST_UNIVERSE_CAP = 380    # 股票宇宙上限（控制K线拉取耗时）
MIN_GRADE_SAMPLES = 15         # 分级后验最小样本数，低于此不据此调参（防过拟合）

# ---------- 自动调参 ----------
AUTO_TUNE_ENABLED = True       # 主程序是否自动加载 tuned_params.json
AUTO_TUNE_INTERVAL_DAYS = 7    # 定期重调参间隔（自然日）；桌面应用/CLI检查
AUTO_TUNE_SMOOTH = 0.5         # 参数平滑系数：新值=旧*(1-a)+建议*a，避免跳变
TUNE_MIN_SAMPLE = 20           # 调参所需最小信号样本数

# ---------- 自选池与持续跟踪（v7） ----------
WATCH_POOLS = ("auto", "manual")       # 自动自选池（弱转强）/ 手动自选池（看板或界面添加）
POOL_LABELS = {"auto": "自动自选池", "manual": "手动自选池"}
TRACK_HORIZONS = (5, 10, 20)           # 入池后统计节点（交易日），与回测周期一致
TRACK_SNAPSHOT_MAX = 120               # 每只跟踪股最多保留的每日快照数（约半年）
TRACK_LOOKBACK_DAYS = 120              # 跟踪用K线回溯自然日（入池日期早于此时也可按缓存补齐）

# ---------- 盘中监测 / 桌面应用 ----------
REFRESH_INTERVAL_SEC = 45      # 盘中行情刷新间隔（秒）
MONITOR_ALERT_POPUP = True     # 弱转强/A杀是否弹窗提醒
MONITOR_SOUND = True           # 是否声音提醒
CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 18765           # 看板直达按钮 -> 本地控制服务端口（避开常见占用端口）
CONTROL_PORT_FILE = DATA_DIR / "control_port.json"  # 实际端口落盘（占用时自动+1），供看板读取

# ---------- 展示 ----------
TOP_N = 500
