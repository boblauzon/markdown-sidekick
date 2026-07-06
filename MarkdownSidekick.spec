# PyInstaller spec for Markdown Sidekick (one-folder, windowed).
# Build:  .venv\Scripts\pyinstaller.exe MarkdownSidekick.spec --noconfirm
#
# collect_all is used for every package that ships non-Python assets the import
# tracer can't see: ONNX models (rapidocr, magika), Tcl files (tkinterdnd2),
# native DLLs (pypdfium2, onnxruntime, ctranslate2, av), and faster-whisper's
# bundled VAD model.

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
for pkg in (
    "rapidocr_onnxruntime",
    "magika",
    "tkinterdnd2",
    "pypdfium2",
    "pypdfium2_raw",
    "onnxruntime",
    "ctranslate2",
    "faster_whisper",
    "av",
    "markitdown",
    "fastmcp",  # the exe hosts the MCP server via `MarkdownSidekick.exe --mcp`
):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# Bundled user guide (package data, loaded via importlib.resources).
datas += [("src/markdown_sidekick/USERGUIDE.md", "markdown_sidekick")]

a = Analysis(
    ["app.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=[
        # Not used by the app.
        "IPython", "matplotlib", "pytest", "setuptools._distutils",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="MarkdownSidekick",
    console=False,          # windowed GUI app
    upx=False,              # UPX breaks some native DLLs; size is fine without it
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="MarkdownSidekick",
)
