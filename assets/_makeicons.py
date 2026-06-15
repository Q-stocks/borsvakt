"""Engångsgenerator för PWA-ikoner (PNG) – matchar icon.svg. Kräver Pillow.
   python assets/_makeicons.py"""
from PIL import Image, ImageDraw
from pathlib import Path

INK = (13, 20, 32, 255)      # #0d1420
GOLD = (233, 185, 73, 255)   # #e9b949
HERE = Path(__file__).resolve().parent


def render(size: int) -> Image.Image:
    s = 512
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, s, s], radius=112, fill=INK)
    bars = [(138, 300, 196, 398), (227, 232, 285, 398), (316, 150, 374, 398)]
    for x0, y0, x1, y1 in bars:
        d.rounded_rectangle([x0, y0, x1, y1], radius=12, fill=GOLD)
    d.ellipse([323, 96, 367, 140], fill=GOLD)
    if size != s:
        img = img.resize((size, size), Image.LANCZOS)
    return img


for sz in (180, 192, 512):
    render(sz).save(HERE / f"icon-{sz}.png")
    print(f"wrote icon-{sz}.png")
