# PyInstaller build: one-folder, windowed. Run: pyinstaller PDFSqueeze.spec
# One-folder (not one-file) so nothing is unpacked into %TEMP% at launch - locked-down
# PCs (AppLocker, antivirus) often block that, and startup is faster.

a = Analysis(
    ["pdf_squeeze/__main__.py"],
    datas=[("assets/icon.ico", "assets")],
    excludes=["numpy", "matplotlib", "pytest", "pypdfium2", "IPython"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="PDFSqueeze",
    console=False,
    icon="assets/icon.ico",
    version="build/version_info.txt",
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="PDFSqueeze", upx=False)
