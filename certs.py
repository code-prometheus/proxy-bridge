"""
Proxy Bridge — Certificate Manager.
Per-host certificate generation for MITM TLS termination.
Migrated from utils.py.
"""
import datetime
import logging
import os
import subprocess
import sys

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509 import ExtensionNotFound

logger = logging.getLogger('proxy_bridge.certs')

CA_DIR = os.path.join(os.path.expanduser('~'), '.proxy-bridge-ca')
CERTS_DIR = os.path.join(CA_DIR, 'certs')
os.makedirs(CERTS_DIR, exist_ok=True)


class CertManager:
    CA_CERT_PATH = os.path.join(CA_DIR, 'ca-cert.pem')
    CA_KEY_PATH = os.path.join(CA_DIR, 'ca-key.pem')

    @classmethod
    def get_ca(cls):
        """
        Generate or load the root CA certificate.

        Returns:
            (ca_cert, ca_key) — cryptography objects.
        """
        if os.path.exists(cls.CA_CERT_PATH) and os.path.exists(cls.CA_KEY_PATH):
            with open(cls.CA_CERT_PATH, 'rb') as f:
                ca_cert = x509.load_pem_x509_certificate(f.read())
            with open(cls.CA_KEY_PATH, 'rb') as f:
                ca_key = serialization.load_pem_private_key(f.read(), password=None)
            return ca_cert, ca_key

        # Generate new CA
        ca_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )

        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, 'Proxy Bridge Local CA'),
        ])

        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.utcnow())
            .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365 * 10))
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=None),
                critical=True,
            )
            .add_extension(
                x509.KeyUsage(
                    key_cert_sign=True,
                    crl_sign=True,
                    digital_signature=False,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256())
        )

        # Persist to disk
        with open(cls.CA_CERT_PATH, 'wb') as f:
            f.write(ca_cert.public_bytes(serialization.Encoding.PEM))
        with open(cls.CA_KEY_PATH, 'wb') as f:
            f.write(ca_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            ))

        logger.info("Generated new CA certificate and key.")
        return ca_cert, ca_key

    @classmethod
    def get_cert_for_host(cls, host):
        """
        Generate per-host certificate signed by CA for MITM.

        Args:
            host: hostname, optionally with port (host:port).

        Returns:
            (cert_path, key_path) — paths to PEM files.
        """
        # Strip port if present
        host = host.split(':')[0]

        cert_path = os.path.join(CERTS_DIR, f'{host}.crt')
        key_path = os.path.join(CERTS_DIR, f'{host}.key')

        # Return cached if available
        if os.path.exists(cert_path) and os.path.exists(key_path):
            return cert_path, key_path

        ca_cert, ca_key = cls.get_ca()

        # Generate host key
        host_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )

        subject = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, host),
        ])

        # Get CA SubjectKeyIdentifier for AuthorityKeyIdentifier
        try:
            ca_ski_ext = ca_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
            ca_ski = ca_ski_ext.value
        except ExtensionNotFound:
            ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())

        # Build SAN: IP addresses use IPAddress, hostnames use DNSName.
        import ipaddress as _ipaddress
        try:
            _ipaddress.ip_address(host)
            san = [x509.IPAddress(_ipaddress.ip_address(host))]
        except ValueError:
            san = [x509.DNSName(host)]

        host_cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(ca_cert.subject)
            .public_key(host_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.utcnow())
            .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365))
            .add_extension(
                x509.SubjectAlternativeName(san),
                critical=False,
            )
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None),
                critical=True,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_encipherment=True,
                    key_cert_sign=False,
                    crl_sign=False,
                    content_commitment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
                critical=False,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(host_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier(
                    key_identifier=ca_ski.digest,
                    authority_cert_issuer=None,
                    authority_cert_serial_number=None,
                ),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256())
        )

        # Persist host cert and key
        with open(cert_path, 'wb') as f:
            f.write(host_cert.public_bytes(serialization.Encoding.PEM))
        with open(key_path, 'wb') as f:
            f.write(host_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            ))

        logger.info("Generated new certificate for host: %s", host)
        return cert_path, key_path

    @classmethod
    def install_ca_to_system(cls):
        """
        Install CA to Windows root store via certutil.

        Returns:
            (success: bool, message: str)
        """
        cls.get_ca()  # ensure CA exists
        cmds = [
            ['certutil', '-addstore', '-f', 'Root', cls.CA_CERT_PATH],
            ['certutil', '-addstore', '-f', '-user', 'Root', cls.CA_CERT_PATH],
        ]
        for cmd in cmds:
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if r.returncode == 0:
                    return True, f"Installed: {' '.join(cmd[:3])}"
            except Exception:
                pass
        return False, "Installation failed. Run as Administrator."
