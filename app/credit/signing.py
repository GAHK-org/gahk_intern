"""Signatures: on the anchors (Ed25519) and on the statement PDFs (an ECDSA P-256 certificate).

Nothing depends on a key staying alive. The ledger's integrity is its keyless hash chains and the
anchors kept outside the database; a signature only adds "the intranet made this". Every signature
stores its key's id, and the public half of every key ever used is in the repository
(credit/keys/), so an old signature still verifies after its private key is lost or replaced. A lost
key is a routine event: `manage.py credit_signing_keys` makes a new pair, its public half is
committed, and the new private half goes in the environment.

Two kinds of key, for two readers:
  * anchors: Ed25519, small and fast, verified by our own code (credit.checks) or anyone with the
    public key;
  * PDFs: ECDSA P-256 with a self-signed certificate, which PDF readers (Adobe, Preview, browsers)
    understand. A reader shows "unknown signer" until the certificate is trusted; a certificate from
    a trusted provider can replace it later without changing anything else.

Settings (environment):
  CREDIT_ANCHOR_SIGNING_KEY   the anchor key's private half, PEM;   CREDIT_ANCHOR_KEY_ID its id
  CREDIT_PDF_SIGNING_KEY      the PDF key's private half, PEM;      CREDIT_PDF_SIGNING_CERT its cert
Under DEBUG with nothing configured, a throwaway key is made per process so local pages work.
"""

import datetime
import functools
import io
import logging
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.x509.oid import NameOID
from django.conf import settings

logger = logging.getLogger(__name__)

KEYS_DIR = Path(__file__).resolve().parent / "keys"
DEV_KEY_ID = "dev"


class SigningUnavailable(Exception):
    """No key is configured, and this is no development process."""


# ---- anchors: Ed25519 -----------------------------------------------------------------------------


@functools.cache
def _dev_anchor_key() -> ed25519.Ed25519PrivateKey:
    return ed25519.Ed25519PrivateKey.generate()


def _anchor_key() -> tuple[ed25519.Ed25519PrivateKey, str] | None:
    pem = getattr(settings, "CREDIT_ANCHOR_SIGNING_KEY", "")
    if pem:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        if not isinstance(key, ed25519.Ed25519PrivateKey):
            raise SigningUnavailable("CREDIT_ANCHOR_SIGNING_KEY is not an Ed25519 key")
        return key, settings.CREDIT_ANCHOR_KEY_ID
    if settings.DEBUG:
        return _dev_anchor_key(), DEV_KEY_ID
    return None


def sign_anchor(message: bytes) -> tuple[bytes, str]:
    """(signature, key id), or (b"", "") when no key is configured: the anchor is still made and
    delivered, unsigned, and the log says so."""
    found = _anchor_key()
    if found is None:
        logger.error("GAHK-konto: no CREDIT_ANCHOR_SIGNING_KEY; anchor left unsigned")
        return b"", ""
    key, key_id = found
    return key.sign(message), key_id


def anchor_public_key(key_id: str) -> ed25519.Ed25519PublicKey | None:
    if key_id == DEV_KEY_ID and settings.DEBUG:
        return _dev_anchor_key().public_key()
    path = KEYS_DIR / f"{key_id}.anchor.pub"
    if not key_id or "/" in key_id or not path.is_file():
        return None
    key = serialization.load_pem_public_key(path.read_bytes())
    return key if isinstance(key, ed25519.Ed25519PublicKey) else None


def verify_anchor(message: bytes, signature: bytes, key_id: str) -> bool:
    key = anchor_public_key(key_id)
    if key is None or not signature:
        return False
    try:
        key.verify(signature, message)
    except InvalidSignature:
        return False
    return True


# ---- PDFs: ECDSA P-256 with a certificate --------------------------------------------------------


def new_pdf_identity(*, days: int = 3650) -> tuple[bytes, bytes]:
    """(private key PEM, self-signed certificate PEM) for signing statements."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "GAHK Intern - GAHK-konto"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "G. A. Hagemanns Kollegium"),
            x509.NameAttribute(NameOID.COUNTRY_NAME, "DK"),
        ]
    )
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=True,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return key_pem, cert.public_bytes(serialization.Encoding.PEM)


@functools.cache
def _dev_pdf_identity() -> tuple[bytes, bytes]:
    return new_pdf_identity(days=30)


def _pdf_identity() -> tuple[bytes, bytes]:
    key = getattr(settings, "CREDIT_PDF_SIGNING_KEY", "")
    cert = getattr(settings, "CREDIT_PDF_SIGNING_CERT", "")
    if key and cert:
        return key.encode(), cert.encode()
    if settings.DEBUG:
        return _dev_pdf_identity()
    raise SigningUnavailable("CREDIT_PDF_SIGNING_KEY / CREDIT_PDF_SIGNING_CERT are not set")


def sign_pdf(pdf: bytes, *, reason: str) -> bytes:
    """The PDF with a digital signature over all of it (an incremental update, as PDF readers expect)."""
    from pyhanko.keys import load_certs_from_pemder_data, load_private_key_from_pemder_data
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import signers
    from pyhanko_certvalidator.registry import SimpleCertificateStore

    key_pem, cert_pem = _pdf_identity()
    cert = next(iter(load_certs_from_pemder_data(cert_pem)))
    signer = signers.SimpleSigner(
        signing_cert=cert,
        signing_key=load_private_key_from_pemder_data(key_pem, passphrase=None),
        cert_registry=SimpleCertificateStore.from_certs([cert]),
    )
    meta = signers.PdfSignatureMetadata(
        field_name="GAHKKonto", reason=reason, location="G. A. Hagemanns Kollegium", md_algorithm="sha256"
    )
    out = signers.sign_pdf(IncrementalPdfFileWriter(io.BytesIO(pdf)), meta, signer=signer)
    return out.getvalue()


def pdf_certificate_fingerprint() -> str:
    """SHA-256 of the signing certificate, as printed on statements so a reader can compare it."""
    _, cert_pem = _pdf_identity()
    cert = x509.load_pem_x509_certificate(cert_pem)
    return cert.fingerprint(hashes.SHA256()).hex()
