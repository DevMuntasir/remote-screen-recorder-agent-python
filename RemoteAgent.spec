# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

project_dir = Path.cwd()
env_file = project_dir / ".env"
build_version_file = project_dir / "AGENT_VERSION_BUILD.txt"
datas = []
if env_file.exists():
    datas.append((str(env_file), "."))
if build_version_file.exists():
    datas.append((str(build_version_file), "."))


a = Analysis(
    ['socket_video_service.pyw'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=['cloudinary', 'engineio.async_drivers.threading', 'cv2'],
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
    a.binaries,
    a.datas,
    [],
    name='RemoteAgent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
