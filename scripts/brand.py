"""Builds the brand images from the approved Tape Chart brand kit (needs Pillow).

Run: python scripts/brand.py <path to Tape-Chart-Brand-Kit>

Writes the same set to two places:
- custom_components/tapechart/brand/  — served by Home Assistant 2026.3+ itself
- brands/custom_integrations/tapechart/ — ready for a home-assistant/brands PR
  (for Home Assistant versions before 2026.3)

icon = the navy rounded-square mark (256 / 512, square).
logo = the three bars alone on a transparent background, trimmed (height 256 / 512):
there is no approved wordmark, and the bare bars read on light and dark backgrounds,
so no dark_ variants are needed.
"""

from pathlib import Path
import sys

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
OUTS = [
    ROOT / "custom_components" / "tapechart" / "brand",
    ROOT / "brands" / "custom_integrations" / "tapechart",
]


def logo(kit: Path, height: int) -> Image.Image:
    """The transparent bars, trimmed to their own bounds, scaled to a height."""
    src = Image.open(
        kit / "02-png/logo-transparent-tight/logo-transparent-tight-1800.png"
    )
    src = src.convert("RGBA")
    src = src.crop(src.getchannel("A").getbbox())
    width = round(src.width * height / src.height)
    return src.resize((width, height), Image.Resampling.LANCZOS)


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: python scripts/brand.py <Tape-Chart-Brand-Kit>")
    kit = Path(sys.argv[1])
    icons = {
        "icon.png": Image.open(kit / "02-png/logo-primary/logo-primary-256.png"),
        "icon@2x.png": Image.open(kit / "02-png/logo-primary/logo-primary-512.png"),
        "logo.png": logo(kit, 256),
        "logo@2x.png": logo(kit, 512),
    }
    for out in OUTS:
        out.mkdir(parents=True, exist_ok=True)
        for name, img in icons.items():
            img.save(out / name, optimize=True)


if __name__ == "__main__":
    main()
