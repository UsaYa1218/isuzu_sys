# -*- mode: python ; coding: utf-8 -*-
import importlib
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, copy_metadata


def collect_python_extension_modules(module_names):
    collected = []
    for module_name in module_names:
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        module_file = getattr(module, "__file__", None)
        if not module_file:
            continue
        module_path = Path(module_file)
        if module_path.suffix.lower() in {".pyd", ".dll"} and module_path.exists():
            collected.append((str(module_path), "."))
    return collected


def collect_conda_library_dlls():
    names = [
        "libssl-3-x64.dll",
        "libcrypto-3-x64.dll",
        "libffi-8.dll",
        "libffi-7.dll",
        "ffi-8.dll",
        "ffi-7.dll",
        "ffi.dll",
        "sqlite3.dll",
        "libbz2.dll",
        "zlib.dll",
    ]
    roots = [Path(sys.prefix), Path(sys.base_prefix)]
    collected = []
    seen = set()
    for root in roots:
        library_bin = root / "Library" / "bin"
        for name in names:
            path = library_bin / name
            key = name.lower()
            if path.exists() and key not in seen:
                collected.append((str(path), "."))
                seen.add(key)
    return collected


def collect_tk_dlls():
    candidates = []
    base_prefix = Path(sys.base_prefix)
    prefix = Path(sys.prefix)
    for root in (prefix, base_prefix):
        candidates.extend([
            root / "Library" / "bin" / "tcl86t.dll",
            root / "Library" / "bin" / "tk86t.dll",
            root / "DLLs" / "tcl86t.dll",
            root / "DLLs" / "tk86t.dll",
        ])
    collected = []
    seen = set()
    for path in candidates:
        if path.exists() and path.name.lower() not in seen:
            collected.append((str(path), "."))
            seen.add(path.name.lower())
    return collected


def collect_tcl_tk_data():
    collected = []
    for root in (Path(sys.prefix), Path(sys.base_prefix)):
        library_lib = root / "Library" / "lib"
        tcl_data = library_lib / "tcl8.6"
        tk_data = library_lib / "tk8.6"
        tcl_modules = library_lib / "tcl8"
        if tcl_data.exists():
            collected.append((str(tcl_data), "_tcl_data"))
        if tk_data.exists():
            collected.append((str(tk_data), "_tk_data"))
        if tcl_modules.exists():
            collected.append((str(tcl_modules), "tcl8"))
    return collected


def collect_cython_utility_data():
    collected = []
    for root in (Path(sys.prefix), Path(sys.base_prefix)):
        cython_utility = root / "Lib" / "site-packages" / "Cython" / "Utility"
        if cython_utility.exists():
            collected.append((str(cython_utility), "Cython/Utility"))
            break
    return collected


def collect_package_metadata(package_names):
    collected = []
    seen = set()
    for package_name in package_names:
        try:
            entries = copy_metadata(package_name)
        except Exception:
            continue
        for src, dest in entries:
            key = (src, dest)
            if key not in seen:
                collected.append((src, dest))
                seen.add(key)
    return collected


datas = [("app\\templates", "app\\templates"), ("app\\static", "app\\static")]
datas += collect_tcl_tk_data()
datas += collect_cython_utility_data()
datas += collect_package_metadata([
    "einops",
    "ftfy",
    "imagesize",
    "Jinja2",
    "lxml",
    "opencv-contrib-python",
    "opencv-python",
    "opencv-python-headless",
    "openpyxl",
    "premailer",
    "pyclipper",
    "pypdfium2",
    "regex",
    "scikit-learn",
    "shapely",
    "tiktoken",
    "tokenizers",
])
binaries = []
hiddenimports = [
    "paddleocr",
    "paddlex",
    "cv2",
    "_socket",
    "_ssl",
    "_hashlib",
    "_bz2",
    "_lzma",
    "_ctypes",
    "_decimal",
    "_queue",
    "select",
]
binaries += collect_python_extension_modules(hiddenimports)
binaries += collect_conda_library_dlls()
binaries += collect_tk_dlls()
for package_name in ["paddleocr", "paddlex", "paddle", "imagesize", "pyclipper", "pypdfium2", "shapely"]:
    tmp_ret = collect_all(package_name)
    datas += tmp_ret[0]
    binaries += tmp_ret[1]
    hiddenimports += tmp_ret[2]


a = Analysis(
    ["desktop_launcher.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TransferSummaryBrowserTool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="TransferSummaryBrowserTool",
)
