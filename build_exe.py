# -*- coding: utf-8 -*-
"""安全打包：PyInstaller 的 COLLECT 会清空 dist/A股情绪监测，
本脚本先备份该目录下的 data/ 与 reports/，构建完成后原样恢复，
避免每次重新打包都丢失用户的自选池、跟踪记录和看板。

用法：python build_exe.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "dist" / "A股情绪监测"
BACKUP = ROOT / "dist" / "_A股情绪监测_数据备份"
KEEP = ("data", "reports")


def main():
    if BACKUP.exists():
        shutil.rmtree(BACKUP)
    BACKUP.mkdir(parents=True, exist_ok=True)

    saved = []
    for name in KEEP:
        src = TARGET / name
        if src.exists():
            dst = BACKUP / name
            shutil.copytree(src, dst, dirs_exist_ok=True)
            saved.append(name)

    cmd = [sys.executable, "-m", "PyInstaller", "build.spec", "--noconfirm"]
    print("[build]", " ".join(cmd), flush=True)
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        print(f"[build] 失败（退出码 {r.returncode}），备份保留在 {BACKUP}")
        return r.returncode

    for name in saved:
        dst = TARGET / name
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copytree(BACKUP / name, dst, dirs_exist_ok=True)
    shutil.rmtree(BACKUP, ignore_errors=True)
    print(f"[build] 完成，已恢复本地数据目录：{saved}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
