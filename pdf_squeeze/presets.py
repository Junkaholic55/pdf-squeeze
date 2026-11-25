"""Compression presets shown in the GUI."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    description: str
    target_dpi: int
    jpeg_quality: int


PRESETS: tuple[Preset, ...] = (
    Preset(
        key="high",
        label="High quality",
        description="200 DPI - for printing and fine detail",
        target_dpi=200,
        jpeg_quality=85,
    ),
    Preset(
        key="balanced",
        label="Balanced",
        description="150 DPI - good for email and archiving",
        target_dpi=150,
        jpeg_quality=72,
    ),
    Preset(
        key="smallest",
        label="Smallest",
        description="96 DPI - for upload limits, on-screen reading",
        target_dpi=96,
        jpeg_quality=55,
    ),
)

DEFAULT_PRESET_KEY = "balanced"


def get_preset(key: str) -> Preset:
    for preset in PRESETS:
        if preset.key == key:
            return preset
    raise KeyError(f"Unknown preset: {key!r}")
