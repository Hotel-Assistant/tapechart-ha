"""Draws the brand images (a tape chart: rows of booking bars) with Pillow.

Run: python scripts/brand.py  (needs Pillow)
"""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = (
    Path(__file__).resolve().parent.parent / "custom_components" / "tapechart" / "brand"
)
INK = (15, 23, 42, 255)  # slate-900 tile
BARS = [(249, 115, 22, 255), (96, 165, 250, 255), (226, 232, 240, 255)]


def icon(size: int) -> Image.Image:
    """Square icon: three rows of slanted booking bars on a rounded tile."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 256
    d.rounded_rectangle((8 * s, 8 * s, 248 * s, 248 * s), radius=48 * s, fill=INK)
    rows = [(58, 48, 172), (110, 100, 212), (162, 68, 160)]
    for (y, x0, x1), color in zip(rows, BARS, strict=True):
        h, k = 36 * s, 7 * s
        y0, y1 = y * s, y * s + h
        d.polygon(
            [(x0 * s + k, y0), (x1 * s + k, y0), (x1 * s - k, y1), (x0 * s - k, y1)],
            fill=color,
        )
    return img


def logo(height: int) -> Image.Image:
    """Wide logo: the icon only (no text, so no font dependency)."""
    width = int(height * 2)
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    img.paste(icon(height), ((width - height) // 2, 0))
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    icon(256).save(OUT / "icon.png", optimize=True)
    icon(512).save(OUT / "icon@2x.png", optimize=True)
    logo(256).save(OUT / "logo.png", optimize=True)
    logo(512).save(OUT / "logo@2x.png", optimize=True)


if __name__ == "__main__":
    main()
