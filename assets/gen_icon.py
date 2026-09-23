"""Иконка BCleaner: тёмный чип + орбита чистки + искры. Без банальных кистей."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
PNG = HERE / "icon.png"
ICO = HERE / "icon.ico"
S = 1024


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def sparkle(d, cx, cy, r, fill):
    k = 0.20
    pts = [(cx, cy - r), (cx + r * k, cy - r * k), (cx + r, cy),
           (cx + r * k, cy + r * k), (cx, cy + r), (cx - r * k, cy + r * k),
           (cx - r, cy), (cx - r * k, cy - r * k)]
    d.polygon(pts, fill=fill)


# фон: вертикальный градиент + скругление
bg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
tmp = Image.new("RGBA", (S, S))
td = ImageDraw.Draw(tmp)
top, bot = (30, 64, 110), (8, 14, 30)
for y in range(S):
    td.line([(0, y), (S, y)], fill=lerp(top, bot, y / S) + (255,))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=232, fill=255)
bg.paste(tmp, (0, 0), mask)

d = ImageDraw.Draw(bg)
CYAN = (34, 211, 238, 255)
CYAN_DIM = (34, 211, 238, 110)

# орбита чистки (кольцо)
d.ellipse([120, 300, 904, 724], outline=CYAN_DIM, width=40)
d.ellipse([120, 300, 904, 724], outline=CYAN, width=10)
# пылинки на орбите
for cx, cy, r in [(200, 640, 26), (830, 400, 22), (760, 660, 16)]:
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=CYAN)

# свечение + главная искра
glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
gd = ImageDraw.Draw(glow)
sparkle(gd, 620, 400, 250, (34, 211, 238, 90))
glow = glow.filter(ImageFilter.GaussianBlur(60))
bg.alpha_composite(glow)
d = ImageDraw.Draw(bg)
sparkle(d, 620, 400, 168, (255, 255, 255, 255))
sparkle(d, 300, 730, 84, (224, 254, 255, 255))
sparkle(d, 800, 730, 62, CYAN)
sparkle(d, 268, 300, 44, (186, 240, 253, 255))

icon = bg.resize((256, 256), Image.LANCZOS)
icon.save(PNG)
icon.save(ICO, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("saved:", PNG, ICO)
