# -*- mode: python ; coding: utf-8 -*-
import os

version = os.environ.get('SEEKBAR_VERSION', '0.1.5')
architecture = os.environ.get('SEEKBAR_ARCH', 'arm64')
analysis = Analysis(
    ['seekbar/app.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=[],
    excludes=['tkinter', 'unittest'],
    noarchive=False,
)
archive = PYZ(analysis.pure)
executable = EXE(
    archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name='SeekBar',
    console=False,
    argv_emulation=False,
    target_arch=architecture,
)
files = COLLECT(executable, analysis.binaries, analysis.datas, name='SeekBar')
app = BUNDLE(
    files,
    name='SeekBar.app',
    icon='assets/SeekBar.icns',
    bundle_identifier='local.seekbar.launcher',
    version=version,
    info_plist={
        'CFBundleDisplayName': '寻径',
        'CFBundleName': '寻径',
        'CFBundleShortVersionString': version,
        'CFBundleDevelopmentRegion': 'zh_CN',
        'LSUIElement': True,
        'LSMinimumSystemVersion': os.environ.get('SEEKBAR_MIN_MACOS', '11.0'),
        'NSDesktopFolderUsageDescription': '读取桌面上的项目名称以显示搜索结果和文件夹快捷入口。',
        'NSDocumentsFolderUsageDescription': '读取文稿项目名称以显示搜索结果和文件夹快捷入口。',
        'NSDownloadsFolderUsageDescription': '读取下载项目名称以显示搜索结果和文件夹快捷入口。',
    },
)
