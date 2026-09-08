"""
Proxy Bridge v2.0 - One-Click AutoSetup (Single directory edition)
Usage: AutoSetup.py [install-dir]
  AutoSetup.py                       -> prompts for install directory
  AutoSetup.py D:/MyProxy            -> installs to D:/MyProxy
  ProxyBridge-Setup.exe              -> prompts (or pass dir as argument)
  ProxyBridge-Setup.exe D:/MyProxy   -> installs to D:/MyProxy

All files go into ONE directory. No scattering.
CA cert exported to install dir for Linux import.
"""
import sys
import os
import json
import hashlib
import base64
import subprocess
import traceback
from pathlib import Path


# -- PyInstaller support --------------------------------------------------------
def _app_dir():
    """Directory containing bundled resources (exe temp dir or script dir)."""
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


SRC_DIR = _app_dir()
NATIVE_NAME = 'com.example.proxy_bridge'


# -- Helpers -------------------------------------------------------------------
def banner(msg):
    print(f'\n{"=" * 50}')
    print(msg)
    print('=' * 50)

def ok(msg):
    print(f'  [OK] {msg}')

def err(msg):
    print(f'  [ERROR] {msg}')

def ensure_admin():
    """Re-launch as Administrator if not already elevated.
    Uses ShellExecuteW with 'runas' verb for UAC prompt."""
    import ctypes
    if ctypes.windll.shell32.IsUserAnAdmin():
        return

    print('[*] Not running as Administrator - requesting elevation...')
    if getattr(sys, 'frozen', False):
        exe = sys.executable
        args = sys.argv[1:]
    else:
        exe = sys.executable
        args = [__file__] + sys.argv[1:]

    ret = ctypes.windll.shell32.ShellExecuteW(
        None, 'runas', exe,
        ' '.join(f'"{a}"' for a in args),
        None, 1  # SW_SHOWNORMAL
    )
    if ret <= 32:
        print(f'  [ERROR] Could not elevate (code {ret}).')
        print('  Please right-click -> Run as Administrator.')
        input('Press Enter to exit...')
    sys.exit(0)

def find_python():
    r = subprocess.run(['where', 'python'], capture_output=True, text=True)
    if r.returncode != 0:
        err('Python 3.8+ required')
        sys.exit(1)
    path = r.stdout.strip().split('\n')[0].strip()
    ok(f'Python: {path}')
    return path

def compute_ext_id():
    manifest = SRC_DIR / 'extension' / 'manifest.json'
    with open(manifest, 'rb') as f:
        m = json.load(f)
    pub_der = base64.b64decode(m['key'])
    h = hashlib.sha256(pub_der).digest()[:16]
    alphabet = 'abcdefghijklmnop'
    ext_id = ''.join(alphabet[b >> 4] + alphabet[b & 0x0f] for b in h)
    return ext_id, m['version']


# -- Steps --------------------------------------------------------------------

def step_choose_dir():
    """Determine install directory from command line or user input."""
    if len(sys.argv) >= 2:
        install_dir = Path(sys.argv[1]).resolve()
    else:
        default = str(Path.home() / 'proxy-bridge')
        choice = input(f'\n  Install directory [{default}]: ').strip()
        install_dir = Path(choice if choice else default).resolve()

    install_dir.mkdir(parents=True, exist_ok=True)
    ok(f'Install directory: {install_dir}')
    return install_dir

def step_copy_source(install_dir):
    """Copy all files to the install directory."""
    import shutil

    for pyf in ['entry.py', 'local_proxy.py', 'utils.py']:
        src = SRC_DIR / pyf
        dst = install_dir / pyf
        if src.exists():
            shutil.copy2(src, dst)

    ext_src = SRC_DIR / 'extension'
    ext_dst = install_dir / 'extension'
    if ext_dst.exists():
        for item in ext_dst.rglob('*'):
            if item.is_file():
                item.unlink()
    else:
        ext_dst.mkdir(parents=True, exist_ok=True)
    for fpath in sorted(ext_src.rglob('*')):
        if fpath.is_file():
            rel = fpath.relative_to(ext_src)
            target = ext_dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fpath, target)

    nm_dir = install_dir / 'chrome-native-config'
    nm_dir.mkdir(parents=True, exist_ok=True)
    key_src = SRC_DIR / 'chrome-native-config' / 'extension-key.pem'
    if key_src.exists():
        shutil.copy2(key_src, nm_dir / 'extension-key.pem')

    ok(f'Files installed to {install_dir}')
    return ext_dst

def step_generate_run_host(install_dir, python_path):
    """Write run-host.bat for Chrome NM."""
    nm_dir = install_dir / 'chrome-native-config'
    nm_dir.mkdir(parents=True, exist_ok=True)
    bat = nm_dir / 'run-host.bat'
    entry = install_dir / 'entry.py'
    bat.write_text(
        f'@echo off\r\ncd /d "{install_dir}" && "{python_path}" "{entry}"\r\n',
        encoding='ascii'
    )
    ok('run-host.bat OK')

def step_install_cryptography(python_path):
    """Ensure cryptography is available in system Python."""
    r = subprocess.run(
        [python_path, '-c', 'import cryptography'],
        capture_output=True
    )
    if r.returncode == 0:
        ok('cryptography OK')
        return

    for cmd in [
        [python_path, '-m', 'pip', 'install', 'cryptography', '--quiet'],
        [python_path, '-m', 'pip', 'install', 'cryptography',
         '-i', 'https://pypi.tuna.tsinghua.edu.cn/simple/',
         '--trusted-host', 'pypi.tuna.tsinghua.edu.cn', '--quiet'],
    ]:
        if subprocess.run(cmd, capture_output=True).returncode == 0:
            ok('cryptography installed')
            return
    err('Cannot install cryptography')
    sys.exit(1)

def step_generate_ca(install_dir, python_path):
    """Generate Root CA using system Python (not frozen exe)."""
    ca_cert = Path.home() / '.proxy-bridge-ca' / 'ca-cert.pem'
    entry = str(install_dir / 'entry.py')
    r = subprocess.run(
        [python_path, entry, '--init-ca'],
        capture_output=True, text=True, timeout=120
    )
    if ca_cert.exists():
        ok(f'CA: {ca_cert}')
    else:
        err('CA generation failed')
        print(f'  stdout: {r.stdout}')
        print(f'  stderr: {r.stderr}')
        sys.exit(1)

def step_install_ca():
    """Install CA to Windows trust store via certutil."""
    ca_cert = str(Path.home() / '.proxy-bridge-ca' / 'ca-cert.pem')
    for flag in [['-addstore', '-f', 'Root'], ['-addstore', '-f', '-user', 'Root']]:
        subprocess.run(['certutil'] + flag + [ca_cert], capture_output=True)
    ok('CA installed to Windows Trust Store')

def step_export_ca(install_dir):
    """Copy CA certificate to install dir for Linux export."""
    import shutil
    ca_src = Path.home() / '.proxy-bridge-ca' / 'ca-cert.pem'
    ca_dst = install_dir / 'ca-cert.pem'
    if ca_src.exists():
        shutil.copy2(ca_src, ca_dst)
        ok(f'CA exported -> {ca_dst}')
        print(f'       Ubuntu: sudo cp ca-cert.pem /usr/local/share/ca-certificates/')
        print(f'       Then:   sudo update-ca-certificates')

def step_register_nm(install_dir, ext_id):
    """Register Native Messaging host in registry."""
    nm_dir = install_dir / 'chrome-native-config'
    nm_manifest = nm_dir / f'{NATIVE_NAME}.json'
    run_bat = str(nm_dir / 'run-host.bat').replace('\\', '\\\\')
    nm_data = {
        'name': NATIVE_NAME,
        'description': 'Proxy Bridge',
        'path': run_bat,
        'type': 'stdio',
        'allowed_origins': [f'chrome-extension://{ext_id}/']
    }
    nm_manifest.write_text(json.dumps(nm_data, ensure_ascii=False), encoding='utf-8')

    for hive in ['HKCU', 'HKLM']:
        subprocess.run([
            'reg', 'add',
            f'{hive}\\Software\\Google\\Chrome\\NativeMessagingHosts\\{NATIVE_NAME}',
            '/ve', '/t', 'REG_SZ', '/d', str(nm_manifest), '/f'
        ], capture_output=True)
    ok(f'NM registered -> {nm_manifest}')


# -- Main ---------------------------------------------------------------------

def main():
    ensure_admin()

    banner('Proxy Bridge v2.0 - Setup')

    # 1. Choose install directory
    print('\n[1/7] Install Directory')
    install_dir = step_choose_dir()

    # 2. Copy files
    print('\n[2/7] Install Files')
    ext_dir = step_copy_source(install_dir)

    # 3. Python + cryptography
    print('\n[3/7] Python + cryptography')
    python_path = find_python()
    step_generate_run_host(install_dir, python_path)
    step_install_cryptography(python_path)

    # 4. Root CA
    print('\n[4/7] Root CA')
    step_generate_ca(install_dir, python_path)

    # 5. Install CA
    print('\n[5/7] Install CA')
    step_install_ca()

    # 6. Export CA cert to install dir (for Linux import)
    print('\n[6/7] Export CA cert')
    step_export_ca(install_dir)

    # 7. Extension ID + NM register
    print('\n[7/7] Extension ID + NM')
    ext_id, ext_version = compute_ext_id()
    ok(f'Extension ID: {ext_id}  v{ext_version}')
    step_register_nm(install_dir, ext_id)

    banner('Setup Complete!')
    print(f'  Install dir : {install_dir}')
    print(f'  Extension   : {ext_id}')
    print(f'  CA cert     : {install_dir / "ca-cert.pem"}')
    print(f'  Proxy       : 0.0.0.0:60130')
    print()
    print('  Next steps:')
    print(f'  1. chrome://extensions -> Developer mode ON')
    print(f'  2. Load unpacked -> {ext_dir}')
    print(f'  3. Restart Chrome')
    print(f'  4. Linux: copy ca-cert.pem -> /usr/local/share/ca-certificates/')
    print()
    input('Press Enter to exit...')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f'\n{"=" * 50}')
        print(f'FATAL ERROR: {e}')
        print(f'{"=" * 50}')
        traceback.print_exc()
        print()
        input('Press Enter to exit...')
        sys.exit(1)
