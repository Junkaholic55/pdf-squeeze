"""Per-user settings and logging, stored under %LOCALAPPDATA%\\PDFSqueeze (no registry, no install)."""

from __future__ import annotations

import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .presets import DEFAULT_PRESET_KEY, PRESETS

log = logging.getLogger(__name__)


def app_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "PDFSqueeze"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "pdf-squeeze"


def log_path() -> Path:
    return app_dir() / "log.txt"


def configure_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    try:
        app_dir().mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            log_path(), maxBytes=1_000_000, backupCount=1, encoding="utf-8"
        )
    except OSError:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)


def _settings_file() -> Path:
    return app_dir() / "settings.json"


def load_preset_key() -> str:
    try:
        key = json.loads(_settings_file().read_text(encoding="utf-8")).get("preset")
    except (OSError, ValueError, AttributeError):
        return DEFAULT_PRESET_KEY
    return key if any(p.key == key for p in PRESETS) else DEFAULT_PRESET_KEY


def save_preset_key(key: str) -> None:
    try:
        app_dir().mkdir(parents=True, exist_ok=True)
        _settings_file().write_text(json.dumps({"preset": key}), encoding="utf-8")
    except OSError:
        log.warning("Could not save settings", exc_info=True)
