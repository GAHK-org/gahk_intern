# GAHK-konto signing keys (public halves)

The public half of every key GAHK-konto has signed with, committed so an old signature still
verifies after its private key is lost or replaced (credit.signing).

- `<id>.anchor.pub`: an Ed25519 key that signs anchors.
- `<id>.pdf.crt`: the certificate that signs statement PDFs.

`python manage.py credit_signing_keys <id>` writes both here and prints the private halves for the
environment (CREDIT_ANCHOR_SIGNING_KEY, CREDIT_ANCHOR_KEY_ID, CREDIT_PDF_SIGNING_KEY,
CREDIT_PDF_SIGNING_CERT). Never commit a private key.
