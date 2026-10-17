"""The LocalNotes logo, drawn from one set of shapes so every format matches.

A glowing emerald tile with a tilted note page: a few text lines, and a voice
waveform that shifts from green ("Me") to violet ("Them"), plus an AI sparkle.

    python logo.py      # regenerates assets/logo.svg, assets/logo.png, assets/localnotes.ico, ui/logo.svg
"""
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ASSETS = Path(__file__).parent / "assets"
S = 1024                                    # design grid

THEMES = {   # tile top-left, tile bottom-right, fold/lines tint, waveform start, waveform end
    "idle": ("#2fd39b", "#0b5e4b", "#cfeee2", "#1f8a66", "#7357d6"),
    "live": ("#ff7a7d", "#a3202b", "#f8d0d2", "#d6333d", "#8a2be2"),
}
TILT = -9                                   # page rotation, degrees
TILE_R = 232
PAGE = (300, 196, 724, 836)
PAGE_R = 58
FOLD = 150
LINES = [(368, 318, 236), (368, 392, 164)]  # x, y, width (detailed sizes only)
LINE_H = 34
BARS = [150, 250, 330, 230, 290, 140]       # waveform heights
BAR_W, BAR_GAP, BAR_CY = 40, 24, 610
SPARKLES = [(800, 238, 112), (690, 120, 46)]  # cx, cy, radius (the small one is dropped at tiny sizes)


def _hex(c: str) -> tuple[int, int, int]:
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def _mix(a: str, b: str, t: float) -> tuple[int, int, int]:
    return tuple(round(x + (y - x) * t) for x, y in zip(_hex(a), _hex(b)))


def _bars(simple: bool):
    heights = [230, 360, 260] if simple else BARS
    w, gap = (84, 40) if simple else (BAR_W, BAR_GAP)
    cy = 560 if simple else BAR_CY
    total = len(heights) * w + (len(heights) - 1) * gap
    x = (PAGE[0] + PAGE[2]) / 2 - total / 2
    for i, h in enumerate(heights):
        yield x, cy - h / 2, w, h, i / max(1, len(heights) - 1)
        x += w + gap


def _star(cx: float, cy: float, r: float, steps: int = 16) -> list[tuple[float, float]]:
    """Four-point sparkle: quadratic curves between the tips, pinched toward the centre."""
    tips = [(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)]
    pts = []
    for i in range(4):
        (ax, ay), (bx, by) = tips[i], tips[(i + 1) % 4]
        for s in range(steps):
            t = s / steps
            pts.append(((1 - t) ** 2 * ax + 2 * (1 - t) * t * cx + t * t * bx,
                        (1 - t) ** 2 * ay + 2 * (1 - t) * t * cy + t * t * by))
    return pts


def _star_path(cx: float, cy: float, r: float) -> str:
    return (f"M{cx},{cy - r} Q{cx},{cy} {cx + r},{cy} Q{cx},{cy} {cx},{cy + r} "
            f"Q{cx},{cy} {cx - r},{cy} Q{cx},{cy} {cx},{cy - r} Z")


def render(size: int, theme: str = "idle") -> Image.Image:
    """Raster logo at any size (drawn on the 1024 grid, then downsampled for clean edges)."""
    top, bottom, tint, wave0, wave1 = THEMES[theme]
    simple = size <= 32

    # tile: diagonal gradient plus a soft glow in the top-left
    yy, xx = np.mgrid[0:S, 0:S].astype(np.float32)
    t = ((xx + yy) / (2 * S - 2))[..., None]
    rgb = np.array(_hex(top), np.float32) * (1 - t) + np.array(_hex(bottom), np.float32) * t
    glow = np.clip(1 - np.hypot(xx - 260, yy - 220) / 620, 0, 1)[..., None] ** 2
    rgb = rgb + (255 - rgb) * glow * 0.28
    tile = Image.fromarray(rgb.astype(np.uint8), "RGB")
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, S - 1, S - 1), radius=TILE_R, fill=255)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(tile, (0, 0), mask)

    # page layer, drawn upright then tilted
    x0, y0, x1, y1 = PAGE
    page_mask = Image.new("L", (S, S), 0)
    pd = ImageDraw.Draw(page_mask)
    pd.rounded_rectangle(PAGE, radius=PAGE_R, fill=255)
    pd.polygon([(x1 - FOLD, y0 - 2), (x1 + 2, y0 - 2), (x1 + 2, y0 + FOLD)], fill=0)
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    layer.paste((255, 255, 255, 255), (0, 0), page_mask)
    d = ImageDraw.Draw(layer)
    d.polygon([(x1 - FOLD, y0), (x1 - FOLD + 14, y0 + FOLD - 14), (x1, y0 + FOLD)], fill=_hex(tint))
    if not simple:
        for lx, ly, lw in LINES:
            d.rounded_rectangle((lx, ly, lx + lw, ly + LINE_H), radius=LINE_H // 2, fill=_hex(tint))
    for bx, by, bw, bh, f in _bars(simple):
        d.rounded_rectangle((bx, by, bx + bw, by + bh), radius=bw / 2, fill=_mix(wave0, wave1, f))
    layer = layer.rotate(-TILT, resample=Image.BICUBIC, center=(S / 2, S / 2))

    shadow = layer.getchannel("A").filter(ImageFilter.GaussianBlur(30)).point(lambda v: int(v * 0.42))
    img.paste((4, 40, 30, 255), (0, 22), shadow)
    img.alpha_composite(layer)

    # sparkles
    sd = ImageDraw.Draw(img)
    for i, (cx, cy, r) in enumerate(SPARKLES):
        if simple and i:
            continue
        sd.polygon(_star(cx, cy, r * (1.25 if simple else 1)), fill=(255, 255, 255, 255))
    return img.resize((size, size), Image.LANCZOS)


def svg(theme: str = "idle") -> str:
    top, bottom, tint, wave0, wave1 = THEMES[theme]
    x0, y0, x1, y1 = PAGE
    r = PAGE_R
    page = (f"M{x0 + r},{y0} H{x1 - FOLD} L{x1},{y0 + FOLD} V{y1 - r} Q{x1},{y1} {x1 - r},{y1} "
            f"H{x0 + r} Q{x0},{y1} {x0},{y1 - r} V{y0 + r} Q{x0},{y0} {x0 + r},{y0} Z")
    lines = "".join(f'<rect x="{lx}" y="{ly}" width="{lw}" height="{LINE_H}" rx="{LINE_H // 2}" fill="{tint}"/>'
                    for lx, ly, lw in LINES)
    bars = "".join(
        f'<rect x="{bx:.0f}" y="{by:.0f}" width="{bw}" height="{bh}" rx="{bw / 2:.0f}" '
        f'fill="#{"".join(f"{c:02x}" for c in _mix(wave0, wave1, f))}"/>'
        for bx, by, bw, bh, f in _bars(False))
    stars = "".join(f'<path d="{_star_path(cx, cy, rr)}" fill="#fff"/>' for cx, cy, rr in SPARKLES)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {S} {S}" role="img" aria-label="LocalNotes">
  <defs>
    <linearGradient id="tile" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{top}"/><stop offset="1" stop-color="{bottom}"/>
    </linearGradient>
    <radialGradient id="glow" cx="260" cy="220" r="620" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="#fff" stop-opacity="0.28"/><stop offset="1" stop-color="#fff" stop-opacity="0"/>
    </radialGradient>
    <filter id="shadow" x="-25%" y="-25%" width="150%" height="150%">
      <feDropShadow dx="0" dy="22" stdDeviation="30" flood-color="#04281e" flood-opacity="0.42"/>
    </filter>
  </defs>
  <rect width="{S}" height="{S}" rx="{TILE_R}" fill="url(#tile)"/>
  <rect width="{S}" height="{S}" rx="{TILE_R}" fill="url(#glow)"/>
  <g transform="rotate({TILT} {S / 2} {S / 2})" filter="url(#shadow)">
    <path d="{page}" fill="#fff"/>
    <path d="M{x1 - FOLD},{y0} L{x1 - FOLD + 14},{y0 + FOLD - 14} L{x1},{y0 + FOLD} Z" fill="{tint}"/>
    {lines}
    {bars}
  </g>
  {stars}
</svg>
"""


def build():
    ASSETS.mkdir(exist_ok=True)
    (ASSETS / "logo.svg").write_text(svg(), encoding="utf-8")
    (ASSETS.parent / "ui" / "logo.svg").write_text(svg(), encoding="utf-8")   # the UI loads it from its own folder
    render(512).save(ASSETS / "logo.png", optimize=True)
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    frames = [render(s) for s in sizes]
    frames[-1].save(ASSETS / "localnotes.ico", sizes=[(s, s) for s in sizes], append_images=frames[:-1])
    print("wrote", ", ".join(p.name for p in sorted(ASSETS.iterdir())))


if __name__ == "__main__":
    build()
