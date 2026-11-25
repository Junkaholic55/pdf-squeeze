"""PDF compression engine: recompresses and downsamples images, leaves everything else intact.

The engine is pure (no GUI) so it can be tested headless and driven from the CLI self-test.
"""

from __future__ import annotations

import io
import logging
import math
import os
import secrets
import threading
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import pikepdf
from pikepdf import Name
from PIL import Image, ImageChops

from .presets import Preset

log = logging.getLogger(__name__)

# Scanned pages are large; these are the user's own local files, so lift Pillow's
# decompression-bomb guard rather than refusing big scans.
Image.MAX_IMAGE_PIXELS = None

Matrix = tuple[float, float, float, float, float, float]
ObjGen = tuple[int, int]
# progress(stage, done, total): stage is IMAGES (done/total images) or SAVING (percent of 100).
ProgressCallback = Callable[[str, int, int], None]
IMAGES = "images"
SAVING = "saving"

IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

# Only downsample when the image is meaningfully above the target resolution.
DOWNSAMPLE_THRESHOLD = 1.10
# Only replace an image when the new encoding is meaningfully smaller.
MIN_IMAGE_SAVING = 0.90
# Report "already optimized" unless the whole file shrinks by at least this much.
MIN_FILE_SAVING = 0.99
MIN_IMAGE_BYTES = 10 * 1024
MIN_IMAGE_PIXELS = 64
MIN_OUTPUT_PIXELS = 16
MAX_FORM_DEPTH = 16
# zlib level 9 is pathologically slow on large low-entropy scans for ~1% gain.
ZLIB_LEVEL = 6
# Channel difference (0-255) below which an RGB image is treated as grayscale.
GRAY_TOLERANCE = 6
# Images with at most this many colors (gray levels) are "flat" graphics - charts, screenshots,
# logos - and are tried with lossless encoding, which keeps edges crisp.
MAX_FLAT_COLORS = 256
MAX_FLAT_GRAY_LEVELS = 16
# Keep a lossless encoding unless JPEG is smaller by more than this factor.
LOSSLESS_PREFERENCE = 1.25

SUPPORTED_BPC = {2, 4, 8}
SIMPLE_COLORSPACES = {"/DeviceGray", "/DeviceRGB", "/DeviceCMYK", "/CalGray", "/CalRGB"}


class Status(Enum):
    COMPRESSED = "compressed"
    ALREADY_OPTIMIZED = "already_optimized"


class CompressionError(Exception):
    """A problem the user should see, with a human-readable message."""


class PasswordProtectedError(CompressionError):
    pass


class Cancelled(Exception):
    """Raised when the caller's cancel event is set; no output is written."""


@dataclass
class Result:
    status: Status
    original_size: int
    new_size: int
    output_path: Path | None
    images_total: int = 0
    images_recompressed: int = 0
    images_skipped: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def saved_fraction(self) -> float:
        if self.original_size == 0:
            return 0.0
        return 1.0 - self.new_size / self.original_size


# --------------------------------------------------------------------------------------
# Finding how large each image is drawn on the page
# --------------------------------------------------------------------------------------


def _multiply(m: Matrix, n: Matrix) -> Matrix:
    """Return m x n using PDF's row-vector convention (apply m first, then n)."""
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (
        a * a2 + b * c2,
        a * b2 + b * d2,
        c * a2 + d * c2,
        c * b2 + d * d2,
        e * a2 + f * c2 + e2,
        e * b2 + f * d2 + f2,
    )


def _as_matrix(values) -> Matrix | None:
    try:
        nums = tuple(float(v) for v in values)
    except (TypeError, ValueError):
        return None
    if len(nums) != 6 or not all(math.isfinite(v) for v in nums):
        return None
    return nums  # type: ignore[return-value]


class _DrawSizes:
    """Largest size (in points) at which each image object is drawn anywhere in the document."""

    def __init__(self) -> None:
        self.sizes: dict[ObjGen, tuple[float, float]] = {}
        # Images whose draw size could not be determined: never downsample these.
        self.unknown: set[ObjGen] = set()

    def record(self, objgen: ObjGen, width_pt: float, height_pt: float) -> None:
        old_w, old_h = self.sizes.get(objgen, (0.0, 0.0))
        self.sizes[objgen] = (max(old_w, width_pt), max(old_h, height_pt))

    def mark_unknown_in(self, resources) -> None:
        for _, xobj in _iter_xobjects(resources):
            if xobj.objgen != (0, 0):
                self.unknown.add(xobj.objgen)


def _iter_xobjects(resources):
    if not isinstance(resources, pikepdf.Dictionary):
        return
    xobjects = resources.get(Name.XObject)
    if not isinstance(xobjects, pikepdf.Dictionary):
        return
    for name, xobj in xobjects.items():
        if isinstance(xobj, pikepdf.Stream):
            yield name, xobj


def _walk_content(content, resources, ctm: Matrix, sizes: _DrawSizes, stack: set[ObjGen]) -> None:
    """Track the CTM through a content stream and record where images are drawn."""
    try:
        instructions = pikepdf.parse_content_stream(content, "q Q cm Do")
    except Exception:  # malformed content: be conservative with everything it can reach
        log.warning("Could not parse a content stream; its images will not be downsampled")
        sizes.mark_unknown_in(resources)
        return

    xobjects = dict(_iter_xobjects(resources))
    current = ctm
    saved: list[Matrix] = []
    for inst in instructions:
        op = str(inst.operator)
        if op == "q":
            saved.append(current)
        elif op == "Q":
            current = saved.pop() if saved else ctm
        elif op == "cm":
            m = _as_matrix(inst.operands)
            if m is not None:
                current = _multiply(m, current)
        elif op == "Do" and inst.operands:
            xobj = xobjects.get(str(inst.operands[0]))
            if xobj is None:
                continue
            subtype = xobj.get(Name.Subtype)
            if subtype == Name.Image:
                a, b, c, d, _, _ = current
                sizes.record(xobj.objgen, math.hypot(a, b), math.hypot(c, d))
            elif subtype == Name.Form:
                _walk_form(xobj, resources, current, sizes, stack)


def _walk_form(form, parent_resources, ctm: Matrix, sizes: _DrawSizes, stack: set[ObjGen]) -> None:
    form_resources = form.get(Name.Resources, parent_resources)
    if form.objgen in stack or len(stack) >= MAX_FORM_DEPTH:
        sizes.mark_unknown_in(form_resources)
        return
    form_matrix = _as_matrix(form.get(Name.Matrix, IDENTITY)) or IDENTITY
    stack.add(form.objgen)
    try:
        _walk_content(form, form_resources, _multiply(form_matrix, ctm), sizes, stack)
    finally:
        stack.discard(form.objgen)


def _collect_draw_sizes(pdf: pikepdf.Pdf) -> _DrawSizes:
    sizes = _DrawSizes()
    for page in pdf.pages:
        try:
            user_unit = float(page.obj.get(Name.UserUnit, 1))
        except (TypeError, ValueError):
            user_unit = 1.0
        resources = page.resources
        _walk_content(page, resources, (user_unit, 0.0, 0.0, user_unit, 0.0, 0.0), sizes, set())
        # Images in annotation appearances (stamps, signatures) are not tracked: keep full size.
        for annot in page.obj.get(Name.Annots, []):
            appearance = annot.get(Name.AP) if isinstance(annot, pikepdf.Dictionary) else None
            if isinstance(appearance, pikepdf.Dictionary):
                for state in appearance.values():
                    streams = state.values() if isinstance(state, pikepdf.Dictionary) else [state]
                    for stream in streams:
                        if isinstance(stream, pikepdf.Stream):
                            sizes.mark_unknown_in(stream.get(Name.Resources))
    return sizes


# --------------------------------------------------------------------------------------
# Recompressing a single image
# --------------------------------------------------------------------------------------


class _Skip(Exception):
    """This image is left untouched; the message is the reason (for logging and stats)."""


@dataclass
class _Encoded:
    data: bytes
    filter: Name
    colorspace: object
    width: int
    height: int
    decode: pikepdf.Array | None = None


def _colorspace_info(colorspace) -> tuple[str, pikepdf.Object | None]:
    """Return (family, icc_stream) for an image /ColorSpace, raising _Skip if unsupported."""
    if isinstance(colorspace, pikepdf.Name):
        if str(colorspace) in SIMPLE_COLORSPACES:
            return str(colorspace), None
        raise _Skip(f"colorspace {colorspace}")
    if isinstance(colorspace, pikepdf.Array) and len(colorspace) >= 1:
        family = str(colorspace[0])
        if family in ("/CalGray", "/CalRGB"):
            return family, None
        if family == "/ICCBased" and len(colorspace) == 2:
            return family, colorspace[1]
        if family == "/Indexed" and len(colorspace) == 4:
            base_family, _ = _colorspace_info(colorspace[1])
            if base_family == "/Indexed":
                raise _Skip("nested indexed colorspace")
            return family, None
    raise _Skip("unsupported colorspace")


def _decode(image_obj) -> Image.Image:
    """Decode a PDF image to a Pillow image in L, RGB or CMYK mode (true, non-inverted values)."""
    try:
        pil = pikepdf.PdfImage(image_obj).as_pil_image(apply_mask=False)
    except Exception as exc:
        raise _Skip(f"cannot decode ({type(exc).__name__})") from exc

    if pil.mode == "P":
        pil = pil.convert("RGB")
    elif pil.mode in ("LA", "La"):
        pil = pil.convert("L")
    elif pil.mode in ("I;16", "I;16B", "I", "F"):
        raise _Skip("16-bit image")
    elif pil.mode not in ("L", "RGB", "CMYK"):
        pil = pil.convert("RGB")
    return pil


def _is_effectively_gray(pil: Image.Image) -> bool:
    if pil.mode == "L":
        return True
    # Check a reduced copy: fast, and enough to tell a gray scan from a color photo.
    sample = pil.copy()
    sample.thumbnail((512, 512))
    r, g, b = sample.split()
    for x, y in ((r, g), (g, b), (r, b)):
        if ImageChops.difference(x, y).getextrema()[1] > GRAY_TOLERANCE:
            return False
    return True


def _palette_image(colors) -> Image.Image:
    palette = bytearray()
    for _, rgb in colors:
        palette += bytes(rgb)
    pal_img = Image.new("P", (1, 1))
    pal_img.putpalette(palette + bytes(768 - len(palette)))
    return pal_img


def _encode_lossless(pil: Image.Image, colors) -> _Encoded:
    """Lossless encoding for flat graphics: palette-indexed for RGB, plain Flate for gray."""
    if pil.mode == "L":
        return _Encoded(
            zlib.compress(pil.tobytes(), ZLIB_LEVEL), Name.FlateDecode, Name.DeviceGray, *pil.size
        )
    # Every pixel color is in the palette, so nearest-color mapping without dithering is exact
    # (or, after a resize, snaps antialiasing back to the original crisp colors).
    indexed = pil.quantize(palette=_palette_image(colors), dither=Image.Dither.NONE)
    palette = b"".join(bytes(rgb) for _, rgb in colors)
    colorspace = pikepdf.Array([Name.Indexed, Name.DeviceRGB, len(colors) - 1, palette])
    return _Encoded(zlib.compress(indexed.tobytes(), ZLIB_LEVEL), Name.FlateDecode, colorspace, *pil.size)


def _encode_jpeg(pil: Image.Image, quality: int, icc) -> _Encoded:
    buf = io.BytesIO()
    pil.save(buf, format="JPEG", quality=quality, optimize=True)
    channels = len(pil.getbands())
    decode = None
    if icc is not None and int(icc.get(Name.N, 0)) == channels:
        colorspace = pikepdf.Array([Name.ICCBased, icc])  # keep the original color profile
    else:
        colorspace = {1: Name.DeviceGray, 3: Name.DeviceRGB, 4: Name.DeviceCMYK}[channels]
    if channels == 4:
        # Pillow writes CMYK JPEGs Adobe-style (inverted); /Decode tells viewers to undo that,
        # the same convention Photoshop and Acrobat use. Keeping CMYK avoids color shifts.
        decode = pikepdf.Array([1, 0] * 4)
    return _Encoded(buf.getvalue(), Name.DCTDecode, colorspace, *pil.size, decode=decode)


def _target_size(
    width: int, height: int, draw_size: tuple[float, float] | None, preset: Preset
) -> tuple[int, int]:
    if draw_size is None:
        return width, height
    width_pt, height_pt = draw_size
    if width_pt <= 0 or height_pt <= 0:
        return width, height
    dpi = min(width / (width_pt / 72.0), height / (height_pt / 72.0))
    if dpi <= preset.target_dpi * DOWNSAMPLE_THRESHOLD:
        return width, height
    scale = preset.target_dpi / dpi
    return (
        max(MIN_OUTPUT_PIXELS, round(width * scale)),
        max(MIN_OUTPUT_PIXELS, round(height * scale)),
    )


def _recompress_image(image_obj: pikepdf.Stream, draw_size, preset: Preset) -> None:
    """Recompress one image in place, or raise _Skip with the reason it was left alone."""
    width = int(image_obj.get(Name.Width, 0))
    height = int(image_obj.get(Name.Height, 0))
    original_bytes = len(image_obj.read_raw_bytes())

    if image_obj.get(Name.ImageMask, False):
        raise _Skip("stencil mask")
    if width < MIN_IMAGE_PIXELS or height < MIN_IMAGE_PIXELS or original_bytes < MIN_IMAGE_BYTES:
        raise _Skip("small")
    filters = image_obj.get(Name.Filter)
    filter_names = {str(f) for f in (filters if isinstance(filters, pikepdf.Array) else [filters])}
    if "/JBIG2Decode" in filter_names or "/CCITTFaxDecode" in filter_names:
        raise _Skip("bilevel scan")
    if int(image_obj.get(Name.BitsPerComponent, 8)) not in SUPPORTED_BPC:
        raise _Skip("bit depth")
    if isinstance(image_obj.get(Name.Mask), pikepdf.Array):
        raise _Skip("color-key mask")
    smask = image_obj.get(Name.SMask)
    if isinstance(smask, pikepdf.Stream) and Name.Matte in smask:
        raise _Skip("premultiplied soft mask")
    if Name.Alternates in image_obj or Name.OPI in image_obj:
        raise _Skip("alternate images")

    _, icc = _colorspace_info(image_obj.get(Name.ColorSpace))
    pil = _decode(image_obj)

    if pil.mode == "RGB" and _is_effectively_gray(pil):
        pil = pil.convert("L")

    # Count colors before resizing: resampling blends edges into many new in-between colors.
    # JPEG/JPEG2000 sources are photos (or already lossy), so they are never "flat".
    lossy_source = bool(filter_names & {"/DCTDecode", "/JPXDecode"})
    max_colors = MAX_FLAT_GRAY_LEVELS if pil.mode == "L" else MAX_FLAT_COLORS
    colors = None if lossy_source or pil.mode == "CMYK" else pil.getcolors(max_colors)

    new_w, new_h = _target_size(width, height, draw_size, preset)
    resized = (new_w, new_h) != (width, height)
    if resized:
        pil = pil.resize((new_w, new_h), Image.Resampling.LANCZOS, reducing_gap=3.0)

    encoded = _encode_jpeg(pil, preset.jpeg_quality, icc)
    if colors is not None:
        lossless = _encode_lossless(pil, colors)
        if len(lossless.data) <= len(encoded.data) * LOSSLESS_PREFERENCE:
            encoded = lossless

    if len(encoded.data) >= original_bytes * MIN_IMAGE_SAVING:
        raise _Skip("no gain")

    new_smask = None
    if isinstance(smask, pikepdf.Stream) and resized:
        new_smask = _resized_smask(smask, (new_w, new_h))
        if new_smask is None:
            raise _Skip("soft mask not resizable")

    image_obj.write(encoded.data, filter=encoded.filter)
    image_obj[Name.Width] = encoded.width
    image_obj[Name.Height] = encoded.height
    image_obj[Name.ColorSpace] = encoded.colorspace
    image_obj[Name.BitsPerComponent] = 8
    if Name.DecodeParms in image_obj:
        del image_obj[Name.DecodeParms]
    if encoded.decode is not None:
        image_obj[Name.Decode] = encoded.decode
    elif Name.Decode in image_obj:
        del image_obj[Name.Decode]
    if new_smask is not None:
        smask.write(zlib.compress(new_smask.tobytes(), ZLIB_LEVEL), filter=Name.FlateDecode)
        smask[Name.Width], smask[Name.Height] = new_smask.size
        smask[Name.BitsPerComponent] = 8
        smask[Name.ColorSpace] = Name.DeviceGray
        for key in (Name.DecodeParms, Name.Decode):
            if key in smask:
                del smask[key]


def _resized_smask(smask: pikepdf.Stream, size: tuple[int, int]) -> Image.Image | None:
    try:
        mask = pikepdf.PdfImage(smask).as_pil_image()
    except Exception:
        return None
    if mask.mode != "L":
        mask = mask.convert("L")
    return mask.resize(size, Image.Resampling.LANCZOS)


# --------------------------------------------------------------------------------------
# Whole document
# --------------------------------------------------------------------------------------


def _image_objects(pdf: pikepdf.Pdf) -> list[pikepdf.Stream]:
    """All unique image XObjects, excluding soft masks (handled with their parent image)."""
    images: list[pikepdf.Stream] = []
    smasks: set[ObjGen] = set()
    for obj in pdf.objects:
        if isinstance(obj, pikepdf.Stream) and obj.get(Name.Subtype) == Name.Image:
            images.append(obj)
            smask = obj.get(Name.SMask)
            if isinstance(smask, pikepdf.Stream):
                smasks.add(smask.objgen)
    return [img for img in images if img.objgen not in smasks]


def compress_pdf(
    src: str | os.PathLike,
    dst: str | os.PathLike,
    preset: Preset,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> Result:
    """Write a compressed copy of ``src`` to ``dst``. ``src`` is never modified.

    If the result would not be meaningfully smaller, nothing is written and the
    result status is ALREADY_OPTIMIZED. If ``cancel`` gets set, raises Cancelled
    and leaves no partial output behind.
    """

    def check_cancel() -> None:
        if cancel is not None and cancel.is_set():
            raise Cancelled

    def report(stage: str, done: int, total: int) -> None:
        check_cancel()
        if progress:
            progress(stage, done, total)

    src = Path(src)
    dst = Path(dst)
    original_size = src.stat().st_size

    try:
        pdf = pikepdf.open(src)
    except pikepdf.PasswordError as exc:
        raise PasswordProtectedError(
            "This PDF is password protected. Open it, remove the password, and try again."
        ) from exc
    except pikepdf.PdfError as exc:
        raise CompressionError(f"This file could not be read as a PDF.\n\n{exc}") from exc

    with pdf:
        sizes = _collect_draw_sizes(pdf)
        images = _image_objects(pdf)
        result = Result(Status.COMPRESSED, original_size, original_size, None, images_total=len(images))

        for done, image_obj in enumerate(images, start=1):
            objgen = image_obj.objgen
            draw_size = None if objgen in sizes.unknown else sizes.sizes.get(objgen)
            try:
                _recompress_image(image_obj, draw_size, preset)
                result.images_recompressed += 1
            except Exception as exc:  # one odd image must never fail the whole document
                reason = str(exc) if isinstance(exc, _Skip) else "error"
                if not isinstance(exc, _Skip):
                    log.warning("Left image %s unchanged after an error", objgen, exc_info=True)
                result.images_skipped += 1
                result.skip_reasons[reason] = result.skip_reasons.get(reason, 0) + 1
                log.debug("Skipped image %s: %s", objgen, reason)
            report(IMAGES, done, len(images))

        for page in pdf.pages:
            if Name.Thumb in page.obj:
                del page.obj[Name.Thumb]

        dst.parent.mkdir(parents=True, exist_ok=True)
        # Not mkstemp: that creates owner-only files, and the output should get normal permissions.
        tmp = dst.with_name(f"~{dst.stem}.{secrets.token_hex(4)}.pdf.tmp")
        try:
            pdf.save(
                tmp,
                compress_streams=True,
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
                recompress_flate=True,
                linearize=True,
                encryption=True if pdf.is_encrypted else None,
                progress=lambda percent: report(SAVING, percent, 100),
            )
            new_size = tmp.stat().st_size
            if new_size >= original_size * MIN_FILE_SAVING:
                tmp.unlink()
                result.status = Status.ALREADY_OPTIMIZED
                return result
            os.replace(tmp, dst)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    result.new_size = new_size
    result.output_path = dst
    return result
