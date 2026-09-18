"""HTTPS material for the configuration UI (CLAUDE.md section 3.2).

The web UI is served over TLS by default. On first run - or whenever the stored
certificate is missing or expired - a self-signed certificate is generated into
``<data-dir>/certs``. An operator can point ``tls_cert_path`` /
``tls_key_path`` at a real certificate instead; those are used verbatim and
never overwritten.

Self-signed material is generated locally with ``cryptography``; nothing here
reaches the network, which matters on an air-gapped factory VLAN.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

logger = logging.getLogger(__name__)

CERT_VALIDITY_DAYS = 825  # the CA/Browser Forum maximum for server certificates
RENEW_BEFORE_DAYS = 30


def _local_names() -> tuple[list[str], list[str]]:
    """Best-effort hostnames and IPs to put in the SAN extension."""
    names = {"localhost"}
    addresses = {"127.0.0.1", "::1"}
    try:
        hostname = socket.gethostname()
        if hostname:
            names.add(hostname)
        for info in socket.getaddrinfo(hostname, None):
            address = info[4][0]
            if address:
                addresses.add(address.split("%", 1)[0])
    except OSError as exc:  # pragma: no cover - depends on host networking
        logger.debug("could not enumerate local addresses: %s", exc)
    return sorted(names), sorted(addresses)


def generate_self_signed(cert_path: Path, key_path: Path) -> None:
    """Write a fresh self-signed certificate and 2048-bit RSA key."""
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    hostnames, addresses = _local_names()
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, hostnames[0] if hostnames else "localhost"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Snap7 Industrial Gateway"),
        ]
    )
    alt_names: list[x509.GeneralName] = [x509.DNSName(h) for h in hostnames]
    for address in addresses:
        try:
            alt_names.append(x509.IPAddress(ipaddress.ip_address(address)))
        except ValueError:
            continue

    now = dt.datetime.now(dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=CERT_VALIDITY_DAYS))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    try:
        key_path.chmod(0o600)  # the private key must not be world-readable
    except OSError:  # pragma: no cover - Windows ACLs differ
        pass
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    logger.warning(
        "generated a self-signed TLS certificate at %s - browsers will warn until it is "
        "trusted or replaced with a certificate from your own CA",
        cert_path,
    )


def _needs_regeneration(cert_path: Path, key_path: Path) -> bool:
    if not cert_path.exists() or not key_path.exists():
        return True
    try:
        certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except Exception as exc:  # noqa: BLE001 - corrupt file: regenerate
        logger.warning("stored certificate could not be parsed (%s), regenerating", exc)
        return True
    expiry = certificate.not_valid_after_utc
    if expiry <= dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=RENEW_BEFORE_DAYS):
        logger.warning("stored certificate expires %s, regenerating", expiry.isoformat())
        return True
    return False


def ensure_certificate(
    cert_dir: Path,
    *,
    cert_override: str = "",
    key_override: str = "",
) -> tuple[Path, Path]:
    """Return usable ``(cert, key)`` paths, generating a self-signed pair if needed.

    An operator-supplied pair is validated for existence only - its contents are
    the operator's business and are never rewritten.
    """
    if cert_override and key_override:
        cert_path, key_path = Path(cert_override), Path(key_override)
        if cert_path.exists() and key_path.exists():
            logger.info("using operator-supplied TLS certificate %s", cert_path)
            return cert_path, key_path
        logger.error(
            "configured TLS certificate/key not found (%s, %s); falling back to the "
            "self-signed certificate",
            cert_override,
            key_override,
        )

    cert_path = Path(cert_dir) / "server.crt"
    key_path = Path(cert_dir) / "server.key"
    if _needs_regeneration(cert_path, key_path):
        generate_self_signed(cert_path, key_path)
    return cert_path, key_path


def certificate_summary(cert_path: Path) -> dict[str, str]:
    """Short description of the active certificate for the Security page."""
    try:
        certificate = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    return {
        "subject": certificate.subject.rfc4514_string(),
        "issuer": certificate.issuer.rfc4514_string(),
        "not_before": certificate.not_valid_before_utc.isoformat(timespec="seconds"),
        "not_after": certificate.not_valid_after_utc.isoformat(timespec="seconds"),
        "serial": f"{certificate.serial_number:X}",
        "self_signed": str(certificate.subject == certificate.issuer),
    }
