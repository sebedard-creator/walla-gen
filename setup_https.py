import argparse
import ipaddress
import os
import socket
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
CERT_DIR = os.path.join(ROOT_DIR, 'certs')
CA_CERT_PATH = os.path.join(CERT_DIR, 'walla-local-ca.crt')
SERVER_KEY_PATH = os.path.join(CERT_DIR, 'walla-server.key')
SERVER_CERT_PATH = os.path.join(CERT_DIR, 'walla-server.crt')


def detect_lan_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(('10.255.255.255', 1))
        return sock.getsockname()[0]
    finally:
        sock.close()


def write_private_key(path, key):
    with open(path, 'wb') as output:
        output.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ))


def write_certificate(path, certificate):
    with open(path, 'wb') as output:
        output.write(certificate.public_bytes(serialization.Encoding.PEM))


def generate_certificates(lan_ip, force=False):
    paths = (CA_CERT_PATH, SERVER_KEY_PATH, SERVER_CERT_PATH)
    existing = [path for path in paths if os.path.exists(path)]
    if existing and not force:
        raise FileExistsError(
            'Des certificats existent déjà. Utilisez --force pour les remplacer.'
        )

    os.makedirs(CERT_DIR, exist_ok=True)
    now = datetime.now(timezone.utc)

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    ca_name = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'walla-gen'),
        x509.NameAttribute(NameOID.COMMON_NAME, 'walla-gen Local CA'),
    ])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
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

    server_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    server_name = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'walla-gen'),
        x509.NameAttribute(NameOID.COMMON_NAME, str(lan_ip)),
    ])
    alternative_names = [
        x509.IPAddress(ipaddress.ip_address(lan_ip)),
        x509.IPAddress(ipaddress.ip_address('127.0.0.1')),
        x509.DNSName('localhost'),
    ]
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_cert.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=825))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName(alternative_names), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    write_certificate(CA_CERT_PATH, ca_cert)
    write_private_key(SERVER_KEY_PATH, server_key)
    write_certificate(SERVER_CERT_PATH, server_cert)

    print(f'Certificats créés pour {lan_ip}:')
    print(f'  Autorité publique à installer: {CA_CERT_PATH}')
    print(f'  Certificat du serveur: {SERVER_CERT_PATH}')
    print(f'  Clé privée du serveur: {SERVER_KEY_PATH}')


def main():
    parser = argparse.ArgumentParser(description='Crée les certificats HTTPS locaux de walla-gen.')
    parser.add_argument('--ip', default=detect_lan_ip(), help='Adresse IPv4 LAN du serveur.')
    parser.add_argument('--force', action='store_true', help='Remplace les certificats existants.')
    args = parser.parse_args()
    lan_ip = ipaddress.ip_address(args.ip)
    if lan_ip.version != 4:
        parser.error("L'adresse doit être une adresse IPv4.")
    generate_certificates(lan_ip, args.force)


if __name__ == '__main__':
    main()
