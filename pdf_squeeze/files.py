"""Output naming and size formatting."""

from __future__ import annotations

import tempfile
from pathlib import Path

SUFFIX = "_compressed"


def next_output_path(src: Path) -> Path:
    """``report.pdf`` -> ``report_compressed.pdf``, or ``report_compressed (2).pdf`` if taken."""
    candidate = src.with_name(f"{src.stem}{SUFFIX}.pdf")
    n = 2
    while candidate.exists():
        candidate = src.with_name(f"{src.stem}{SUFFIX} ({n}).pdf")
        n += 1
    return candidate


def is_writable_dir(directory: Path) -> bool:
    """Probe by creating a file: os.access is unreliable with Windows ACLs and OneDrive."""
    try:
        with tempfile.TemporaryFile(dir=directory):
            return True
    except OSError:
        return False


def human_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} bytes"
    value = float(num_bytes)
    for unit in ("KB", "MB", "GB"):
        value /= 1024
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if value >= 100 else f"{value:.1f} {unit}"
    raise AssertionError("unreachable")
