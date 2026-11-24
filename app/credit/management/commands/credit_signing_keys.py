"""Make a new pair of GAHK-konto signing keys: write the public halves to credit/keys/ (commit them)
and print the private halves for the environment. Run once, and again whenever a key is lost or
replaced; old signatures keep verifying with the old public halves."""

import re

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from django.core.management.base import BaseCommand, CommandError, CommandParser

from credit import signing


class Command(BaseCommand):
    help = "Make GAHK-konto's anchor and PDF signing keys (prints the private halves)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("key_id", help="A name for the pair, e.g. 2026-11 (letters, digits, - and _).")

    def handle(self, *args: object, **opts: str) -> None:
        key_id = opts["key_id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", key_id) or key_id == signing.DEV_KEY_ID:
            raise CommandError("Use letters, digits, - and _ (and not 'dev').")
        anchor_pub = signing.KEYS_DIR / f"{key_id}.anchor.pub"
        pdf_cert = signing.KEYS_DIR / f"{key_id}.pdf.crt"
        if anchor_pub.exists() or pdf_cert.exists():
            raise CommandError(f"{key_id} exists already: pick a new id.")
        anchor_key = ed25519.Ed25519PrivateKey.generate()
        anchor_pem = anchor_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        anchor_pub.write_bytes(
            anchor_key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        )
        pdf_key, cert = signing.new_pdf_identity()
        pdf_cert.write_bytes(cert)

        def env(value: bytes) -> str:
            return value.decode().strip().replace("\n", "\\n")

        self.stdout.write(f"Wrote {anchor_pub} and {pdf_cert}: commit them.\n")
        self.stdout.write("Put these in the environment (secret):\n")
        self.stdout.write(f"CREDIT_ANCHOR_KEY_ID={key_id}")
        self.stdout.write(f"CREDIT_ANCHOR_SIGNING_KEY={env(anchor_pem)}")
        self.stdout.write(f"CREDIT_PDF_SIGNING_KEY={env(pdf_key)}")
        self.stdout.write(f"CREDIT_PDF_SIGNING_CERT={env(cert)}")
