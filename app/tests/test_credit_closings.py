"""GAHK-konto's closings, anchors and signed monthly statements."""

import contextlib
import datetime
import io
import uuid
from collections.abc import Iterator

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from django.core import mail
from django.db import DatabaseError, connection, transaction
from django.urls import reverse
from django.utils import timezone

from credit import anchors, closing, ledger, signing, statements
from credit.ledger import LedgerError
from credit.models import Anchor, AnchorDelivery, Book, Closing, CreditAccount, Posting, Statement, Transfer
from tests.test_credit_ledger import world  # noqa: F401 — the shared fixture

pytestmark = pytest.mark.django_db


@contextlib.contextmanager
def at(moment: datetime.datetime) -> Iterator[None]:
    """Transfers made inside are dated `moment`: history, for periods that have ended."""
    field = Transfer._meta.get_field("created_at")
    old = field.default
    field.default = lambda: moment
    field.__dict__.pop("_get_default", None)
    try:
        yield
    finally:
        field.default = old
        field.__dict__.pop("_get_default", None)


@pytest.fixture(autouse=True)
def keys(settings, tmp_path, monkeypatch):  # noqa: ANN001, ANN201
    """A real signing setup: an Ed25519 anchor key whose public half is in the keys folder, and a PDF
    certificate. Anchors go by mail to the treasurers."""
    key = ed25519.Ed25519PrivateKey.generate()
    monkeypatch.setattr(signing, "KEYS_DIR", tmp_path)
    (tmp_path / "test.anchor.pub").write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    settings.CREDIT_ANCHOR_SIGNING_KEY = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    settings.CREDIT_ANCHOR_KEY_ID = "test"
    pdf_key, cert = signing.new_pdf_identity(days=5)
    settings.CREDIT_PDF_SIGNING_KEY, settings.CREDIT_PDF_SIGNING_CERT = pdf_key.decode(), cert.decode()
    settings.CREDIT_ANCHOR_BUCKET = ""
    settings.CREDIT_ANCHOR_EMAILS = ["kasserer@gahk.dk"]


def months_ago(n: int) -> tuple[datetime.date, datetime.datetime]:
    """The first day of the month `n` months back, and a moment in its middle."""
    first = timezone.localdate().replace(day=1)
    for _ in range(n):
        first = (first - datetime.timedelta(days=1)).replace(day=1)
    return first, timezone.make_aware(datetime.datetime.combine(first.replace(day=10), datetime.time(12)))


def token() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def history(world):  # noqa: ANN001, ANN201
    """The cellar's book made two months ago, with Bob paying in 100 kr and buying for 30 kr then."""
    first, middle = months_ago(2)
    Book.objects.filter(pk__in=[world.cellar.pk, world.house.pk]).update(
        created_at=timezone.make_aware(datetime.datetime.combine(first, datetime.time()))
    )
    CreditAccount.objects.update(
        created_at=timezone.make_aware(datetime.datetime.combine(first, datetime.time()))
    )
    world.cellar.refresh_from_db()
    with at(middle):
        ledger.send(
            actor=world.olga,
            sender=world.cellar_bank,
            receiver=world.bob_o,
            amount_ore=10000,
            dedupe_token=token(),
        )
        ledger.send(
            actor=world.bob, sender=world.bob_o, receiver=world.takings, amount_ore=3000, dedupe_token=token()
        )
    return world


# ---- closing ------------------------------------------------------------------------------------


def test_a_period_closes_only_once_it_has_ended(world) -> None:  # noqa: ANN001
    with pytest.raises(LedgerError, match="ikke slut"):
        closing.close(actor=world.olga, book=world.cellar, bank_statements={})


def test_the_bank_statement_must_match_the_ledger(history) -> None:  # noqa: ANN001
    bank = history.cellar_bank
    with pytest.raises(LedgerError, match="stemmer ikke"):
        closing.close(actor=history.olga, book=history.cellar, bank_statements={bank.pk: 9000})
    made = closing.close(actor=history.olga, book=history.cellar, bank_statements={bank.pk: 10000})
    balances = {b.account_id: b.balance for b in made.balances.all()}
    assert balances[history.bob_o.pk] == 7000
    assert balances[bank.pk] == -10000
    assert made.anchor.kind == Anchor.Kind.CLOSING and made.anchor.book == history.cellar
    assert not anchors.verify(made.anchor)


def test_only_managers_close(history) -> None:  # noqa: ANN001
    with pytest.raises(LedgerError, match="ansvarlige"):
        closing.close(actor=history.bob, book=history.cellar, bank_statements={history.cellar_bank.pk: 10000})


def test_nothing_is_recorded_into_a_closed_period(history) -> None:  # noqa: ANN001
    made = closing.close(
        actor=history.olga, book=history.cellar, bank_statements={history.cellar_bank.pk: 10000}
    )
    _, middle = months_ago(2)
    with pytest.raises(DatabaseError, match="closed"), transaction.atomic(), at(middle):
        ledger.send(
            actor=history.bob,
            sender=history.bob_o,
            receiver=history.takings,
            amount_ore=100,
            dedupe_token=token(),
        )
    # Today's transfers go into the open period as ever.
    ledger.send(
        actor=history.bob,
        sender=history.bob_o,
        receiver=history.takings,
        amount_ore=100,
        dedupe_token=token(),
    )
    start, _ = closing.next_period(history.cellar)
    assert start == made.period_end + datetime.timedelta(days=1)


def test_an_unsettled_mellemregning_needs_a_note(history) -> None:  # noqa: ANN001
    _, middle = months_ago(2)
    with at(middle):
        ledger.send(
            actor=history.bob,
            sender=history.bob_o,
            receiver=history.lucy_g,
            amount_ore=1000,
            dedupe_token=token(),
        )
    with pytest.raises(LedgerError, match="Ikke afregnet"):
        closing.close(
            actor=history.olga, book=history.cellar, bank_statements={history.cellar_bank.pk: 10000}
        )
    made = closing.close(
        actor=history.olga,
        book=history.cellar,
        bank_statements={history.cellar_bank.pk: 10000},
        note="I transit",
    )
    assert made.note == "I transit"


def test_closing_delivers_its_anchor_by_mail_when_no_locked_bucket(
    history,  # noqa: ANN001
    django_capture_on_commit_callbacks,  # noqa: ANN001
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        made = closing.close(
            actor=history.olga, book=history.cellar, bank_statements={history.cellar_bank.pk: 10000}
        )
    delivery = AnchorDelivery.objects.get(anchor=made.anchor)
    assert delivery.way == "mail" and delivery.ok
    assert mail.outbox[-1].to == ["kasserer@gahk.dk"]
    attached = mail.outbox[-1].attachments[0][1]
    assert bytes(made.anchor.anchor_hash).hex() in (
        attached.decode() if isinstance(attached, bytes) else attached
    )


# ---- anchors ------------------------------------------------------------------------------------


def test_an_anchor_is_signed_and_verifies(world) -> None:  # noqa: ANN001
    ledger.send(
        actor=world.olga, sender=world.cellar_bank, receiver=world.bob_o, amount_ore=500, dedupe_token=token()
    )
    anchor = anchors.make(Anchor.Kind.DAILY, timezone.now())
    assert anchor.key_id == "test" and bytes(anchor.signature)
    assert anchors.verify(anchor) == []
    assert anchors.make(Anchor.Kind.DAILY, anchor.as_of) == anchor  # once per moment
    later = anchors.make(Anchor.Kind.DAILY, timezone.now())
    assert later.previous == anchor


def test_a_rewritten_history_no_longer_matches_its_anchor(world) -> None:  # noqa: ANN001
    ledger.send(
        actor=world.olga, sender=world.cellar_bank, receiver=world.bob_o, amount_ore=500, dedupe_token=token()
    )
    anchor = anchors.make(Anchor.Kind.DAILY, timezone.now())
    posting = Posting.objects.get(account=world.bob_o)
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute("ALTER TABLE credit_posting DISABLE TRIGGER credit_posting_append_only")
        cursor.execute("UPDATE credit_posting SET row_hash = %s WHERE id = %s", [b"y" * 32, posting.pk])
        cursor.execute("ALTER TABLE credit_posting ENABLE TRIGGER credit_posting_append_only")
    assert any("rod" in fault for fault in anchors.verify(anchor))


def test_an_anchor_goes_to_a_locked_bucket_when_there_is_one(world, settings, monkeypatch) -> None:  # noqa: ANN001
    put = []

    class Client:
        def get_object_lock_configuration(self, Bucket: str) -> dict:
            return {"ObjectLockConfiguration": {"ObjectLockEnabled": "Enabled"}}

        def put_object(self, **kwargs: object) -> None:
            put.append(kwargs)

    monkeypatch.setattr(anchors, "_bucket_client", Client)
    settings.CREDIT_ANCHOR_BUCKET = "anchors"
    delivery = anchors.deliver(anchors.make(Anchor.Kind.DAILY, timezone.now()))
    assert delivery.way == "s3" and delivery.ok
    assert put[0]["ObjectLockMode"] == "COMPLIANCE" and put[0]["Bucket"] == "anchors"


def test_without_object_lock_the_anchor_goes_by_mail(world, settings, monkeypatch) -> None:  # noqa: ANN001
    class Client:
        def get_object_lock_configuration(self, Bucket: str) -> dict:
            raise RuntimeError("ObjectLockConfigurationNotFoundError")

    monkeypatch.setattr(anchors, "_bucket_client", Client)
    settings.CREDIT_ANCHOR_BUCKET = "media"
    assert anchors.deliver(anchors.make(Anchor.Kind.DAILY, timezone.now())).way == "mail"


# ---- statements ---------------------------------------------------------------------------------


def _signed(pdf: bytes) -> bool:
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation import validate_pdf_signature

    signatures = PdfFileReader(io.BytesIO(pdf)).embedded_signatures
    return len(signatures) == 1 and validate_pdf_signature(signatures[0]).intact


def test_last_months_statements_go_out_once_signed(history) -> None:  # noqa: ANN001
    first, _ = months_ago(2)
    mail.outbox.clear()
    assert statements.send_month(first.year, first.month) >= 1
    bobs = [m for m in mail.outbox if m.to == ["bob@gahk.dk"]]
    assert len(bobs) == 1
    _name, pdf, kind = bobs[0].attachments[0]
    assert kind == "application/pdf" and _signed(pdf)
    sent = Statement.objects.get(holder=history.bob_o.holder, year=first.year, month=first.month)
    assert sent.anchor.kind == Anchor.Kind.MONTH
    assert statements.send_month(first.year, first.month) == 0


def test_a_statement_carries_last_months_number(history) -> None:  # noqa: ANN001
    first, _ = months_ago(2)
    nxt, _ = months_ago(1)
    this = statements.account_month(history.bob_o, first.year, first.month)
    then = statements.account_month(history.bob_o, nxt.year, nxt.month)
    assert (this.opening, this.closing) == (0, 7000)
    assert then.previous_head == this.head and then.opening == this.closing


def test_the_statement_page_gives_the_holder_a_signed_pdf(client, history) -> None:  # noqa: ANN001
    first, _ = months_ago(2)
    url = reverse("credit:statement", args=[history.bob_o.pk, first.year, first.month])
    client.force_login(history.lucy)
    assert client.get(url).status_code == 404
    client.force_login(history.bob)
    response = client.get(url)
    assert response.status_code == 200 and _signed(response.content)
    this_month = timezone.localdate()
    assert (
        client.get(
            reverse("credit:statement", args=[history.bob_o.pk, this_month.year, this_month.month])
        ).status_code
        == 404
    )


def test_the_book_page_closes_a_period(client, history) -> None:  # noqa: ANN001
    client.force_login(history.olga)
    page = client.get(reverse("credit:book", args=[history.cellar.pk]))
    assert "Luk perioden" in page.content.decode()
    client.post(
        reverse("credit:close_period", args=[history.cellar.pk]), {f"bank_{history.cellar_bank.pk}": "100,00"}
    )
    assert Closing.objects.filter(book=history.cellar).exists()
