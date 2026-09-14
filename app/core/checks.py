"""Startup validation of configuration whose mistakes have no server-side symptom.

Two unrelated subjects share this module, and what they have in common is the only thing that
matters: get either wrong and Django starts, pages render, and nothing is logged.

  E001-E006  the VAPID key pair (Web Push)
  E007-E010  the media URL/prefix/backend invariants (see the second block, and core/storage.py)

VAPID lives in core because the keys are shared by every feature that pushes (Den Hurtige and
opslagstavlen), not owned by whichever one shipped first. The check IDs moved with it:
`den_hurtige.E00x` became `core.E00x`. The channel registry's own checks stayed behind in
den_hurtige.checks, because a channel is that feature's concept and nothing else's — they are
`den_hurtige.E007`-`E010`, so they do not collide with `core.E007`/`core.E008` below despite the
overlapping numbers.

A wrong VAPID key pair has no server-side symptom whatsoever: Django starts, the feed renders, the
subscribe button appears, and the only evidence is the browser refusing to subscribe — as a generic
`AbortError` from the push service, which is indistinguishable from being offline. Every mistake
that is cheap to detect is therefore detected here, so `manage.py check` (which `runserver` and the
deploy both run) reports it in one line instead.

Checked, in order of how easy each is to get wrong:
  E001  not base64url                     — e.g. a PEM header/newlines pasted in
  E002  wrong length or missing 0x04 tag  — e.g. the SPKI DER body instead of the raw point
  E003  public set, private missing
  E004  65 bytes and 0x04, but not a point on P-256 — passes a shape check, still rejected by FCM
  E005  private key is not a raw 32-byte scalar
  E006  the two keys are not each other's halves — the classic result of regenerating the pair
        and updating only one of the two environment variables

"""

import base64
import binascii
from collections.abc import Sequence

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.apps.config import AppConfig
from django.conf import settings
from django.core.checks import CheckMessage, Error

# An uncompressed P-256 point: the 0x04 tag byte followed by the 32-byte X and Y coordinates. This
# is what the browser wants as `applicationServerKey` - NOT the SPKI PEM openssl writes by default.
VAPID_KEY_BYTES = 65
UNCOMPRESSED_POINT_TAG = 0x04
VAPID_PRIVATE_KEY_BYTES = 32

CONVERT_HINT = (
    "Both keys must be raw base64url, not .pem file contents or paths. "
    "app/.env.example has the one-liner that converts a PEM to the two values this app expects."
)


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _uncompressed_point(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def check_vapid_public_key(app_configs: Sequence[AppConfig] | None, **kwargs: object) -> list[CheckMessage]:
    """Validate the VAPID key pair when push is configured at all."""
    public_b64 = getattr(settings, "VAPID_PUBLIC_KEY", "")
    private_b64 = getattr(settings, "VAPID_PRIVATE_KEY", "")
    if not public_b64:
        return []  # push deliberately disabled (the normal dev default); the UI reports it in-page

    try:
        public_raw = _b64url_decode(public_b64)
    except (binascii.Error, ValueError):
        return [Error("VAPID_PUBLIC_KEY is not valid base64url.", hint=CONVERT_HINT, id="core.E001")]

    if len(public_raw) != VAPID_KEY_BYTES or public_raw[0] != UNCOMPRESSED_POINT_TAG:
        leading = f"0x{public_raw[0]:02x}" if public_raw else "nothing"
        return [
            Error(
                f"VAPID_PUBLIC_KEY decodes to {len(public_raw)} bytes starting with {leading}; "
                f"expected {VAPID_KEY_BYTES} bytes starting with 0x04.",
                hint=CONVERT_HINT,
                id="core.E002",
            )
        ]

    if not private_b64:
        return [
            Error(
                "VAPID_PUBLIC_KEY is set but VAPID_PRIVATE_KEY is empty — browsers could subscribe "
                "but the server could not sign a single push.",
                hint="Set both, or neither to disable push.",
                id="core.E003",
            )
        ]

    try:
        public_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public_raw)
    except ValueError:
        return [
            Error(
                "VAPID_PUBLIC_KEY is the right length but is not a point on the P-256 curve. The "
                "browser's push service rejects it, which surfaces as a generic subscribe failure.",
                hint=CONVERT_HINT,
                id="core.E004",
            )
        ]

    try:
        private_raw = _b64url_decode(private_b64)
        if len(private_raw) != VAPID_PRIVATE_KEY_BYTES:
            raise ValueError(f"expected {VAPID_PRIVATE_KEY_BYTES} bytes, got {len(private_raw)}")
        private_key = ec.derive_private_key(int.from_bytes(private_raw, "big"), ec.SECP256R1())
    except (binascii.Error, ValueError) as exc:
        return [
            Error(
                f"VAPID_PRIVATE_KEY is not a raw base64url P-256 scalar ({exc}).",
                hint=CONVERT_HINT,
                id="core.E005",
            )
        ]

    if _uncompressed_point(private_key.public_key()) != _uncompressed_point(public_key):
        return [
            Error(
                "VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY are not halves of the same key pair. "
                "Browsers subscribe against the public key and the server signs with the private "
                "one, so no push can ever be delivered.",
                hint=(
                    "Re-derive BOTH values from one .pem — updating only one of them after "
                    "regenerating the pair is the usual cause. See app/.env.example."
                ),
                id="core.E006",
            )
        ]

    return []


# --- Media URL, prefix and backend invariants (core.E007-E010) -------------------------------------------------------
#
# Not about push at all, but it belongs beside the VAPID checks for the same reason those are here:
# it is a configuration mistake with no server-side symptom. See core.storage's module docstring for
# the full account of what breaks. In short, MEDIA_URL is a prefix of content stored in the
# database — cms.Page.background_image, /media/ URLs inside CMS bodies, opslag Markdown — so changing
# it silently unlinks live images and arms purge_notices to delete the files behind them.

MEDIA_URL_REQUIRED = "/media/"

MEDIA_URL_HINT = (
    "cms.Page.background_image stores the URL string itself, CMS bodies embed <img src=/media/...>, "
    "and opslag bodies embed the same in Markdown — so MEDIA_URL is effectively a database value. "
    "Serving media from object storage does NOT require changing it: core.storage.MediaS3Storage "
    "keeps returning /media/<name> and core.media.serve_media redirects that to a presigned URL. "
    "Changing it for real would need a data migration over Notice.body, Page.background_image, "
    "PageVersion.background_image and RoomConditionScore.image."
)


def check_media_url(app_configs: Sequence[AppConfig] | None, **kwargs: object) -> list[CheckMessage]:
    """MEDIA_URL must stay `/media/`, whatever the storage backend is.

    Deliberately unconditional rather than gated on S3 being active: the coupling is to stored
    content, not to the backend, so pointing FileSystemStorage at a different MEDIA_URL breaks
    exactly the same things.
    """
    if settings.MEDIA_URL == MEDIA_URL_REQUIRED:
        return []
    return [
        Error(
            f"MEDIA_URL is {settings.MEDIA_URL!r}, but it must be {MEDIA_URL_REQUIRED!r}.",
            hint=MEDIA_URL_HINT,
            id="core.E007",
        )
    ]


def check_media_storage_url(app_configs: Sequence[AppConfig] | None, **kwargs: object) -> list[CheckMessage]:
    """The configured default storage must produce site-relative /media/ URLs.

    Catches the mistake core.E007 cannot: STORAGES["default"] pointed at plain
    `storages.backends.s3.S3Storage` instead of `core.storage.MediaS3Storage`. MEDIA_URL is then
    still correct, but `.url()` returns a presigned https URL carrying an X-Amz-Signature — which
    the compose toolbar writes into a Notice body and the CMS admin writes into
    Page.background_image, both of which expire within the hour and are then wrong permanently.
    """
    from django.core.files.storage import storages

    try:
        # A representative name: a real subdirectory, an extension, nothing needing quoting.
        produced = storages["default"].url("opslag/2026/09/probe.jpg")
    except Exception as exc:  # a misconfigured backend must report, not crash `manage.py check`
        return [
            Error(
                f"STORAGES['default'] could not produce a URL ({exc.__class__.__name__}: {exc}).",
                hint="Check the STORAGES['default'] BACKEND and OPTIONS in config/settings.py.",
                id="core.E008",
            )
        ]

    if produced.startswith(MEDIA_URL_REQUIRED):
        return []
    return [
        Error(
            f"STORAGES['default'].url() returned {produced!r}, which is not under {MEDIA_URL_REQUIRED!r}.",
            hint=(
                "Use core.storage.MediaS3Storage, not storages.backends.s3.S3Storage directly. "
                + MEDIA_URL_HINT
            ),
            id="core.E008",
        )
    ]


def check_media_storage_prefix(
    app_configs: Sequence[AppConfig] | None, **kwargs: object
) -> list[CheckMessage]:
    """An object-storage media backend must keep its key prefix.

    core.storage.MediaS3Storage defaults `location` to MEDIA_PREFIX, but a default cannot stop
    someone writing `"location": ""` into STORAGES OPTIONS, and an empty one is not cosmetic.
    django-storages resolves names with `safe_join(self.location, name)`: with a prefix, a name that
    climbs out of it raises; with no prefix there is nothing to climb out of, so `../../x` quietly
    normalises to `x`. Since /media/ is public and unauthenticated, that turns the view into a read
    primitive for the whole bucket — including the `backups/` prefix the database dumps land in.
    """
    from django.core.files.storage import storages

    from core.storage import MediaS3Storage

    storage = storages["default"]
    if not isinstance(storage, MediaS3Storage):
        return []  # FileSystemStorage confines itself to MEDIA_ROOT; no prefix concept applies.
    if storage.location:
        return []
    return [
        Error(
            "The media object-storage backend has an empty `location`, so a /media/ request "
            "containing `..` can reach any key in the bucket (the database backups included).",
            hint='Set "location": "media" in STORAGES["default"]["OPTIONS"], or omit the key and '
            "let core.storage.MediaS3Storage default it.",
            id="core.E009",
        )
    ]


def check_media_backend_in_production(
    app_configs: Sequence[AppConfig] | None, **kwargs: object
) -> list[CheckMessage]:
    """Production must not fall back to local-disk media by accident.

    S3_BUCKET is deliberately the only switch between the bucket and MEDIA_ROOT, which made the
    migration safe: unset it and the app was exactly as it had always been. That property died the
    day the `media` volume was emptied. Now an unset S3_BUCKET means the app serves nothing, and
    quietly writes new uploads onto a disk nobody backs up and the next container rebuild discards —
    with no error, because falling back is precisely what the setting is designed to do.

    A missing environment variable is not an exotic failure. It is a mistyped key in Coolify, a
    resource recreated from a stale template, a restore that predates the migration. Each of those
    is a normal Tuesday, and each currently produces a site with no pictures and no log line.

    Escape hatch rather than an absolute rule: DEBUG covers dev and CI, and ALLOW_LOCAL_MEDIA=1
    covers the legitimate prod-shaped exception — a staging box with no bucket of its own, or
    rehearsing the rollback while the volume still has files in it. Requiring the operator to say so
    out loud is the whole point; the failure this prevents is silence.
    """
    if settings.DEBUG or settings.S3_BUCKET or settings.ALLOW_LOCAL_MEDIA:
        return []
    return [
        Error(
            "DEBUG is off but S3_BUCKET is unset, so uploads would be read from and written to "
            "MEDIA_ROOT on local disk.",
            hint=(
                "Set S3_BUCKET (with S3_ACCESS_KEY/S3_SECRET_KEY) — see DEPLOY.md 4c. If local-disk "
                "media really is intended here, set ALLOW_LOCAL_MEDIA=1 to say so explicitly."
            ),
            id="core.E010",
        )
    ]
