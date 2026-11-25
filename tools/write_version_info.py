"""Write the Windows version resource (File > Properties > Details) for PyInstaller."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pdf_squeeze import __version__

TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(filevers=({v}, 0), prodvers=({v}, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('FileDescription', 'PDF Squeeze - shrink PDFs with images'),
      StringStruct('FileVersion', '{s}'),
      StringStruct('InternalName', 'PDFSqueeze'),
      StringStruct('OriginalFilename', 'PDFSqueeze.exe'),
      StringStruct('ProductName', 'PDF Squeeze'),
      StringStruct('ProductVersion', '{s}'),
    ])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])]),
  ],
)
"""

out = Path(sys.argv[1] if len(sys.argv) > 1 else "build/version_info.txt")
out.parent.mkdir(parents=True, exist_ok=True)
parts = ", ".join(__version__.split("."))
out.write_text(TEMPLATE.format(v=parts, s=__version__), encoding="utf-8")
