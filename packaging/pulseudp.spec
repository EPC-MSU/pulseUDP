# PyInstaller spec for the pulseUDP standalone client (one-folder build).
#
# Build from the repo root:
#     py -3.8 -m PyInstaller packaging/pulseudp.spec --noconfirm
# Output: dist/pulseUDP/  (ship the whole folder, e.g. zipped).
#
# One-folder (COLLECT) is deliberate: faster startup and far fewer antivirus
# false positives than --onefile. The folder bundles Qt platform plugins, the
# descriptor schema (package data), and the third-party license texts that GPL
# compliance requires (see packaging/licenses/THIRD_PARTY_NOTICES.md).

import os
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

SPECPATH = os.path.dirname(os.path.abspath(SPEC))   # packaging/
ROOT = os.path.dirname(SPECPATH)                     # repo root

datas = []
# Descriptor schema shipped as package data (loaded via pkgutil at runtime).
datas += collect_data_files("pulseudp", includes=["data/*.json"])
# License texts must accompany the GPL binary.
datas += [(os.path.join(SPECPATH, "licenses"), "licenses")]
datas += [(os.path.join(ROOT, "LICENSE"), ".")]

# pyqtgraph pulls some modules dynamically; make sure they are collected.
hiddenimports = collect_submodules("pyqtgraph")

block_cipher = None

a = Analysis(
    # Use a launcher with an absolute import, not the package's __main__.py:
    # PyInstaller runs the entry script as top-level __main__ (no parent
    # package), so __main__.py's relative `from .app import run` would fail.
    [os.path.join(SPECPATH, "entrypoint.py")],
    pathex=[os.path.join(ROOT, "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Trim heavy, unused optional deps to keep the bundle smaller.
    excludes=["tkinter", "matplotlib", "scipy", "PySide2", "PySide6", "PyQt6"],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="pulseUDP",
    console=False,          # GUI app: no console window
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="pulseUDP",
)
