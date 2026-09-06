# -*- coding: utf-8 -*-
"""
make_icon.py —— 生成应用图标（assets/app.ico 多尺寸 + assets/app_icon.png）

运行方式：
    python make_icon.py        （需已安装 PySide6）

说明：
  - 用 PySide6(QImage/QPainter) 绘制 256px 主图，再缩放出多个尺寸；
  - 用纯 Python 手写 ICO 容器（32 位 BGRA + AND 掩码，标准格式，
    Windows / PyInstaller / Qt 均可识别）；
  - 想换图标：修改 draw_icon() 或改成加载自己的 256px 图片，重跑本脚本即可。
"""
from __future__ import annotations

import os
import struct

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
)

# 输出目录
HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(HERE, "assets")

# 需要生成的尺寸
ICON_SIZES = [16, 24, 32, 48, 64, 128, 256]


# ---------------------------------------------------------------------------
# 绘制图标
# ---------------------------------------------------------------------------
def draw_icon(size: int) -> QImage:
    """在 size×size 透明画布上画一个“下载箭头”图标。"""
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)

    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s = float(size)

    # —— 背景：圆角方块 + 蓝紫渐变 ——
    margin = s * 0.06
    rect = QRectF(margin, margin, s - 2.0 * margin, s - 2.0 * margin)
    radius = s * 0.22
    grad = QLinearGradient(0.0, margin, 0.0, s - margin)
    grad.setColorAt(0.0, QColor("#4b8dff"))
    grad.setColorAt(1.0, QColor("#1e4fd0"))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(grad)
    p.drawRoundedRect(rect, radius, radius)

    # —— 顶部高光（增强立体感）——
    hl = QRectF(s * 0.10, s * 0.08, s * 0.80, s * 0.14)
    hl_grad = QLinearGradient(0.0, hl.top(), 0.0, hl.bottom())
    hl_grad.setColorAt(0.0, QColor(255, 255, 255, 130))
    hl_grad.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.setBrush(hl_grad)
    p.drawRoundedRect(hl, s * 0.10, s * 0.10)

    # —— 白色下载箭头 ——
    white = QColor("#ffffff")
    p.setBrush(white)
    p.setPen(Qt.PenStyle.NoPen)

    # 箭杆
    p.drawRect(QRectF(s * 0.44, s * 0.24, s * 0.12, s * 0.30))
    # 箭头（三角）
    head = QPainterPath()
    head.moveTo(s * 0.28, s * 0.48)
    head.lineTo(s * 0.72, s * 0.48)
    head.lineTo(s * 0.50, s * 0.74)
    head.closeSubpath()
    p.drawPath(head)
    # 托盘线
    p.drawRoundedRect(QRectF(s * 0.16, s * 0.80, s * 0.68, s * 0.10),
                      s * 0.05, s * 0.05)

    p.end()
    return img


# ---------------------------------------------------------------------------
# 把 QImage 打包成标准 .ico
# ---------------------------------------------------------------------------
def _bgra_rows(img: QImage) -> bytes:
    """把 QImage 转成自下而上的 32 位 BGRA 像素流。"""
    w, h = img.width(), img.height()
    rows = []
    for y in range(h - 1, -1, -1):          # ICO 像素行自下而上存储
        row = bytearray()
        for x in range(w):
            c = img.pixelColor(x, y)
            row.append(c.blue())
            row.append(c.green())
            row.append(c.red())
            row.append(c.alpha())
        rows.append(bytes(row))
    return b"".join(rows)


def _and_mask_bytes(w: int, h: int) -> bytes:
    """1bpp 的 AND 掩码；32 位色带 alpha 时填 0 即可。"""
    row_bytes = ((w + 31) // 32) * 4
    return b"\x00" * (row_bytes * h)


def build_ico(images) -> bytes:
    """把多张 QImage 打包成一个 .ico 文件。"""
    count = len(images)
    entries = []
    blobs = []
    offset = 6 + 16 * count

    for img in images:
        w, h = img.width(), img.height()
        # BITMAPINFOHEADER（40 字节）
        header = struct.pack(
            "<IiiHHIIiiII",
            40,            # biSize
            w,             # biWidth
            h * 2,         # biHeight（XOR + AND 两段）
            1,             # biPlanes
            32,            # biBitCount
            0,             # biCompression = BI_RGB
            0, 0, 0, 0, 0, # 其余字段留 0
        )
        blob = header + _bgra_rows(img) + _and_mask_bytes(w, h)
        blobs.append(blob)

        entries.append(struct.pack(
            "<BBBBHHII",
            0 if w >= 256 else w,   # bWidth（0 表示 256）
            0 if h >= 256 else h,   # bHeight
            0,                      # bColorCount
            0,                      # bReserved
            1,                      # wPlanes
            32,                     # wBitCount
            len(blob),
            offset,
        ))
        offset += len(blob)

    header = struct.pack("<HHH", 0, 1, count)  # ICONDIR
    return header + b"".join(entries) + b"".join(blobs)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    os.makedirs(ASSETS_DIR, exist_ok=True)

    # 先画 256px 主图，再平滑缩放到其它尺寸
    base = draw_icon(256)
    images = []
    for size in ICON_SIZES:
        if size == 256:
            images.append(base)
        else:
            images.append(base.scaled(size, size,
                                      Qt.AspectRatioMode.IgnoreAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation))

    ico_path = os.path.join(ASSETS_DIR, "app.ico")
    with open(ico_path, "wb") as f:
        f.write(build_ico(images))

    png_path = os.path.join(ASSETS_DIR, "app_icon.png")
    base.save(png_path, "PNG")

    print(f"OK: {ico_path} ({len(ICON_SIZES)} sizes)")
    print(f"OK: {png_path} (256x256)")


if __name__ == "__main__":
    main()