"""
Proxy Bridge v2.0 — One-Click AutoSetup
Usage: AutoSetup.py  (or run the compiled .exe)
All-in-one: CA → CRX → extension ID → NM register → Chrome force-install
"""
import sys, os, json, hashlib, base64, subprocess, struct, time
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
        # Use the directory the exe lives in as install root
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = _source_root()
SRC_DIR = _app_dir()  # where extension, key, py files live (bundled in exe)
NH_DIR = ROOT / 'chrome-native-config'
CA_DIR = Path.home() / '.proxy-bridge-ca'
CA_CERT = CA_DIR / 'ca-cert.pem'
NATIVE_NAME = 'com.example.proxy_bridge'
KEY_PEM = SRC_DIR / 'chrome-native-config' / 'extension-key.pem'
CRX_FILE = NH_DIR / 'proxy-bridge.crx'
UPDATE_PORT = 58999

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

    # Copy Python source files to ROOT
    for pyf in PY_FILES:
        src = SRC_DIR / pyf
        dst = ROOT / pyf
        if src.exists() and src != dst:
            shutil.copy2(src, dst)

    # Copy extension directory
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

    # Copy extension-key.pem
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
    # Check system Python, not current process (exe has it bundled)
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

def step_pack_crx(ext_id, ext_version):
    try:
        from cryptography.hazmat.primitives import serialization, hashes
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:
        err('cryptography not available — skipping CRX')
        return False

    import zipfile, io
    ext_dir = ROOT / 'extension'

    with open(KEY_PEM, 'rb') as f:
        key = serialization.load_pem_private_key(f.read(), password=None)

    pub_key_der = key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    )

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for fpath in sorted(ext_dir.rglob('*')):
            if fpath.is_file():
                zf.write(fpath, fpath.relative_to(ext_dir))
    zip_body = zip_buf.getvalue()

    signature = key.sign(zip_body, padding.PKCS1v15(), hashes.SHA256())

    header = b'Cr24' + struct.pack('<I', 2)
    header += struct.pack('<I', len(pub_key_der))
    header += struct.pack('<I', len(signature))
    header += pub_key_der + signature

    NH_DIR.mkdir(parents=True, exist_ok=True)
    CRX_FILE.write_bytes(header + zip_body)
    ok(f'CRX packed: {CRX_FILE} ({len(header + zip_body)} bytes)')
    return True

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

def step_force_install(ext_id):
    """Force-install via Chrome policy ExtensionInstallForcelist.
    Uses a localhost update URL — the update server step will serve the CRX.
    Chrome polls policies within ~3 minutes of detecting the registry change."""
    policy_value = f'{ext_id};http://127.0.0.1:{UPDATE_PORT}/update.xml'
    for hive in ['HKLM', 'HKCU']:
        key = f'{hive}\\Software\\Policies\\Google\\Chrome\\ExtensionInstallForcelist'
        r = subprocess.run([
            'reg', 'add', key, '/v', '1', '/t', 'REG_SZ', '/d', policy_value, '/f'
        ], capture_output=True)
        if r.returncode == 0:
            ok(f'ExtensionInstallForcelist: {key} → {ext_id}')
            return
    err('Could not set ExtensionInstallForcelist')

def step_serve_update(ext_id, ext_version):
    """One-shot HTTP server so Chrome can fetch update.xml + CRX."""
    import http.server, threading
    import xml.etree.ElementTree as ET

    gupdate = ET.Element('gupdate', {
        'xmlns': 'http://www.google.com/update2/response',
        'protocol': '2.0'
    })
    app = ET.SubElement(gupdate, 'app', {'appid': ext_id})
    ET.SubElement(app, 'updatecheck', {
        'codebase': f'http://127.0.0.1:{UPDATE_PORT}/proxy-bridge.crx',
        'version': ext_version
    })
    update_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n' +
        ET.tostring(gupdate, encoding='unicode')
    )

    hit_count = [0]
    max_hits = 3

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(NH_DIR), **kwargs)

        def do_GET(self):
            hit_count[0] += 1
            if self.path.endswith('.crx'):
                self.send_response(200)
                self.send_header('Content-Type', 'application/x-chrome-extension')
                self.send_header('Content-Length', str(CRX_FILE.stat().st_size))
                self.end_headers()
                with open(CRX_FILE, 'rb') as fh:
                    self.wfile.write(fh.read())
                print(f'  [update-server] Served CRX')
            elif self.path.endswith('update.xml'):
                body = update_xml.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/xml')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                print(f'  [update-server] Served update.xml')
            if hit_count[0] >= max_hits:
                threading.Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, format, *args):
            pass

    server = http.server.HTTPServer(('127.0.0.1', UPDATE_PORT), Handler)
    server.timeout = 1
    print(f'\n  [*] Update server on 127.0.0.1:{UPDATE_PORT}')
    print(f'  [*] Chrome must RESTART to read new policies. Restart Chrome NOW.')
    print(f'  [*] Extension will load on restart.')
    print(f'  \n')
    deadline = time.time() + 10
    while time.time() < deadline and hit_count[0] < max_hits:
        server.handle_request()
    server.server_close()
    if hit_count[0] > 0:
        ok(f'Extension install served ({hit_count[0]} requests)')
    else:
        print(f'  [*] No install requests in 10s — Chrome may need restart')


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    ensure_admin()

    banner('Proxy Bridge v2.0 — AutoSetup')

    # 1. Copy source files to install location (needed for bundled exe)
    print('\n[1/8] Install Files')
    step_copy_source()

    # 2. Python detection
    print('\n[2/8] Python')
    python_path = find_python()
    step_generate_run_host(python_path)

    # 3. cryptography
    print('\n[3/8] cryptography')
    step_install_cryptography(python_path)

    # 4. CA generate
    print('\n[4/8] Root CA')
    step_generate_ca(python_path)

    # 5. Install CA
    print('\n[5/8] Install CA')
    step_install_ca()

    # 6. Extension ID + CRX pack
    print('\n[6/8] Extension + CRX')
    ext_id, ext_version = compute_ext_id()
    crx_ok = step_pack_crx(ext_id, ext_version)

    # 7. NM register
    print('\n[7/8] Native Messaging')
    step_register_nm(ext_id)

    # 8. Force-install in Chrome
    print('\n[8/8] Chrome Policy')
    step_force_install(ext_id)

    # Serve CRX for Chrome auto-install
    if crx_ok:
        step_serve_update(ext_id, ext_version)

    banner('Setup Complete!')
    print(f'  Proxy:      127.0.0.1:60130  →  0.0.0.0:60130')
    print(f'  Extension:  {ext_id}  (force-installed)')
    print(f'  NM Host:    {NATIVE_NAME}')
    print(f'  CA:         {CA_CERT}')
    print(f'  Install:    {ROOT}')
    print(f'\n  Next: Restart Chrome — proxy auto-launches on startup\n')
    input('Press Enter to exit...')


if __name__ == '__main__':
    main()
