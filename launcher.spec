# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the ARGUS one-click launcher.

Build:  .venv/Scripts/pyinstaller.exe launcher.spec --noconfirm
Result: dist/ARGUS.exe  (console app; closing its window stops ARGUS)
"""
from pathlib import Path
import sys

import playwright
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH)
VERSION_FILE = ROOT / "packaging" / "version_info.txt"
if not VERSION_FILE.is_file():
    raise FileNotFoundError(f"Missing Windows version resource: {VERSION_FILE}")

# Keep the frozen build aligned with the source tree.  ``collect_submodules``
# covers routers, services, automation adapters, and migration imports that
# are intentionally loaded dynamically by FastAPI/SQLAlchemy.  Playwright's
# Python driver package is included, but its browser cache/profile is never a
# source input; operators install/point at browsers separately at runtime.
APP_HIDDEN_IMPORTS = collect_submodules("app")
PLAYWRIGHT_HIDDEN_IMPORTS = collect_submodules("playwright")
PLAYWRIGHT_DATAS = collect_data_files("playwright")
PLAYWRIGHT_DRIVER = Path(playwright.__file__).resolve().parent / "driver"
PLAYWRIGHT_NODE = PLAYWRIGHT_DRIVER / ("node.exe" if sys.platform == "win32" else "node")
if not PLAYWRIGHT_NODE.is_file():
    raise FileNotFoundError(f"Missing Playwright driver executable: {PLAYWRIGHT_NODE}")
PLAYWRIGHT_BINARIES = [(str(PLAYWRIGHT_NODE), "playwright/driver")]
MIGRATIONS = []
MIGRATIONS_ROOT = ROOT / "app" / "migrations"
if MIGRATIONS_ROOT.is_dir():
    MIGRATIONS.append((str(MIGRATIONS_ROOT), "app/migrations"))

# Packaged and shortcut defaults remain OFF.  This spec does not inject a
# data directory, profile, cookie store, provider allowlist, or live-submit
# environment; Settings.load() supplies the fail-closed OFF/zero defaults.

a = Analysis(
    [str(ROOT / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=PLAYWRIGHT_BINARIES,
    datas=[
        (str(ROOT / "app" / "templates"), "app/templates"),
        (str(ROOT / "app" / "static"), "app/static"),
        *MIGRATIONS,
        *PLAYWRIGHT_DATAS,
    ],
    hiddenimports=sorted(set([
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "app",
        "app.main",
        "app.cli",
        "app.routers.api",
        "app.routers.mail",
        "app.routers.lab",
        "app.routers.pages",
        "app.routers.scout",
        "app.routers.sweep",
        "app.routers.scout_pages",
        "app.services.handoff",
        "app.services.submission_intents",
        "app.scouting.scheduler",
        "sqlalchemy.dialects.sqlite",
        "playwright",
        "playwright.sync_api",
        *APP_HIDDEN_IMPORTS,
        *PLAYWRIGHT_HIDDEN_IMPORTS,
    ])),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "pip", "setuptools"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ARGUS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    icon=None,
    version=str(VERSION_FILE),
)
