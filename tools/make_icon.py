"""Generate assets/icon.ico: a document being squeezed by two arrows. Run once; output is committed."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 1024  # draw large, then downsample for clean edges at every icon size
BLUE = (37, 99, 235, 255)
WHITE = (255, 255, 255, 255)


def draw() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = SIZE / 256

    def box(x0, y0, x1, y1):
        return (x0 * s, y0 * s, x1 * s, y1 * s)

    # Rounded tile
    d.rounded_rectangle(box(8, 8, 248, 248), radius=48 * s, fill=BLUE)
    # Page with folded corner
    fold = 34
    left, top, right, bottom = 70, 40, 186, 216
    d.polygon(
        [
            (left * s, top * s),
            ((right - fold) * s, top * s),
            (right * s, (top + fold) * s),
            (right * s, bottom * s),
            (left * s, bottom * s),
        ],
        fill=WHITE,
    )
    d.polygon(
        [
            ((right - fold) * s, top * s),
            ((right - fold) * s, (top + fold) * s),
            (right * s, (top + fold) * s),
        ],
        fill=(191, 208, 250, 255),
    )
    # Two chevrons pointing at each other (squeeze)
    cx, w, t = 128, 34, 14

    def chevron(tip_y, direction):
        base_y = tip_y - direction * 26
        pts = [(cx - w, base_y), (cx, tip_y), (cx + w, base_y)]
        d.line([(x * s, y * s) for x, y in pts], fill=BLUE, width=int(t * s), joint="curve")
        for x, y in (pts[0], pts[2]):
            r = t * s / 2
            d.ellipse((x * s - r, y * s - r, x * s + r, y * s + r), fill=BLUE)

    chevron(112, 1)  # upper chevron points down
    chevron(150, -1)  # lower chevron points up
    return img


def main(out: Path) -> None:
    big = draw()
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    frames = [big.resize((n, n), Image.Resampling.LANCZOS) for n in sizes]
    out.parent.mkdir(parents=True, exist_ok=True)
    frames[-1].save(out, format="ICO", sizes=[(n, n) for n in sizes], append_images=frames[:-1])
    big.resize((256, 256), Image.Resampling.LANCZOS).save(out.with_suffix(".png"))


if __name__ == "__main__":
    main(
        Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "assets/icon.ico"
    )
