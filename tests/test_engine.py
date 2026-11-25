from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest
from pikepdf import Name
from PIL import ImageChops, ImageStat

from pdf_squeeze.engine import (
    SAVING,
    Cancelled,
    CompressionError,
    PasswordProtectedError,
    Status,
    compress_pdf,
)
from pdf_squeeze.files import human_size, next_output_path
from pdf_squeeze.presets import PRESETS, get_preset

from . import fixtures
from .fixtures import FIXTURES, TEXT

# Mean per-channel pixel difference allowed between original and output renders at 72 DPI.
MAX_VISUAL_DIFF = {"high": 1.5, "balanced": 2.5, "smallest": 4.0}
PRESET_KEYS = [p.key for p in PRESETS]


@pytest.fixture(scope="session")
def sources(tmp_path_factory) -> dict[str, Path]:
    directory = tmp_path_factory.mktemp("sources")
    return {name: build(directory / f"{name}.pdf") for name, build in FIXTURES.items()}


@pytest.fixture(scope="session")
def outputs(sources, tmp_path_factory) -> dict[tuple[str, str], tuple[Path, object]]:
    directory = tmp_path_factory.mktemp("outputs")
    results = {}
    for name, src in sources.items():
        for key in PRESET_KEYS:
            dst = directory / f"{name}_{key}.pdf"
            results[name, key] = (dst, compress_pdf(src, dst, get_preset(key)))
    return results


def render(path: Path, page: int):
    return pdfium.PdfDocument(path)[page].render(scale=1).to_pil().convert("RGB")


def page_text(path: Path, page: int) -> str:
    return pdfium.PdfDocument(path)[page].get_textpage().get_text_range()


def first_image(path: Path, page: int = 0) -> tuple[pikepdf.Pdf, pikepdf.Stream]:
    pdf = pikepdf.open(path)
    resources = pdf.pages[page].resources
    while True:
        xobj = next(iter(resources.XObject.values()))
        if xobj.Subtype == Name.Image:
            return pdf, xobj
        resources = xobj.Resources


# ------------------------------------------------------------------ every fixture, every preset


@pytest.mark.parametrize("key", PRESET_KEYS)
@pytest.mark.parametrize("name", list(FIXTURES))
def test_output_is_smaller_valid_and_looks_the_same(name, key, sources, outputs):
    src = sources[name]
    dst, result = outputs[name, key]

    assert result.status is Status.COMPRESSED
    assert result.output_path == dst
    assert result.new_size == dst.stat().st_size < src.stat().st_size

    with pikepdf.open(src) as before, pikepdf.open(dst) as after:
        assert len(after.pages) == len(before.pages)
        for i in range(len(before.pages)):
            assert page_text(dst, i) == page_text(src, i)
            diff = ImageStat.Stat(ImageChops.difference(render(src, i), render(dst, i))).mean
            assert max(diff) < MAX_VISUAL_DIFF[key], f"page {i} diff {diff}"


@pytest.mark.parametrize("name", ["photo_doc", "owner_restricted", "form"])
def test_text_still_searchable(name, outputs):
    assert TEXT in page_text(outputs[name, "smallest"][0], 0)


def test_stronger_presets_give_smaller_files(outputs):
    for name in FIXTURES:
        sizes = [outputs[name, key][1].new_size for key in PRESET_KEYS]
        assert sizes[0] >= sizes[1] >= sizes[2], (name, sizes)


def test_source_is_never_modified(tmp_path):
    src = fixtures.make_photo_doc(tmp_path / "in.pdf")
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    compress_pdf(src, tmp_path / "out.pdf", get_preset("smallest"))
    assert hashlib.sha256(src.read_bytes()).hexdigest() == digest


# ------------------------------------------------------------------ resolution


@pytest.mark.parametrize(
    ("name", "drawn_inches"),
    [
        ("photo_doc", 6),  # 2400 px drawn 6 in wide
        ("reused", 6),  # same image drawn at 2, 4 and 6 in: the largest use wins
        ("form", 3),  # drawn through a Form XObject whose /Matrix scales it to 3 in
    ],
)
@pytest.mark.parametrize("key", PRESET_KEYS)
def test_downsampled_to_target_dpi(name, drawn_inches, key, outputs):
    dst, _ = outputs[name, key]
    pdf, image = first_image(dst)
    with pdf:
        expected = drawn_inches * get_preset(key).target_dpi
        assert abs(int(image.Width) - expected) <= 1


def test_full_page_scan_downsampled(outputs):
    dst, _ = outputs["rgb_scan", "balanced"]
    pdf, image = first_image(dst)
    with pdf:
        assert (int(image.Width), int(image.Height)) == (1275, 1650)  # 8.5 x 11 in at 150 DPI


def test_image_already_below_target_keeps_resolution(tmp_path):
    builder = fixtures.Builder()
    # 600 px across 8 in = 75 DPI: below every preset, so no resampling, only re-encoding.
    builder.page([(builder.jpeg_image(fixtures.photo(600, 450), 100), (0, 100, 576, 432))])
    src = builder.save(tmp_path / "low.pdf")
    dst = tmp_path / "out.pdf"
    result = compress_pdf(src, dst, get_preset("high"))
    if result.status is Status.COMPRESSED:
        pdf, image = first_image(dst)
        with pdf:
            assert int(image.Width) == 600


# ------------------------------------------------------------------ color and encoding


def test_gray_scan_becomes_gray_jpeg(outputs):
    pdf, image = first_image(outputs["gray_scan", "balanced"][0])
    with pdf:
        assert image.ColorSpace == Name.DeviceGray
        assert image.Filter == Name.DCTDecode


def test_cmyk_stays_cmyk(outputs):
    pdf, image = first_image(outputs["cmyk", "balanced"][0])
    with pdf:
        assert image.ColorSpace == Name.DeviceCMYK
        assert list(image.Decode) == [1, 0] * 4


def test_flat_graphics_stay_lossless(outputs):
    pdf, image = first_image(outputs["screenshot", "balanced"][0])
    with pdf:
        assert image.Filter == Name.FlateDecode
        assert image.ColorSpace[0] == Name.Indexed


def test_soft_mask_resized_with_image(outputs):
    dst, _ = outputs["smask", "balanced"]
    with pikepdf.open(dst) as pdf:
        image = pdf.pages[0].resources.XObject.Im
        assert (image.SMask.Width, image.SMask.Height) == (image.Width, image.Height)
        assert int(image.Width) == 833  # 400 pt = 5.556 in at 150 DPI


def test_bilevel_scan_left_byte_identical(sources, outputs):
    before_pdf, before = first_image(sources["bilevel_plus_photo"])
    after_pdf, after = first_image(outputs["bilevel_plus_photo", "smallest"][0])
    with before_pdf, after_pdf:
        assert int(after.BitsPerComponent) == 1
        assert after.read_raw_bytes() == before.read_raw_bytes()


# ------------------------------------------------------------------ whole-document behavior


def test_owner_restrictions_preserved(outputs):
    dst, _ = outputs["owner_restricted", "balanced"]
    with pikepdf.open(dst) as pdf:  # opens without a password
        assert pdf.is_encrypted
        assert not pdf.allow.extract


def test_user_password_reports_clear_error(tmp_path):
    src = fixtures.make_user_password(tmp_path / "locked.pdf")
    with pytest.raises(PasswordProtectedError):
        compress_pdf(src, tmp_path / "out.pdf", get_preset("balanced"))
    assert not (tmp_path / "out.pdf").exists()


def test_not_a_pdf_reports_clear_error(tmp_path):
    src = tmp_path / "fake.pdf"
    src.write_bytes(b"this is not a pdf" * 100)
    with pytest.raises(CompressionError):
        compress_pdf(src, tmp_path / "out.pdf", get_preset("balanced"))


def test_text_only_pdf_is_already_optimized(tmp_path):
    src = fixtures.make_text_only(tmp_path / "text.pdf")
    dst = tmp_path / "out.pdf"
    result = compress_pdf(src, dst, get_preset("smallest"))
    assert result.status is Status.ALREADY_OPTIMIZED
    assert result.output_path is None
    assert not dst.exists()
    assert list(tmp_path.iterdir()) == [src]  # no temp files left behind


def test_progress_reports_every_image_then_saving(tmp_path):
    src = fixtures.make_rgb_scan(tmp_path / "scan.pdf")
    events = []
    compress_pdf(src, tmp_path / "out.pdf", get_preset("balanced"), progress=lambda *e: events.append(e))
    assert events[:2] == [("images", 1, 2), ("images", 2, 2)]
    assert all(stage == SAVING for stage, _, _ in events[2:])


def test_cancel_leaves_nothing_behind(tmp_path):
    src = fixtures.make_rgb_scan(tmp_path / "scan.pdf")
    cancel = threading.Event()

    def progress(stage, done, total):
        if stage == SAVING and done >= 10:
            cancel.set()

    with pytest.raises(Cancelled):
        compress_pdf(src, tmp_path / "out.pdf", get_preset("balanced"), progress=progress, cancel=cancel)
    assert list(tmp_path.iterdir()) == [src]


# ------------------------------------------------------------------ helpers


def test_next_output_path(tmp_path):
    src = tmp_path / "report.pdf"
    assert next_output_path(src).name == "report_compressed.pdf"
    (tmp_path / "report_compressed.pdf").touch()
    assert next_output_path(src).name == "report_compressed (2).pdf"
    (tmp_path / "report_compressed (2).pdf").touch()
    assert next_output_path(src).name == "report_compressed (3).pdf"


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (512, "512 bytes"),
        (2048, "2.0 KB"),
        (870_000, "850 KB"),
        (12_400_000, "11.8 MB"),
        (5 * 1024**3, "5.0 GB"),
    ],
)
def test_human_size(size, text):
    assert human_size(size) == text
