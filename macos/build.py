"""Build a self-contained macOS menu-bar app using a python.org framework."""
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
from icons import create as create_icons

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / 'dist' / '叠声.app'
CONTENTS = APP / 'Contents'
RES = CONTENTS / 'Resources'
PY = RES / 'python'
FRAMEWORK = Path(sys.base_prefix)


def run(*args):
    subprocess.run(list(map(str, args)), check=True)


def build():
    if sys.platform != 'darwin' or not (FRAMEWORK / 'Python').is_file():
        raise SystemExit('需要 macOS 上 python.org 的 Python framework 来打包。')
    if APP.exists(): shutil.rmtree(APP)
    (CONTENTS / 'MacOS').mkdir(parents=True)
    PY.mkdir(parents=True)
    shutil.copy2(FRAMEWORK / 'Python', PY / 'Python')
    (PY / 'bin').mkdir()
    # The framework's bin/python3.11 is a launcher that re-execs Python.app.
    # Bundle the actual interpreter directly so it remains relocatable.
    shutil.copy2(FRAMEWORK / 'Resources/Python.app/Contents/MacOS/Python', PY / 'bin/python3.11')
    (PY / 'lib').mkdir()
    shutil.copytree(FRAMEWORK / 'lib/python3.11', PY / 'lib/python3.11',
                    ignore=shutil.ignore_patterns('site-packages', '__pycache__', 'test', 'tests', 'idlelib', 'tkinter', 'ensurepip'))
    for dylib in (FRAMEWORK / 'lib').glob('*.dylib'):
        if not dylib.is_symlink(): shutil.copy2(dylib, PY / 'lib' / dylib.name)
    shutil.copytree(ROOT / 'agent_group_chat', RES / 'app/agent_group_chat', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    create_icons(RES)
    # Relocate references to the source Python framework and its OpenSSL libraries.
    binaries = [PY / 'bin/python3.11', PY / 'Python', *list((PY / 'lib').glob('*.dylib')),
                *list((PY / 'lib/python3.11/lib-dynload').glob('*.so'))]
    for binary in binaries:
        result = subprocess.run(['otool', '-L', str(binary)], text=True, capture_output=True, check=True)
        dependencies = set(line.strip().split(' (', 1)[0] for line in result.stdout.splitlines()[1:] if ' (' in line)
        changes = []
        for dependency in dependencies:
            if dependency == str(FRAMEWORK / 'Python'):
                target = '@executable_path/../Python'
            elif dependency.startswith(str(FRAMEWORK / 'lib') + '/'):
                target = '@executable_path/../lib/' + Path(dependency).name
            else:
                continue
            if binary == PY / 'Python' and dependency == str(FRAMEWORK / 'Python'): continue
            changes.extend(['-change', dependency, target])
        if changes: run('install_name_tool', *changes, binary)
        run('codesign', '--force', '--sign', '-', binary)
    plist = {'CFBundleName':'叠声', 'CFBundleDisplayName':'叠声', 'CFBundleIdentifier':'com.diesheng.desktop',
             'CFBundleExecutable':'Diesheng', 'CFBundleIconFile':'AppIcon', 'CFBundlePackageType':'APPL', 'CFBundleShortVersionString':'0.1.0',
             'CFBundleVersion':'1', 'LSUIElement':True,
             'ExistingWorkspace':str(ROOT / 'build/local-workspace'),
             'NSLocalNetworkUsageDescription':'叠声需要访问局域网，以便其他设备和 Agent 连接频道。'}
    with (CONTENTS / 'Info.plist').open('wb') as f: plistlib.dump(plist, f)
    run('swiftc', '-target', 'arm64-apple-macos12.0', ROOT / 'macos/App.swift', '-o', CONTENTS / 'MacOS/Diesheng')
    run('codesign', '--force', '--deep', '--sign', '-', APP)
    print(APP)

if __name__ == '__main__': build()
