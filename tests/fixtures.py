"""Builds synthetic test PDFs covering the image types the engine must handle."""

from __future__ import annotations

import io
import random
import zlib
from collections.abc import Callable
from pathlib import Path

import pikepdf
from pikepdf import Name
from PIL import Image, ImageDraw, ImageFilter

LETTER = (612, 792)
TEXT = "Fixture text stays searchable"


# ---------------------------------------------------------------------------- images


def photo(width: int, height: int, seed: int = 1) -> Image.Image:
    """Photo-like RGB image: smooth color gradients, shapes, and sensor-like noise."""
    rnd = random.Random(seed)
    gradient = Image.linear_gradient("L").resize((width, height))
    radial = Image.radial_gradient("L").resize((width, height))
    img = Image.merge("RGB", (gradient, radial, gradient.transpose(Image.Transpose.FLIP_LEFT_RIGHT)))
    draw = ImageDraw.Draw(img)
    for _ in range(25):
        x, y = rnd.randrange(width), rnd.randrange(height)
        r = rnd.randrange(width // 20, width // 5)
        color = tuple(rnd.randrange(256) for _ in range(3))
        draw.ellipse((x - r, y - r, x + r, y + r), fill=color)
    img = img.filter(ImageFilter.GaussianBlur(width / 300))
    noise = Image.effect_noise((width, height), 18).convert("RGB")
    return Image.blend(img, noise, 0.12)


def scan(width: int, height: int, mode: str = "L", seed: int = 2) -> Image.Image:
    """Scanned document page: off-white paper, dark text lines, a little noise."""
    rnd = random.Random(seed)
    img = Image.new("RGB", (width, height), (246, 244, 238))
    draw = ImageDraw.Draw(img)
    line_h = height // 50
    for row in range(4, 46):
        x = width // 10
        while x < width * 0.85:
            word = rnd.randrange(width // 40, width // 12)
            draw.rectangle((x, row * line_h, x + word, row * line_h + line_h // 2), fill=(30, 30, 35))
            x += word + width // 60
    noise = Image.effect_noise((width, height), 10).convert("RGB")
    img = Image.blend(img, noise, 0.15)
    return img.convert(mode)


def screenshot(width: int, height: int) -> Image.Image:
    """Flat UI graphic with a handful of exact colors (no antialiasing)."""
    img = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, width, height // 8), fill=(32, 64, 160))
    for i in range(6):
        y = height // 6 + i * height // 8
        draw.rectangle((width // 12, y, width * 11 // 12, y + height // 14), fill=(230, 236, 245))
        draw.rectangle(
            (width // 12, y, width // 12 + width * (i + 2) // 10, y + height // 14), fill=(46, 160, 67)
        )
    return img


def jpeg_bytes(img: Image.Image, quality: int = 95) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


# ---------------------------------------------------------------------------- PDF building


def _set(obj: pikepdf.Object, **entries) -> None:
    for key, value in entries.items():
        obj[Name("/" + key)] = value


class Builder:
    def __init__(self) -> None:
        self.pdf = pikepdf.new()
        self.font = self.pdf.make_indirect(
            pikepdf.Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica)
        )

    def jpeg_image(self, img: Image.Image, quality: int = 95, **extra) -> pikepdf.Stream:
        colorspace = {"L": Name.DeviceGray, "RGB": Name.DeviceRGB, "CMYK": Name.DeviceCMYK}[img.mode]
        stream = pikepdf.Stream(self.pdf, jpeg_bytes(img, quality))
        _set(
            stream,
            Type=Name.XObject,
            Subtype=Name.Image,
            Width=img.width,
            Height=img.height,
            ColorSpace=colorspace,
            BitsPerComponent=8,
            Filter=Name.DCTDecode,
            **extra,
        )
        return self.pdf.make_indirect(stream)

    def flate_image(self, img: Image.Image, **extra) -> pikepdf.Stream:
        if img.mode == "1":
            colorspace, bpc = Name.DeviceGray, 1
        else:
            colorspace, bpc = {"L": Name.DeviceGray, "RGB": Name.DeviceRGB}[img.mode], 8
        stream = pikepdf.Stream(self.pdf, zlib.compress(img.tobytes(), 6))
        _set(
            stream,
            Type=Name.XObject,
            Subtype=Name.Image,
            Width=img.width,
            Height=img.height,
            ColorSpace=colorspace,
            BitsPerComponent=bpc,
            Filter=Name.FlateDecode,
            **extra,
        )
        return self.pdf.make_indirect(stream)

    def form(self, content: bytes, xobjects: dict, matrix=None, bbox=(0, 0, 1000, 1000)) -> pikepdf.Stream:
        stream = pikepdf.Stream(self.pdf, content)
        _set(
            stream,
            Type=Name.XObject,
            Subtype=Name.Form,
            BBox=pikepdf.Array(bbox),
            Resources=pikepdf.Dictionary(XObject=pikepdf.Dictionary(xobjects)),
        )
        if matrix:
            stream.Matrix = pikepdf.Array(matrix)
        return self.pdf.make_indirect(stream)

    def page(self, draws: list[tuple[pikepdf.Stream, tuple[float, float, float, float]]], text: bool = True):
        """Add a page drawing each (xobject, (x, y, w, h)) plus a line of real text."""
        xobjects = {}
        ops = []
        for i, (xobj, (x, y, w, h)) in enumerate(draws):
            name = f"/X{i}"
            xobjects[name] = xobj
            ops.append(f"q {w} 0 0 {h} {x} {y} cm {name} Do Q")
        if text:
            ops.append(f"BT /F1 14 Tf 72 40 Td ({TEXT}) Tj ET")
        page = pikepdf.Dictionary(
            Type=Name.Page,
            MediaBox=pikepdf.Array([0, 0, *LETTER]),
            Resources=pikepdf.Dictionary(
                XObject=pikepdf.Dictionary(xobjects), Font=pikepdf.Dictionary(F1=self.font)
            ),
            Contents=self.pdf.make_stream("\n".join(ops).encode()),
        )
        self.pdf.pages.append(pikepdf.Page(page))

    def save(self, path: Path, **kwargs) -> Path:
        self.pdf.save(path, **kwargs)
        return path


FULL_PAGE = (0, 0, *LETTER)
# 8.5 x 11 in at 300 DPI
SCAN_300 = (2550, 3300)


def make_photo_doc(path: Path) -> Path:
    """Digital document: text plus a 6x4 inch photo stored at ~400 DPI."""
    b = Builder()
    b.page([(b.jpeg_image(photo(2400, 1600), 95), (72, 300, 432, 288))])
    return b.save(path)


def make_rgb_scan(path: Path) -> Path:
    b = Builder()
    for seed in (1, 2):
        b.page([(b.jpeg_image(scan(*SCAN_300, "RGB", seed=seed), 92), FULL_PAGE)], text=False)
    return b.save(path)


def make_gray_scan(path: Path) -> Path:
    b = Builder()
    b.page([(b.flate_image(scan(*SCAN_300, "L")), FULL_PAGE)], text=False)
    return b.save(path)


def make_bilevel_plus_photo(path: Path) -> Path:
    """Page 1: 1-bit scan (must be left byte-identical). Page 2: photo (gets compressed)."""
    b = Builder()
    bilevel = scan(*SCAN_300, "L").point(lambda v: 255 if v > 128 else 0).convert("1")
    b.page([(b.flate_image(bilevel), FULL_PAGE)], text=False)
    b.page([(b.jpeg_image(photo(2400, 1600), 95), (72, 300, 432, 288))])
    return b.save(path)


def make_cmyk(path: Path) -> Path:
    """CMYK JPEG as Photoshop/Acrobat write it: Adobe-inverted data plus an inverting /Decode."""
    b = Builder()
    decode = pikepdf.Array([1, 0] * 4)
    img = b.jpeg_image(photo(2000, 1500).convert("CMYK"), 95, Decode=decode)
    b.page([(img, (72, 300, 400, 300))])
    return b.save(path)


def make_smask(path: Path) -> Path:
    """Photo with a soft-mask (transparency) fading to the left over a colored background."""
    b = Builder()
    w, h = 2000, 1500
    alpha = Image.linear_gradient("L").rotate(90, expand=True).resize((w, h))
    smask = b.flate_image(alpha)
    image = b.jpeg_image(photo(w, h), 95, SMask=smask)
    xobjects = {"/Bg": b.flate_image(Image.new("RGB", (64, 64), (200, 40, 40))), "/Im": image}
    page = pikepdf.Dictionary(
        Type=Name.Page,
        MediaBox=pikepdf.Array([0, 0, *LETTER]),
        Resources=pikepdf.Dictionary(XObject=pikepdf.Dictionary(xobjects)),
        Contents=b.pdf.make_stream(b"q 612 0 0 792 0 0 cm /Bg Do Q q 400 0 0 300 72 300 cm /Im Do Q"),
    )
    b.pdf.pages.append(pikepdf.Page(page))
    return b.save(path)


def make_screenshot(path: Path) -> Path:
    b = Builder()
    b.page([(b.flate_image(screenshot(2400, 1800)), (72, 300, 400, 300))])
    return b.save(path)


def make_reused(path: Path) -> Path:
    """One 2400 px image drawn at 2, 4 and 6 inches wide: 6 in is what matters (400 DPI)."""
    b = Builder()
    img = b.jpeg_image(photo(2400, 1600), 95)
    for width_in in (2, 4, 6):
        b.page([(img, (72, 300, width_in * 72, width_in * 48))])
    return b.save(path)


def make_form(path: Path) -> Path:
    """Image inside a Form XObject; the form's /Matrix scales it to 3 inches wide."""
    b = Builder()
    img = b.jpeg_image(photo(2400, 1600), 95)
    # Inside the form the image is drawn 1000 x 667 units; the form matrix scales by 0.216.
    form = b.form(b"q 1000 0 0 667 0 0 cm /Im Do Q", {"/Im": img}, matrix=[0.216, 0, 0, 0.216, 0, 0])
    b.page([(form, (72, 300, 1, 1))])
    return b.save(path)


def make_owner_restricted(path: Path) -> Path:
    b = Builder()
    b.page([(b.jpeg_image(photo(2400, 1600), 95), (72, 300, 432, 288))])
    enc = pikepdf.Encryption(owner="owner-secret", user="", allow=pikepdf.Permissions(extract=False))
    return b.save(path, encryption=enc)


def make_user_password(path: Path) -> Path:
    b = Builder()
    b.page([(b.jpeg_image(photo(800, 600), 95), (72, 300, 432, 288))])
    return b.save(path, encryption=pikepdf.Encryption(owner="o", user="open-me"))


def make_text_only(path: Path) -> Path:
    b = Builder()
    b.page([])
    return b.save(path)


FIXTURES: dict[str, Callable[[Path], Path]] = {
    "photo_doc": make_photo_doc,
    "rgb_scan": make_rgb_scan,
    "gray_scan": make_gray_scan,
    "bilevel_plus_photo": make_bilevel_plus_photo,
    "cmyk": make_cmyk,
    "smask": make_smask,
    "screenshot": make_screenshot,
    "reused": make_reused,
    "form": make_form,
    "owner_restricted": make_owner_restricted,
}
