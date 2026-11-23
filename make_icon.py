# -*- coding: utf-8 -*-
"""生成「A股情绪监测」专属应用/托盘图标 assets/app.ico。

设计（与「可转债提醒」的亮蓝底+白折线+圆点图标明确区分）：
  深色渐变圆角底 + 红涨绿跌的递增K线蜡烛 + 金色单段上升箭头 + 右上实时信号点。
红=涨、绿=跌遵循A股习惯。1024 超采样绘制，导出 16~256 多尺寸 ICO。
仅用 Pillow，离线生成；改设计后重跑本脚本即可。
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

S = 1024                      # 超采样画布
HERE = Path(__file__).resolve().parent
OUT = HERE / "assets" / "app.ico"


def _grad_bg():
    bg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    px = ImageDraw.Draw(bg)
    top, bot = (10, 26, 48), (22, 58, 104)
    for y in range(S):
        t = y / (S - 1)
        c = tuple(int(top[i] * (1 - t) + bot[i] * t) for i in range(3)) + (255,)
        px.line([(0, y), (S, y)], fill=c)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=224, fill=255)
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(bg, (0, 0), mask)
    return out


def build():
    img = _grad_bg()
    # 所有半透明元素画在独立图层上再合成，避免直接写入 alpha 把背景抹透明
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    # 淡内描边 + 行情网格 + 基线
    d.rounded_rectangle([14, 14, S - 15, S - 15], radius=214,
                        outline=(255, 255, 255, 26), width=5)
    for gy in (430, 620, 810):
        d.line([(120, gy), (S - 120, gy)], fill=(255, 255, 255, 18), width=3)
    d.line([(150, 812), (880, 812)], fill=(255, 255, 255, 55), width=6)

    # 4 根递增K线蜡烛（红涨 / 绿跌），最右最高代表情绪转强
    candles = [
        (235, 610, (232, 71, 75), 560),
        (405, 520, (240, 86, 74), 470),
        (575, 585, (34, 160, 107), 545),   # 绿：跌停/情绪回落
        (745, 385, (255, 91, 74), 330),
    ]
    bw = 96
    for x, top, col, wick in candles:
        cx = x + bw / 2
        d.line([(cx, wick), (cx, 812)], fill=col + (230,), width=10)
        d.rounded_rectangle([x, top, x + bw, 812], radius=16, fill=col + (255,))
        d.rounded_rectangle([x + 10, top + 8, x + bw - 10, top + 26],
                            radius=9, fill=(255, 255, 255, 60))

    # 金色单段上升箭头（不是旧图标那种带圆点的折线）
    ax0, ay0, ax1, ay1 = 175, 690, 855, 205
    d.line([(ax0, ay0), (ax1, ay1)], fill=(255, 201, 77, 255), width=38)
    dx, dy = ax1 - ax0, ay1 - ay0
    ln = math.hypot(dx, dy)
    ux, uy = dx / ln, dy / ln
    px, py = -uy, ux
    tip = (ax1 + ux * 46, ay1 + uy * 46)
    base = (ax1 - ux * 18, ay1 - uy * 18)
    d.polygon([tip,
               (base[0] + px * 62, base[1] + py * 62),
               (base[0] - px * 62, base[1] - py * 62)],
              fill=(255, 214, 110, 255))

    img = Image.alpha_composite(img, layer)

    # 左上实时信号点：橙色光晕 + 实心圆（远离箭头尖端，构图更平衡）
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([62, 108, 186, 232], fill=(255, 150, 60, 190))
    img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(34)))
    d2 = ImageDraw.Draw(img)
    d2.ellipse([96, 142, 152, 198], fill=(255, 176, 82, 255))
    d2.ellipse([114, 160, 134, 180], fill=(255, 255, 255, 235))
    return img


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    src = build()
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    icon = src.resize((256, 256), Image.LANCZOS)
    icon.save(OUT, format="ICO", sizes=sizes)
    icon.save(HERE / "assets" / "_preview.png")
    print("生成:", OUT, OUT.stat().st_size, "bytes, 尺寸档:", sorted(sizes))


if __name__ == "__main__":
    main()
