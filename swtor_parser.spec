# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for a standalone Windows build. onedir (not onefile) --
# more reliable for the pywebview+Tkinter combo, faster startup, and keeps
# individual files replaceable if a self-updater gets built later.
#
# Build with:  pyinstaller swtor_parser.spec --noconfirm
# (or just run build.ps1, which does this and zips the result)

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    # The three non-.py runtime data locations the app actually reads from
    # (confirmed via grep of Path(__file__).parent-style references):
    # web_ui/ (the pywebview UI), analysis/static/ (shared CSS/JS with the
    # corpus browser), boss_definitions_bundled/ (68 boss encounter JSONs).
    datas=[
        ('web_ui', 'web_ui'),
        ('analysis/static', 'analysis/static'),
        ('boss_definitions_bundled', 'boss_definitions_bundled'),
    ],
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
    name='DPS-Dynamic-Parse-System',
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
    icon='assets/icon.ico',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='DPS-Dynamic-Parse-System',
)
