# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller definition for the Intel-only macOS app bundle."""

import os
from pathlib import Path
from runpy import run_path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


PROJECT_ROOT = Path(SPECPATH).resolve().parent
SOURCE_VERSION = run_path(
    str(PROJECT_ROOT / "src" / "cookies_news_cockpit" / "_version.py")
)["__version__"]
APP_VERSION = os.environ.get("APP_VERSION", SOURCE_VERSION)
if APP_VERSION != SOURCE_VERSION:
    raise SystemExit(
        f"Build version {APP_VERSION} does not match source version {SOURCE_VERSION}"
    )
APP_NAME = "Cookies News Cockpit"
ICON_PATH = PROJECT_ROOT / "build" / "CookiesNewsCockpit.icns"

if not ICON_PATH.is_file():
    raise SystemExit(f"Missing generated app icon: {ICON_PATH}")

datas = collect_data_files("cookies_news_cockpit")
hiddenimports = collect_submodules("uvicorn") + collect_submodules("keyring.backends")

analysis = Analysis(
    [str(PROJECT_ROOT / "src" / "cookies_news_cockpit" / "__main__.py")],
    pathex=[str(PROJECT_ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "ruff", "tkinter"],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    target_arch="x86_64",
)

collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)

app = BUNDLE(
    collection,
    name=f"{APP_NAME}.app",
    icon=str(ICON_PATH),
    bundle_identifier="com.cookies.newscockpit",
    version=APP_VERSION,
    info_plist={
        "CFBundleDisplayName": APP_NAME,
        "CFBundleName": APP_NAME,
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleVersion": APP_VERSION,
        "LSMinimumSystemVersion": "14.0",
        "LSMultipleInstancesProhibited": True,
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
    },
)
