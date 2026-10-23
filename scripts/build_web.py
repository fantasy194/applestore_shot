"""Build a self-contained local Web folder; only replace the previous successful build."""
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'local-web-release'


def replace_release(staging, destination):
    # Both paths stay on the same filesystem. Never delete user data or desktop packages.
    previous = destination.with_name('.local-web-previous-' + uuid.uuid4().hex)
    base = destination.parent.resolve()
    if any(path.resolve().parent != base for path in (staging, destination, previous)):
        raise ValueError('构建和替换目录必须位于同一个目录。')
    if staging.is_symlink() or destination.is_symlink():
        raise ValueError('构建目录不能是符号链接。')
    if destination.exists():
        destination.rename(previous)
    copied = False
    try:
        try:
            staging.rename(destination)
        except PermissionError:
            if os.name != 'nt':
                raise
            # The old release is already saved. Own a fresh destination so a
            # failed Windows copy can be removed without touching other files.
            destination.mkdir()
            copied = True
            shutil.copytree(staging, destination, dirs_exist_ok=True)
    except BaseException:
        if copied:
            shutil.rmtree(destination)
        if previous.exists():
            previous.rename(destination)
        raise
    if previous.exists():
        shutil.rmtree(previous)


def main():
    if sys.platform not in ('darwin', 'win32'):
        raise SystemExit('当前打包支持 macOS 和 Windows。')
    pnpm = shutil.which('pnpm')
    if not pnpm:
        raise SystemExit('请先安装 pnpm。')
    python = ROOT / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not python.exists():
        subprocess.run([sys.executable, str(ROOT / 'scripts/setup_worker.py')], check=True)
    subprocess.run([pnpm, 'exec', 'tsc', '--noEmit'], cwd=ROOT, check=True)
    subprocess.run(['cargo', 'build', '--release', '--locked', '--manifest-path', str(ROOT / 'local-web/Cargo.toml')], cwd=ROOT, check=True)
    staging = Path(tempfile.mkdtemp(prefix='.local-web-build-', dir=ROOT))
    try:
        subprocess.run([pnpm, 'exec', 'vite', 'build', '--mode', 'local-web', '--outDir', str(staging / 'www'), '--emptyOutDir'], cwd=ROOT, check=True)
        backend = 'keyring.backends.Windows' if os.name == 'nt' else 'keyring.backends.macOS'
        subprocess.run([str(python), '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir',
                        '--name', 'duo-killer-worker', '--distpath', str(staging / 'python-dist'),
                        '--workpath', str(ROOT / 'local-web/target/python-build'),
                        '--specpath', str(ROOT / 'local-web/target/python-build'),
                        '--paths', str(ROOT / 'worker/legacy'), '--collect-all', 'playwright',
                        '--collect-data', 'tzdata', '--hidden-import', backend,
                        '--hidden-import', 'srp._pysrp', '--hidden-import', 'qrcode.image.pure',
                        str(ROOT / 'worker/main.py')], cwd=ROOT, check=True)
        frozen = staging / 'python-dist/duo-killer-worker'
        try:
            frozen.rename(staging / 'worker')
        except PermissionError:
            if os.name != 'nt':
                raise
            # Windows can hold a just-created executable directory open.
            # Copy into staging; the bundled smoke still gates publication.
            shutil.copytree(frozen, staging / 'worker')
            shutil.rmtree(frozen)
        (staging / 'python-dist').rmdir()
        executable = 'duo-killer-web.exe' if os.name == 'nt' else 'duo-killer-web'
        shutil.copy2(ROOT / 'local-web/target/release' / executable, staging / executable)
        if os.name == 'nt':
            (staging / 'start.cmd').write_bytes(b'@echo off\r\ncd /d "%~dp0"\r\nduo-killer-web.exe\r\nif errorlevel 1 pause\r\n')
        else:
            launcher = staging / '启动.command'
            launcher.write_text('#!/bin/sh\ncd -- "$(dirname -- "$0")" || exit 1\n./duo-killer-web\n', encoding='utf-8')
            launcher.chmod(0o755)
        (staging / '使用说明.txt').write_text(
            'DUO Killer · 本地 Web\n\n双击启动.command（macOS）或 start.cmd（Windows），自动打开默认浏览器。\n'
            '请保留整个目录；内置 Python，无需另外安装运行环境。登录时仍需安装 Chrome 或 Edge。\n'
            '关闭网页不会退出服务；在启动终端按 Ctrl+C 退出。\n'
            '账号数据保存在系统用户目录 com.niulai.local-web 中，与桌面版分开，重新构建不清除。\n'
            'macOS: ~/Library/Application Support/com.niulai.local-web\nWindows: %APPDATA%\\com.niulai.local-web\n', encoding='utf-8')
        # Exercise the actual bundled server and Python worker before replacing a working package.
        subprocess.run([sys.executable, str(ROOT / 'tests/local_web/smoke.py'), str(staging)], cwd=ROOT, check=True)
        replace_release(staging, OUTPUT)
        print(f'本地 Web 构建完成，旧版已替换：{OUTPUT}')
    finally:
        if staging.exists():
            shutil.rmtree(staging)


if __name__ == '__main__':
    main()
