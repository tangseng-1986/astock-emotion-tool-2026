# 贡献指南

欢迎参与 A股市场情绪强弱分析工具的改进。这个项目完全在本地运行，只有行情获取需要联网。无论你是修 bug、补文档还是加功能，都很有帮助。

## 一、开始之前

本项目基于公开行情与量化规则，仅供研究学习，**不构成投资建议**。贡献时请遵守同样的原则：不要提交任何真实持仓、账户或敏感个人信息。

## 二、开发环境

- **操作系统**：主要面向 Windows（系统托盘 `tray_win.py` 使用 Win32 API）。其他平台可运行核心分析模块，但托盘功能不可用。
- **Python**：3.10（GUI 依赖 tkinter，安装 Python 时请勾选 "tcl/tk and IDLE"）。
- **依赖**：pandas、akshare、numpy、requests、Pillow（图标生成）、pyinstaller（打包）。

```bash
python -m pip install --user pandas akshare numpy requests Pillow pyinstaller
```

Windows 命令行直接 `python` 可用；若提示找不到命令，检查 Python 是否已加入 PATH。

## 三、项目结构

| 模块 | 职责 |
|---|---|
| `main.py` | 主入口与命令行参数，串联六个步骤 |
| `config.py` | 路径、阈值、权重等全局配置 |
| `data_fetcher.py` | 涨跌停池、K线、实时行情获取与清洗、交易日历 |
| `analyzer.py` | 趋势分级、破位过滤、弱转强/A杀信号、候选去重 |
| `tracking.py` | 自选池入池后持续跟踪与收益统计 |
| `auto_tune.py` | 基于回测结果的参数敏感性分析与自动调参 |
| `backtest.py` | T+5/T+10/T+20 回测 |
| `storage.py` | `data/state.json` 读写、双池增删、归档、旧格式迁移 |
| `settings.py` | 用户设置持久化 |
| `report.py` | HTML 看板生成与实时同步脚本 |
| `desktop_app.py` | tkinter 桌面应用、本地控制服务、盘中监测 |
| `tray_win.py` | 纯 Win32 系统托盘 |
| `make_icon.py` | 离线生成 app.ico 与预览图 |
| `build_exe.py` / `build.spec` | PyInstaller 打包脚本与配置 |
| `seed_data/` | 首次启动的初始快照（请勿放入真实数据） |
| `reports/` | 看板样例 |
| `assets/` | 图标资源 |

## 四、本地运行

```bash
python desktop_app.py                 # 桌面应用（推荐）
python main.py --help                 # 查看命令行参数
python main.py --mode close --date 20261119   # 指定日期复盘
python main.py --backtest --tune      # 回测并强制重新调参
```

打包 EXE 请务必用 `python build_exe.py`（它会先备份 `data/`、`reports/` 再构建），不要直接跑 PyInstaller 覆盖，否则会清空本地自选池和跟踪数据。

## 五、改代码前必读的口径

这几条是反复踩坑后定下来的，改动时请保持一致性：

1. **时序口径**：收盘报告 = T 日收盘统计，供 T+1 观察；主池是 T 日涨跌停趋势过滤后的前瞻池，第六项是 T-1 池在 T 日的回顾跟踪。
2. **信号分级**：弱转强按「跌停洗盘之前的趋势」分 A/B/C/D；等级 ≠ 可买，可买等级由自动调参或设置窗口决定。
3. **行情同源**：状态、涨跌幅、现价必须来自同一数据源，收盘优先真实涨跌停池，否则会出现「跌停却 +10%」的错配。
4. **参数优先级**：命令行 `--weights` > 设置窗口 > 自动调参 > config 默认值。
5. **破位过滤**是用户风控要求，强制保留，不因回测波动自动放宽。
6. **去重**：同一只股票只在一个池；候选池在清洗和构建两层都要按代码去重。

## 六、贡献流程

1. **Fork** 本仓库到你自己的账号。
2. **Clone** 你的 Fork：`git clone https://github.com/<你的用户名>/astock-emotion-tool-2026.git`
3. **建分支**：`git checkout -b fix/简短描述` 或 `feat/简短描述`。
4. **修改并自测**：至少手动跑一次 `python main.py --date 20261119`，确认看板能生成、无异常堆栈。
5. **提交**：`git commit -m "简短说明改了什么和为什么"`。
6. **推送并提 PR**：push 到你的 Fork，回到本仓库点 "New pull request"，base 选 `main`，compare 选你的分支。

你不需要先问我能不能改——直接提 PR，我来审核。

## 七、编码约定

- 代码注释和文档用中文，与现有风格保持一致。
- 新增阈值请放进 `config.py` 或设置窗口，不要散落在逻辑里写死。
- 涉及行情口径、趋势分级、自选池结构的改动，请在 PR 描述里说明「为什么这样改」以及如何验证。
- **不要提交** `data/` 下的个人数据、`dist/` 打包产物、`.pyc`、`__pycache__`、旧 EXE 备份；这些已由 `.gitignore` 排除。
- 只改文档或加测试也很欢迎，不必是功能改动。

## 八、PR 自检清单

- [ ] 修改 focused，只包含一件事
- [ ] 本地跑通，无报错
- [ ] 未提交个人数据或构建产物
- [ ] PR 描述写清楚改动原因和验证方式
- [ ] 若改变行为，同步更新 README 相关说明

## 九、遇到问题

开一个 Issue，说明复现步骤、期望行为、实际行为，以及你的 Python / akshare 版本。标签 `good first issue` 的任务适合第一次贡献，认领时在 Issue 下留言即可。
