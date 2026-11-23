# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：A股情绪监测桌面应用（onedir 模式，数据落地 EXE 同级）。"""
from PyInstaller.utils.hooks import collect_data_files, collect_submodules
from pathlib import Path

datas = []
# EXE 首次启动的初始快照（观察池/自选池/调参），避免盘中监测标的为 0
for _seed in sorted(Path("seed_data").glob("*")):
    if _seed.is_file():
        datas.append((str(_seed), "seed_data"))
datas += collect_data_files('akshare')
datas += collect_data_files('pandas')
datas += collect_data_files('requests')
datas += [('assets/app.ico', 'assets')]   # EXE 图标 + 托盘图标
# akshare 某些接口按注册表调用子模块，显式收集避免运行时缺模块
hidden = collect_submodules('akshare') + [
    'pandas', 'numpy', 'requests', 'lxml', 'bs4', 'openpyxl', 'html5lib',
    'urllib3', 'certifi', 'charset_normalizer', 'idna', 'six', 'dateutil', 'pytz',
    'tkinter', 'tkinter.ttk', 'tkinter.messagebox', 'tkinter.scrolledtext',
    'winsound', 'json', 'http.server', 'webbrowser', 'subprocess',
    'tray_win', 'ctypes',
    # 本项目模块（main 在冻结盘后复盘时动态导入，显式声明确保打进包）
    'config', 'data_fetcher', 'analyzer', 'storage', 'tracking', 'settings',
    'report', 'main', 'auto_tune', 'backtest',
]

a = Analysis(
    ['desktop_app.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'scipy', 'jupyter', 'IPython', 'tensorflow', 'torch', 'cv2'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='A股情绪监测',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 无黑框；如需调试改为 True
    icon='assets/app.ico',
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='A股情绪监测',
)
