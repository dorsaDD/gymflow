"""Generates GymFlow's PWA icon set + favicon from the app's own dumbbell
mark (the same path used by _icons.html's 'dumbbell' glyph) on the new
"modern & energetic" brand gradient, so the installed-app icon actually
matches the in-app brand instead of being a generic placeholder.

Run once: python3 _devtools/gen_icons.py
"""
import os

import cairosvg

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "gymflow", "static", "icons")
os.makedirs(OUT, exist_ok=True)

# Same stroke path as _icons.html's dumbbell glyph (viewBox 0 24x24),
# recentered/scaled inside a square canvas with the brand gradient behind it.
DUMBBELL_PATH = "M6 7v10M4 9v6M20 7v10M22 9v6M8 12h8"


def svg_icon(size, *, maskable=False, bg_padding_frac=0.0):
    """A rounded-square gradient tile with the dumbbell mark centered.
    maskable=True adds the extra safe-zone padding Android's adaptive-icon
    spec wants (icon content kept inside the inner ~66% circle).
    """
    pad = size * (0.22 if maskable else 0.09)
    inner = size - 2 * pad
    scale = inner / 24
    radius = 0 if maskable else size * 0.22
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 {size} {size}">
  <defs>
    <linearGradient id="g" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#ff5630"/>
      <stop offset="50%" stop-color="#ff3d77"/>
      <stop offset="100%" stop-color="#7c3aed"/>
    </linearGradient>
  </defs>
  <rect x="0" y="0" width="{size}" height="{size}" rx="{radius}" fill="url(#g)"/>
  <g transform="translate({pad},{pad}) scale({scale})" fill="none" stroke="#ffffff"
     stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round">
    <path d="{DUMBBELL_PATH}"/>
  </g>
</svg>"""


def write_png(svg_str, path, size):
    cairosvg.svg2png(bytestring=svg_str.encode("utf-8"), write_to=path,
                      output_width=size, output_height=size)
    print("wrote", path, size)


write_png(svg_icon(192), os.path.join(OUT, "icon-192.png"), 192)
write_png(svg_icon(512), os.path.join(OUT, "icon-512.png"), 512)
write_png(svg_icon(512, maskable=True), os.path.join(OUT, "icon-maskable-512.png"), 512)
write_png(svg_icon(180), os.path.join(OUT, "apple-touch-icon.png"), 180)

# favicon.ico: a small multi-res ICO built from the same PNG renders.
from PIL import Image  # noqa: E402

sizes = [16, 32, 48]
imgs = []
for s in sizes:
    tmp = os.path.join(OUT, f"_fav_{s}.png")
    write_png(svg_icon(s), tmp, s)
    imgs.append(Image.open(tmp).convert("RGBA"))
imgs[0].save(os.path.join(OUT, "favicon.ico"), format="ICO",
             sizes=[(s, s) for s in sizes], append_images=imgs[1:])
for s in sizes:
    os.remove(os.path.join(OUT, f"_fav_{s}.png"))
print("wrote", os.path.join(OUT, "favicon.ico"))
