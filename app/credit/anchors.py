"""Anchors: fixing the ledger's state outside the database.

An anchor is one hash (a Merkle root, credit.chain) over the chain head of every account as of a
moment: each account's last posting from before it. Anyone able to rewrite the database could
recompute every chain, but not every copy of an anchor already handed out, and a rewritten history
no longer gives the anchored root.

  daily    00:20 every night, every account (credit.tasks.anchor_daily)
  month    the first moment of each month, every account: what every monthly statement prints, so
           each holder's PDF witnesses the whole ledger
  closing  one book's accounts as of its period's end (credit.closing)

Each anchor is signed (credit.signing), chained to the previous anchor of its scope, and delivered:
to a bucket with S3 Object Lock (write once, kept for CREDIT_ANCHOR_RETENTION_DAYS) when
CREDIT_ANCHOR_BUCKET names one that has it, otherwise by e-mail to CREDIT_ANCHOR_EMAILS.
"""

import datetime
import hashlib
import json
import logging
from dataclasses import dataclass

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import IntegrityError, transaction
from django.utils import timezone

from core import mail as outbox

from . import chain, signing
from .models import Anchor, AnchorDelivery, Book, CreditAccount, Posting

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Head:
    account_id: int
    head: bytes
    balance: int
    posting_id: int | None


def heads_as_of(as_of: datetime.datetime, *, book: Book | None = None) -> list[Head]:
    """Every account that existed at `as_of` (in `book`, or all), with its last posting before then."""
    accounts = CreditAccount.objects.filter(created_at__lt=as_of)
    postings = Posting.objects.filter(transfer__created_at__lt=as_of)
    if book is not None:
        accounts = accounts.filter(book=book)
        postings = postings.filter(account__book=book)
    latest = {
        row["account_id"]: row
        for row in postings.order_by("account_id", "-id")
        .distinct("account_id")
        .values("account_id", "id", "row_hash", "balance_after")
    }
    out = []
    # An account counts from its creation, or from its first posting should that be dated earlier.
    for pk in sorted(set(accounts.values_list("pk", flat=True)) | set(latest)):
        row = latest.get(pk)
        if row is None:
            out.append(Head(pk, chain.GENESIS, 0, None))
        else:
            out.append(Head(pk, bytes(row["row_hash"]), row["balance_after"], row["id"]))
    return out


def message(anchor: Anchor) -> bytes:
    """What an anchor's hash and signature cover: its scope, moment, root, counts and predecessor."""
    previous = bytes(anchor.previous.anchor_hash) if anchor.previous is not None else bytes(32)
    payload = {
        "v": 1,
        "kind": anchor.kind,
        "book": anchor.book_id,
        "as_of": anchor.as_of.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "root": bytes(anchor.root).hex(),
        "accounts": anchor.accounts,
        "postings": anchor.postings,
        "previous": previous.hex(),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _scope(kind: str, book: Book | None) -> Anchor | None:
    found = Anchor.objects.filter(kind=kind)
    found = found.filter(book=book) if book is not None else found.filter(book__isnull=True)
    return found.order_by("-as_of", "-id").first()


def make(kind: str, as_of: datetime.datetime, *, book: Book | None = None) -> Anchor:
    """The anchor of `kind` for `as_of` (made once; asking again returns it)."""
    existing = Anchor.objects.filter(kind=kind, as_of=as_of, book=book).first()
    if existing is not None:
        return existing
    heads = heads_as_of(as_of, book=book)
    postings = Posting.objects.filter(transfer__created_at__lt=as_of)
    if book is not None:
        postings = postings.filter(account__book=book)
    anchor = Anchor(
        kind=kind,
        book=book,
        as_of=as_of,
        root=chain.merkle_root((h.account_id, h.head) for h in heads),
        accounts=len(heads),
        postings=postings.count(),
        previous=_scope(kind, book),
    )
    body = message(anchor)
    anchor.anchor_hash = hashlib.sha256(body).digest()
    anchor.signature, anchor.key_id = signing.sign_anchor(body)
    try:
        with transaction.atomic():
            anchor.save()
    except IntegrityError:  # made meanwhile by a second run
        return Anchor.objects.get(kind=kind, as_of=as_of, book=book)
    return anchor


def document(anchor: Anchor) -> dict[str, object]:
    """The anchor as it is delivered: everything needed to check it later without the database."""
    return {
        "what": "GAHK-konto anchor",
        "message": json.loads(message(anchor)),
        "anchor_hash": bytes(anchor.anchor_hash).hex(),
        "signature": bytes(anchor.signature).hex(),
        "key_id": anchor.key_id,
        "made_at": anchor.made_at.isoformat(),
    }


def _bucket_client():  # noqa: ANN202 — a boto3 S3 client
    from core.health import _s3_client

    return _s3_client()


def _object_lock_enabled(client, bucket: str) -> bool:  # noqa: ANN001
    try:
        config = client.get_object_lock_configuration(Bucket=bucket)
    except Exception:
        return False
    return config.get("ObjectLockConfiguration", {}).get("ObjectLockEnabled") == "Enabled"


def deliver(anchor: Anchor) -> AnchorDelivery:
    """Put the anchor where the database cannot change it. Every attempt is recorded."""
    body = json.dumps(document(anchor), indent=2).encode()
    scope = f"book-{anchor.book_id}" if anchor.book_id else "all"
    name = f"{anchor.kind}-{scope}-{anchor.as_of.astimezone(datetime.UTC):%Y%m%dT%H%M%SZ}.json"
    bucket = getattr(settings, "CREDIT_ANCHOR_BUCKET", "")
    if bucket:
        client = _bucket_client()
        if _object_lock_enabled(client, bucket):
            key = f"credit-anchors/{name}"
            retain = timezone.now() + datetime.timedelta(days=settings.CREDIT_ANCHOR_RETENTION_DAYS)
            try:
                client.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=body,
                    ContentType="application/json",
                    ObjectLockMode="COMPLIANCE",
                    ObjectLockRetainUntilDate=retain,
                )
            except Exception as exc:
                logger.exception("GAHK-konto: anchor %s could not be put in %s", anchor.pk, bucket)
                AnchorDelivery.objects.create(
                    anchor=anchor, way="s3", target=key, ok=False, detail=str(exc)[:2000]
                )
            else:
                return AnchorDelivery.objects.create(
                    anchor=anchor, way="s3", target=f"{bucket}/{key}", ok=True
                )
        else:
            logger.warning("GAHK-konto: bucket %s has no Object Lock; anchors go by e-mail", bucket)
    recipients = [a.strip() for a in getattr(settings, "CREDIT_ANCHOR_EMAILS", []) if a.strip()]
    if recipients:
        message_ = EmailMessage(
            subject=f"GAHK-konto: anker {anchor}",
            body=(
                "Et anker for GAHK-konto: en fingeraftryk af hele regnskabets tilstand. Gem mailen; "
                "den bruges til at kontrollere, at ingen har ændret i historikken.\n\n"
                f"Anker-hash: {bytes(anchor.anchor_hash).hex()}\n"
            ),
            to=recipients,
        )
        message_.attach(name, body, "application/json")
        with outbox.kind(outbox.Kind.NORMAL):
            message_.send()
        return AnchorDelivery.objects.create(anchor=anchor, way="mail", target=", ".join(recipients), ok=True)
    logger.error(
        "GAHK-konto: anchor %s has nowhere to go (CREDIT_ANCHOR_BUCKET / CREDIT_ANCHOR_EMAILS)", anchor.pk
    )
    return AnchorDelivery.objects.create(anchor=anchor, way="none", ok=False, detail="Ingen modtager sat op.")


def verify(anchor: Anchor) -> list[str]:
    """What is wrong with a stored anchor against today's database: its root, its link to the
    previous anchor, and its signature. Empty when it holds."""
    faults = []
    heads = heads_as_of(anchor.as_of, book=anchor.book)
    if chain.merkle_root((h.account_id, h.head) for h in heads) != bytes(anchor.root):
        faults.append(f"Anker {anchor.pk} ({anchor}): kontiene giver ikke længere den gemte rod.")
    body = message(anchor)
    if hashlib.sha256(body).digest() != bytes(anchor.anchor_hash):
        faults.append(f"Anker {anchor.pk} ({anchor}): hashen passer ikke med indholdet.")
    if anchor.key_id and not signing.verify_anchor(body, bytes(anchor.signature), anchor.key_id):
        faults.append(f"Anker {anchor.pk} ({anchor}): signaturen holder ikke.")
    return faults


def month_start(year: int, month: int) -> datetime.datetime:
    """The first moment of a month, local time."""
    return timezone.make_aware(datetime.datetime.combine(datetime.date(year, month, 1), datetime.time()))
