"""Entry point.

PDFSqueeze.exe                          open a file picker, then the window
PDFSqueeze.exe report.pdf               (drag-and-drop) open the window for report.pdf
PDFSqueeze.exe --selftest IN OUT [KEY]  compress without a GUI; exit code 0 on success
PDFSqueeze.exe --screenshots DIR IN     render the window states to PNGs (CI pixel review)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from . import __version__
from .engine import CompressionError, Status, compress_pdf
from .presets import DEFAULT_PRESET_KEY, get_preset
from .settings import configure_logging

log = logging.getLogger(__name__)


def selftest(src: str, dst: str, preset_key: str = DEFAULT_PRESET_KEY) -> int:
    try:
        result = compress_pdf(src, dst, get_preset(preset_key))
    except (CompressionError, KeyError, OSError):
        log.exception("Self-test failed")
        return 2
    log.info(
        "Self-test: %s %d -> %d bytes, %d/%d images recompressed",
        result.status.value,
        result.original_size,
        result.new_size,
        result.images_recompressed,
        result.images_total,
    )
    return 0 if result.status is Status.COMPRESSED else 3


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    configure_logging()
    log.info("PDF Squeeze %s started with %d argument(s)", __version__, len(args))

    if args[:1] == ["--selftest"] and len(args) in (3, 4):
        return selftest(*args[1:])

    from . import gui  # tkinter is only needed for the window

    if args[:1] == ["--screenshots"] and len(args) == 3:
        return gui.run(Path(args[2]), screenshot_dir=Path(args[1]))

    files = [Path(a) for a in args if not a.startswith("--")]
    return gui.run(files[0] if files else None, extra_files=len(files) - 1 if files else 0)


if __name__ == "__main__":
    sys.exit(main())
