# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Skill Router desktop backend.

One-folder build (not --onefile): the API reads its migrations from disk at
startup and SQLite opens the database by path, so a stable directory is simpler
and starts faster than unpacking an archive to a temp dir on every launch.

Bundled resources:
  * `backend/migrations/*.sql`  → `<app>/migrations`
  * `sidecar/` (dist + package.json, no node_modules) → `<app>/sidecar`
    so the engine-validate step can still build a candidate when the app is run
    from an installed location instead of a checkout.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent
BACKEND = ROOT / "backend"
SIDECAR = ROOT / "sidecar"

hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "passlib",
    "argon2",
    "jsonschema_specifications",
    "app",
]
hiddenimports += collect_submodules("app")

datas = [
    (str(BACKEND / "migrations"), "migrations"),
]
if SIDECAR.exists():
    datas.append((str(SIDECAR / "dist"), "sidecar/dist"))
    datas.append((str(SIDECAR / "package.json"), "sidecar"))

# The connection CLI ships with the app: the Overview page prints a command that runs
# it, so the file must exist on an installed machine (not only on the build box).
INSTALLER = ROOT / "packages" / "installer"
if (INSTALLER / "bin" / "skill-router-connect.js").exists():
    datas.append((str(INSTALLER / "bin"), "installer/bin"))
    datas.append((str(INSTALLER / "src"), "installer/src"))
    datas.append((str(INSTALLER / "package.json"), "installer"))

a = Analysis(
    [str(BACKEND / "desktop_entry.py")],
    pathex=[str(BACKEND)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pandas", "numpy", "pytest", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="skillrouter-backend",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="skillrouter-backend",
)
