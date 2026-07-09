"""从图片生成终端用的 Sixel logo，可控制缩放（宽度）。

Sixel 图在终端里按**原始像素尺寸**渲染——没有终端普遍认账的"运行时缩放"标志，所以
"缩放 logo" = 按目标像素宽度重新编码一张。等价于 `img2sixel -w <宽度>`，但纯 Python +
Pillow，无需外部工具（本机没有 img2sixel / ImageMagick）。

用法：
    python scripts/make_logo.py                       # logo.png → logo.sixel，宽 800，自动裁白边
    python scripts/make_logo.py --width 600           # 想更小就调小
    python scripts/make_logo.py --src other.png --out logo.sixel --no-trim

生成后直接生效（`agents/__main__.py` 的 `_resolve_logo` 会挑到 logo.six / logo.sixel）。
"""

import argparse
import os
import sys

try:
    from PIL import Image, ImageChops
except ImportError:
    sys.exit("需要 Pillow：pip install pillow")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def autotrim(img: "Image.Image", tol: int = 12) -> "Image.Image":
    """裁掉与左上角同色的均匀外边框（近似原 logo 的紧凑裁切）。"""
    rgb = img.convert("RGB")
    bg = Image.new("RGB", rgb.size, rgb.getpixel((0, 0)))
    diff = ImageChops.difference(rgb, bg)
    diff = ImageChops.add(diff, diff, 2.0, -tol)  # 放大差异、吃掉 tol 以内的噪声
    bbox = diff.getbbox()
    return img.crop(bbox) if bbox else img


def _emit(ch: str, count: int) -> str:
    """Sixel RLE：连续 ≥4 个同字符用 `!count char`，否则直接重复。"""
    return ("!%d%s" % (count, ch)) if count >= 4 else ch * count


def encode_sixel(img: "Image.Image", max_colors: int = 256) -> bytes:
    """把 RGB 图编码成 Sixel 字节（先量化到 ≤max_colors 调色板）。"""
    img = img.convert("RGB")
    # dither=NONE：logo 是平涂插画，不抖动 → 平整色块，RLE 高效、体积小、观感更干净
    pal = img.quantize(colors=max_colors, method=Image.MEDIANCUT, dither=Image.Dither.NONE)
    w, h = pal.size
    palette = pal.getpalette()
    px = list(pal.tobytes())  # 行主序的调色板索引（P 模式一像素一字节）

    out = ["\x1bPq", '"1;1;%d;%d' % (w, h)]
    for i in sorted(set(px)):  # 只定义实际用到的颜色（RGB 0..100）
        r, g, b = palette[i * 3], palette[i * 3 + 1], palette[i * 3 + 2]
        out.append("#%d;2;%d;%d;%d" % (i, round(r * 100 / 255),
                                       round(g * 100 / 255), round(b * 100 / 255)))

    for top in range(0, h, 6):  # 每 6 行一个 band
        rows = min(6, h - top)
        band = [px[(top + r) * w:(top + r) * w + w] for r in range(rows)]
        colors = sorted(set().union(*(set(row) for row in band)))
        for color in colors:
            # 先算这一 band 里该颜色每列的 6-bit 掩码，并记住最后一个非空列
            bits_row = [0] * w
            last = -1
            for r in range(rows):
                bit = 1 << r
                row = band[r]
                for x in range(w):
                    if row[x] == color:
                        bits_row[x] |= bit
                        last = x if x > last else last
            if last < 0:
                continue  # 该颜色在此 band 不出现（保险）
            out.append("#%d" % color)
            # 只编码到 last 列：尾部空 sixel（其它颜色/背景会覆盖）直接丢，大幅压缩体积
            prev, count, line = None, 0, []
            for x in range(last + 1):
                ch = chr(63 + bits_row[x])
                if ch == prev:
                    count += 1
                else:
                    if prev is not None:
                        line.append(_emit(prev, count))
                    prev, count = ch, 1
            if prev is not None:
                line.append(_emit(prev, count))
            out.append("".join(line))
            out.append("$")  # 回车：同 band 叠加下一个颜色（多一个无害，解析器忽略空行）
        out.append("-")      # 换行：下一个 band
    out.append("\x1b\\")     # ST 结束
    return "".join(out).encode("latin-1")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成可缩放的 Sixel logo")
    ap.add_argument("--src", default=os.path.join(_ROOT, "logo.png"), help="源图片（默认 logo.png）")
    ap.add_argument("--out", default=os.path.join(_ROOT, "logo.sixel"), help="输出 Sixel（默认 logo.sixel）")
    ap.add_argument("--width", type=int, default=800, help="目标像素宽度（缩放，默认 800）")
    ap.add_argument("--no-trim", action="store_true", help="不自动裁边")
    ap.add_argument("--max-colors", type=int, default=128,
                    help="调色板颜色数上限（≤256；越小体积越小，平涂 logo 128 足够）")
    args = ap.parse_args(argv)

    img = Image.open(args.src)
    # 有透明通道 → 先合成到白底，避免量化出脏边
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img).convert("RGB")
    else:
        img = img.convert("RGB")

    if not args.no_trim:
        img = autotrim(img)
    w, h = img.size
    if args.width and args.width < w:
        img = img.resize((args.width, round(h * args.width / w)), Image.LANCZOS)

    data = encode_sixel(img, args.max_colors)
    with open(args.out, "wb") as f:
        f.write(data)
    print("已写入 %s：%dx%d 像素，%d 字节" % (args.out, img.size[0], img.size[1], len(data)))
    print("换终端跑 `capybara` 即可看到；想更大/更小改 --width 重跑。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
