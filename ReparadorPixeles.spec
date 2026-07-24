# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['reparador_pixeles_v2.py'],
    pathex=[],
    binaries=[('ffmpeg.exe', '.'), ('ffprobe.exe', '.'), ('avcodec-63.dll', '.'), ('avdevice-63.dll', '.'), ('avfilter-12.dll', '.'), ('avformat-63.dll', '.'), ('avutil-61.dll', '.'), ('swresample-7.dll', '.'), ('swscale-10.dll', '.')],
    datas=[],
    hiddenimports=[],
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
    name='ReparadorPixeles',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
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
    name='ReparadorPixeles',
)
