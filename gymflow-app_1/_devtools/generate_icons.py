"""Generates the GymFlow PWA icon set (dumbbell mark on the report/UI's navy
brand color). Run once at build time — the output PNGs are committed to
gymflow/static/icons/ so end users don't need Pillow installed to run the app.

    python3 _devtools/generate_icons.py
"""
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(os.path.dirname(HERE), "gymflow", "static", "icons")

NAVY = (31, 41, 55, 255)       # --navy from style.css
ACCENT = (37, 99, 235, 255)    # --accent from style.css
WHITE = (255, 255, 255, 255)


def draw_dumbbell(draw: ImageDraw.ImageDraw, cx, cy, scale):
    """A simple, recognizable dumbbell glyph centered at (cx, cy)."""
    bar_w = scale * 0.72
    bar_h = scale * 0.10
    plate_w = scale * 0.16
    plate_h = scale * 0.46
    inner_w = scale * 0.10
    inner_h = scale * 0.30

    # Bar
    draw.rounded_rectangle(
        [cx - bar_w / 2, cy - bar_h / 2, cx + bar_w / 2, cy + bar_h / 2],
        radius=bar_h / 2, fill=WHITE,
    )
    # Outer plates (left + right)
    for sign in (-1, 1):
        px = cx + sign * (bar_w / 2)
        draw.rounded_rectangle(
            [px - plate_w / 2, cy - plate_h / 2, px + plate_w / 2, cy + plate_h / 2],
            radius=plate_w / 3, fill=WHITE,
        )
        draw.rounded_rectangle(
            [px - inner_w / 2, cy - inner_h / 2, px + inner_w / 2, cy + inner_h / 2],
            radius=inner_w / 3, fill=WHITE,
        )
        # Inner plates, slightly smaller, closer to the bar
        ipx = cx + sign * (bar_w / 2 - plate_w * 0.55)
        ip_h = plate_h * 0.72
        draw.rounded_rectangle(
            [ipx - plate_w * 0.35, cy - ip_h / 2, ipx + plate_w * 0.35, cy + ip_h / 2],
            radius=plate_w / 4, fill=WHITE,
        )


def make_icon(size, maskable=False, filename=None):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if maskable:
        # Maskable icons must fill the full canvas edge-to-edge (OS applies
        # its own mask/crop), and keep the important content inside the
        # ~80% "safe zone" circle.
        draw.rectangle([0, 0, size, size], fill=NAVY)
        safe_scale = size * 0.62
    else:
        radius = size * 0.22
        draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=NAVY)
        safe_scale = size * 0.74

    draw_dumbbell(draw, size / 2, size / 2, safe_scale)
    img.save(filename)
    print("wrote", filename, img.size)


def make_favicon(sizes, filename):
    imgs = []
    for s in sizes:
        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        radius = s * 0.22
        draw.rounded_rectangle([0, 0, s - 1, s - 1], radius=radius, fill=NAVY)
        draw_dumbbell(draw, s / 2, s / 2, s * 0.74)
        imgs.append(img)
    imgs[0].save(filename, format="ICO", sizes=[(s, s) for s in sizes])
    print("wrote", filename)


if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    make_icon(192, maskable=False, filename=os.path.join(OUT_DIR, "icon-192.png"))
    make_icon(512, maskable=False, filename=os.path.join(OUT_DIR, "icon-512.png"))
    make_icon(180, maskable=False, filename=os.path.join(OUT_DIR, "apple-touch-icon.png"))
    make_icon(512, maskable=True, filename=os.path.join(OUT_DIR, "icon-maskable-512.png"))
    make_favicon([16, 32, 48], os.path.join(OUT_DIR, "favicon.ico"))
