"""
Proxy Bridge v2.0 — One-Click AutoSetup (Load unpacked edition)
Usage: AutoSetup.py (or run the compiled .exe)
All-in-one: CA → file copy → NM register → done.
User loads extension/ directory in Chrome manually — no ports, no policies.
"""
import sys
import os
import json
import hashlib
import base64
import subprocess
from pathlib import Path


# ── PyInstaller support ─────────────────────────────────────────────────────────
def _app_dir():
    """Return the directory containing app resources (works in exe + source)."""
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def _source_root():
    """When running as exe, resources are extracted to temporary dir.
    We need to copy Python source files to a persistent install location."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = _source_root()
SRC_DIR = _app_dir()
NH_DIR = ROOT / 'chrome-native-config'
EXT_DIR = ROOT / 'extension'
CA_DIR = Path.home() / '.proxy-bridge-ca'
CA_CERT = CA_DIR / 'ca-cert.pem'
NATIVE_NAME = 'com.example.proxy_bridge'
KEY_PEM = SRC_DIR / 'chrome-native-config' / 'extension-key.pem'

PY_FILES = ['entry.py', 'local_proxy.py', 'utils.py']
EXT_SUBDIRS = ['extension', 'chrome-native-config']


# ── Helpers ─────────────────────────────────────────────────────────────────────
def banner(msg):
    print(f'\n{"=" * 50}')
    print(msg)
    print('=' * 50)

def ok(msg):
    print(f'  [OK] {msg}')

def err(msg):
    print(f'  [ERROR] {msg}')

def ensure_admin():
    import ctypes
    if ctypes.windll.shell32.IsUserAnAdmin():
        return True
    print('[*] Requesting Administrator privileges...')
    ctypes.windll.shell32.ShellExecuteW(
        None, 'runas', sys.executable, ' '.join(f'"{a}"' for a in sys.argv), None, 1
    )
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
    ok(f'Extension ID: {ext_id}')
    return ext_id, m['version']


# ── Steps ───────────────────────────────────────────────────────────────────────

def step_copy_source():
    """Copy Python source + extension directory to the install root (for exe mode)."""
    import shutil
    NH_DIR.mkdir(parents=True, exist_ok=True)

    for pyf in PY_FILES:
        src = SRC_DIR / pyf
        dst = ROOT / pyf
        if src.exists() and src != dst:
            shutil.copy2(src, dst)

    ext_src = SRC_DIR / 'extension'
    ext_dst = ROOT / 'extension'
    if ext_src.exists() and ext_src != ext_dst:
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

    key_src = SRC_DIR / 'chrome-native-config' / 'extension-key.pem'
    key_dst = NH_DIR / 'extension-key.pem'
    if key_src.exists() and key_src != key_dst:
        shutil.copy2(key_src, key_dst)

    ok(f'Source files installed to {ROOT}')

def step_generate_run_host(python_path):
    """Write run-host.bat that Chrome NM will invoke."""
    NH_DIR.mkdir(parents=True, exist_ok=True)
    bat = NH_DIR / 'run-host.bat'
    entry = str(ROOT / 'entry.py')
    bat.write_text(
        f'@echo off\r\ncd /d "{ROOT}" && "{python_path}" "{entry}"\r\n',
        encoding='ascii'
    )
    ok(f'run-host.bat: {python_path} → {entry}')

def step_install_cryptography(python_path):
    """Ensure cryptography is installed in system Python (not just inside exe)."""
    r = subprocess.run(
        [python_path, '-c', 'import cryptography'],
        capture_output=True
    )
    if r.returncode == 0:
        ok('cryptography already installed')
        return

    pip_cmds = [
        [python_path, '-m', 'pip', 'install', 'cryptography', '--quiet'],
        [python_path, '-m', 'pip', 'install', 'cryptography',
         '-i', 'https://pypi.tuna.tsinghua.edu.cn/simple/',
         '--trusted-host', 'pypi.tuna.tsinghua.edu.cn', '--quiet'],
    ]
    for cmd in pip_cmds:
        if subprocess.run(cmd, capture_output=True).returncode == 0:
            ok('cryptography installed')
            return
    err('Cannot install cryptography')
    sys.exit(1)

def step_generate_ca(python_path):
    """Generate Root CA using system Python (not the frozen exe)."""
    entry = str(ROOT / 'entry.py')
    r = subprocess.run(
        [python_path, entry, '--init-ca'],
        capture_output=True, text=True, timeout=120
    )
    if CA_CERT.exists():
        ok(f'CA: {CA_CERT}')
    else:
        err('CA generation failed')
        print(f'  stdout: {r.stdout}')
        print(f'  stderr: {r.stderr}')
        sys.exit(1)

def step_install_ca():
    for flag in [['-addstore', '-f', 'Root'], ['-addstore', '-f', '-user', 'Root']]:
        subprocess.run(['certutil'] + flag + [str(CA_CERT)], capture_output=True)
    ok('CA installed to Windows Trust Store')

def step_register_nm(ext_id):
    nm_manifest = NH_DIR / f'{NATIVE_NAME}.json'
    run_bat = str(NH_DIR / 'run-host.bat').replace('\\', '\\\\')
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
    ok(f'NM registered: {nm_manifest}')


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    ensure_admin()

    banner('Proxy Bridge v2.0 — AutoSetup')

    # 1. Copy source files to install location (needed for bundled exe)
    print('\n[1/6] Install Files')
    step_copy_source()

    # 2. Python detection
    print('\n[2/6] Python + cryptography')
    python_path = find_python()
    step_generate_run_host(python_path)
    step_install_cryptography(python_path)

    # 3. CA generate
    print('\n[3/6] Root CA')
    step_generate_ca(python_path)

    # 4. Install CA
    print('\n[4/6] Install CA')
    step_install_ca()

    # 5. Extension ID
    print('\n[5/6] Extension ID')
    ext_id, ext_version = compute_ext_id()

    # 6. NM register
    print('\n[6/6] Native Messaging')
    step_register_nm(ext_id)

    banner('Setup Complete!')
    print(f' Proxy: 127.0.0.1:60130 (bound on 0.0.0.0)')
    print(f' Extension ID: {ext_id}')
    print(f' NM Host: {NATIVE_NAME}')
    print(f' CA: {CA_CERT}')
    print(f' Install dir: {ROOT}')
    print()
    print('  Next steps:')
    print(f'  1. Open chrome://extensions, enable Developer mode')
    print(f'  2. Click "Load unpacked" → select: {EXT_DIR}')
    print(f'  3. Restart Chrome — proxy auto-launches on startup')
    print()
    input('Press Enter to exit...')


if __name__ == '__main__':
    main()
