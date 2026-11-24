"""Smoke test for the seed_demo dev fixture command: it should run, be idempotent, and produce
data that lights up the app's real queries (active period, roles, derived ølkælder balances)."""

from typing import Any

import pytest
from django.core.management import call_command

from access.api import real_roles
from audit import trail
from audit.models import AuditEvent
from den_hurtige.models import QuickPost
from oelkaelder.models import Shopper
from residents.models import Account, active_period


@pytest.mark.django_db
def test_seed_demo_populates_and_is_idempotent() -> None:
    # --force because Django's test runner forces DEBUG=False, which the safety guard blocks.
    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)
    # 12 seeded residents, one of whom then moves out and is replaced by a newly created one, so the
    # current month stays full (see _seed_residencies). That keeps a leaver for the regnskab page
    # without leaving the alumneliste a person short.
    first_count = Account.objects.count()
    assert first_count == 13
    first_trail = AuditEvent.objects.count()

    # Re-running with --fresh must not duplicate or raise (idempotent).
    # --force because Django's test runner forces DEBUG=False, which the safety guard blocks.
    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)
    assert Account.objects.count() == first_count
    # The append-only trail too: emptied and written again, not doubled.
    assert AuditEvent.objects.count() == first_trail

    # active_period follows the seeded current-month residencies.
    year, month = active_period()
    from django.utils import timezone

    today = timezone.localdate()
    assert (year, month) == (today.year, today.month)

    # Documented logins exist with the expected roles and password.
    formand = Account.objects.get(email="formand@gahk.dk")
    assert "administrator" in real_roles(formand)
    assert formand.check_password("demo1234")

    # Two alumni in the current month lack an e-conomic kontonr., for trying the regnskab page's
    # "Mangler e-conomic kontonr." list; everyone else has one.
    missing = Account.objects.filter(economic_account_number__isnull=True)
    assert missing.count() == 2
    assert all(r.residencies.filter(year=year, month=month).exists() for r in missing)

    # Derived ølkælder balance is computable (no crash, integer øre).
    shopper = Shopper.objects.first()
    assert isinstance(shopper.balance_ore, int)

    # The leaver/replacement pair: exactly one resident moved out this month and exactly one new
    # one arrived, so the regnskab page has a fraflytter while the alumneliste stays full.
    from datetime import date

    from residents.models import Residency

    leavers = [r for r in Account.objects.all() if r.move_out_date is not None]
    assert len(leavers) == 1
    leaver = leavers[0]
    assert not leaver.residencies.filter(year=year, month=month).exists(), "a leaver is off this month"
    assert leaver.residencies.exists(), "a leaver was on earlier months, so there is history to settle"

    this_month = Residency.objects.filter(year=year, month=month)
    # Everyone except the one leaver is on this month's list: a full alumneliste, not one short.
    assert this_month.count() == Account.objects.count() - 1
    assert not this_month.filter(resident=leaver.house).exists()
    room_ids = list(this_month.values_list("room_id", flat=True))
    assert len(room_ids) == len(set(room_ids)), "one resident per room this month"
    newcomer = Account.objects.get(person__resident__move_in_date=date(year, month, 1))
    assert this_month.filter(resident=newcomer.house).exists()


@pytest.mark.django_db
def test_seed_demo_fills_the_board_including_the_two_awkward_cases() -> None:
    """The board needs demo content, but two specific rows are what make it useful locally: a pinned
    post (so the pinned-first layout and the 📌 marker are visible without anyone pinning something)
    and one several years old (the board keeps its archive, so the demo should show a genuinely old
    opslag still sitting there and paginating)."""
    from datetime import timedelta

    from django.utils import timezone

    from opslagstavle.models import Notice

    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)

    assert Notice.objects.count() >= 6
    assert Notice.objects.pinned().count() == 1
    assert Notice.objects.pinned().first().pinned_by is not None, "a pin with no attributor"
    # An old post is kept on purpose: the board has no retention, so the demo should show that a
    # years-old opslag is still there rather than that something is about to remove it.
    old = timezone.now() - timedelta(days=365 * 2)
    assert Notice.objects.filter(created_at__lt=old).exists(), "no archive post in the demo board"
    # Every category represented, so the filter chips are all reachable in the demo.
    assert len({n.category for n in Notice.objects.all()}) >= 5
    # Posts embedding a demo album and a demo film, so their widgets show without anyone posting one.
    assert Notice.objects.filter(body__contains="](album:").exists()
    assert Notice.objects.filter(body__contains="](gahkflix:").exists()
    assert not hasattr(Notice.objects, "expired"), (
        "retention was removed from opslagstavlen; a reinstated expired() needs the spec updated too"
    )


@pytest.mark.django_db
def test_seed_demo_leaves_one_tombstone_with_its_replies_intact() -> None:
    """Awkward to reach by hand — deleting a freshly posted message takes the silent path — so the
    seeder makes one, with replies."""
    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)

    tombstones = QuickPost.objects.filter(deleted_at__isnull=False)

    assert tombstones.count() == 1
    tombstone = tombstones.get()
    assert tombstone.content == "", "a tombstone holds no content"
    assert tombstone.comments.exists(), "the replies outlive the message"


@pytest.mark.django_db
def test_seed_demo_gives_the_logins_notifications_in_every_state() -> None:
    """The bell and the notifications page need something to show on a fresh checkout."""
    from notifications import categories, services
    from notifications.categories import Importance
    from notifications.models import Notification

    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)
    beboer = Account.objects.get(email="beboer@gahk.dk")
    # Leave out what GAHKFlix tells beboer@ about their own uploads (gahkflix.demo): this test is about
    # the rows notifications.demo seeds.
    uploads = Notification.objects.filter(recipient=beboer, category=categories.GAHKFLIX_UPLOADS)
    mine = Notification.objects.filter(recipient=beboer).exclude(pk__in=uploads)

    assert uploads.exists()
    assert services.unseen_count(beboer.pk) - uploads.visible().unseen().count() == 4
    assert mine.visible().filter(importance=Importance.KRITISK).exists()
    assert mine.visible().filter(seen_at__isnull=False).count() == 2
    assert any(len(n.actors) == 2 for n in mine)  # "Mette og Jonas svarede"
    assert services.purge_expired() == 5  # one long-expired row per documented login
    assert beboer.notification_preferences.filter(push=False).exists()

    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)  # still idempotent
    again = Notification.objects.filter(recipient__email="beboer@gahk.dk")
    assert again.exclude(category=categories.GAHKFLIX_UPLOADS).count() == 7


@pytest.mark.django_db
def test_seed_demo_writes_weeks_of_audit_history(settings: Any) -> None:  # noqa: ANN401
    """The rest of the seed is unaudited (loading demo data is nobody's action), so without its own
    step the audit page would be empty on a fresh checkout. The step writes the weeks before today:
    every kind of line the page shows, by different people from different addresses, with before and
    after values that are true because the history ends at the seeded data."""
    from datetime import timedelta

    from django.test import Client
    from django.utils import timezone

    from access.models import Grant
    from residents.models import Role

    settings.AUDIT_FALLBACK = True  # as outside the tests, so the shell fix is recorded (as System)
    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)
    events = AuditEvent.objects.all()

    assert {
        "auth.login",
        "auth.login.failed",
        "auth.logout",
        "auth.password.changed",
        "auth.password_reset.requested",
        "auth.password_reset.completed",
        "auth.access.denied",
        "residents.role.added",
        "residents.role.removed",
        "residents.admin_access.approved",
        "residents.admin_access.denied",
        "residents.resident.updated",
        "residents.profile.updated",
        "residents.month_list.saved",
        "residents.month_list.added",
        "residents.preview.set",
        "residents.preview.cleared",
        "rooms.round.ended",
        "regnskab.account_number.updated",
    } <= set(events.values_list("action", flat=True))
    failed = events.filter(action="auth.login.failed")
    assert {e.details["reason"] for e in failed} == {"wrong password", "no account"}
    typed = failed.get(details__email_hash=trail.address_hash("formånd@gahk.dk"))
    assert typed.details["email_domain"] == "gahk.dk", "the typed å, kept only as domain and hash"
    # "Uden handling" both ways: the Django admin (whoever was logged in) and a shell (nobody).
    assert events.filter(unclaimed=True, source="web", actor_name="Admin Istrator").exists()
    assert events.filter(unclaimed=True, source="system", actor=None).exists()

    # Spread out like a real trail: people, addresses (an IPv6 one too), weeks; never in the future.
    now = timezone.now()
    assert events.exclude(actor=None).values("actor").distinct().count() >= 8
    addresses = set(events.exclude(ip=None).values_list("ip", flat=True))
    assert len(addresses) >= 4 and any(":" in ip for ip in addresses)
    assert events.order_by("at").first().at < now - timedelta(days=21)
    assert not events.filter(at__gt=now).exists()
    # The page lists by id, so the history was written in time order.
    times = list(events.order_by("id").values_list("at", flat=True))
    assert times == sorted(times)

    # The history ends at the seeded data: the two administrators it approved and refused lost the
    # role again, and the passwords it changed are still the documented one.
    assert set(
        Grant.objects.active().filter(role=Role.ADMINISTRATOR).values_list("account__email", flat=True)
    ) == {"formand@gahk.dk"}
    for email in ("ak@gahk.dk", "oel@gahk.dk"):
        assert Account.objects.get(email=email).check_password("demo1234")

    client = Client()
    client.force_login(Account.objects.get(email="formand@gahk.dk"))
    page = client.get("/admin/audit").content.decode()
    assert "Ældre" in page, "more than one page of history"
    assert "mislykket login" in page
    assert "Uden handling" in client.get("/admin/audit?unclaimed=1").content.decode()


@pytest.mark.django_db
def test_seed_demo_fills_the_outbox_with_every_kind_and_status(settings: object) -> None:
    """The mail page needs a mail in every state, and seeding must send none of them (core.demo)."""
    from datetime import timedelta

    from django.core import mail as django_mail
    from django.utils import timezone

    from core import mail as outbox
    from core.models import OutgoingMail

    Status, Kind = OutgoingMail.Status, OutgoingMail.Kind
    # Low enough for the demo's dozen residents' ølkælder overview to fill the window (dev's is 10).
    settings.MAIL_RATE_LIMIT = 3  # type: ignore[attr-defined]

    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)
    now = timezone.now()
    mails = OutgoingMail.objects.all()

    # Nothing went out: conftest delivers any mail whose delivery is started, through locmem.
    assert django_mail.outbox == []
    assert {m.status for m in mails} == set(Status.values)
    assert {m.kind for m in mails} == set(Kind.values)
    residents = set(Account.objects.values_list("email", flat=True))
    assert all(m.recipients in residents for m in mails.exclude(subject__contains="henvendelse"))

    # The bulk send under way: the window full, three overviews held back by it.
    assert outbox.quota_wait(now) is not None
    assert outbox._due(now, [Kind.BULK]).count() == 3
    # Waiting again after a failed try: a full mailbox (bulk) and a greylisted reset (interactive).
    retrying = mails.filter(status=Status.QUEUED).exclude(error="")
    assert {m.kind for m in retrying} == {Kind.BULK, Kind.INTERACTIVE}
    assert all(m.next_attempt_at > now and m.payload for m in retrying)

    # Stored as the deliverer leaves them: content gone once sent or expired, kept while it may be resent.
    assert not mails.filter(status__in=[Status.SENT, Status.EXPIRED]).exclude(payload={}).exists()
    assert all(m.error.startswith(outbox.EXPIRED_ERROR) for m in mails.filter(status=Status.EXPIRED))
    assert mails.get(status=Status.UNKNOWN).error == outbox.LOST_ERROR
    failed = list(mails.filter(status=Status.FAILED))
    assert any(outbox.can_send_again(m, now) for m in failed), "a failure to try Send igen on"
    assert any(not m.payload for m in failed), "an old failure the purge has emptied"

    # Spread over time, so each period filter shows a different set.
    ages = [now - m.created_at for m in mails]
    assert min(ages) < timedelta(minutes=5)
    assert any(timedelta(days=1) < age < timedelta(days=7) for age in ages)
    assert any(timedelta(days=7) < age < outbox.RETENTION for age in ages)


@pytest.mark.django_db
def test_seed_demo_leaves_no_mail_waiting_for_a_backend_that_really_sends(settings: object) -> None:
    """A queued mail is sent by deliver_mail's next run: with SMTP configured, to the demo's gahk.dk addresses."""
    from core.models import OutgoingMail

    settings.MAIL_DELIVERY_BACKEND = "django.core.mail.backends.smtp.EmailBackend"  # type: ignore[attr-defined]
    settings.MAIL_RATE_LIMIT = 3  # type: ignore[attr-defined]

    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)

    assert not OutgoingMail.objects.filter(status=OutgoingMail.Status.QUEUED).exists()
    assert OutgoingMail.objects.filter(status=OutgoingMail.Status.FAILED).exists()


@pytest.mark.django_db
def test_seed_demo_gives_gahkflix_premieres_partial_dates_and_drafts_in_every_state() -> None:
    """What the premieres and the drafts look like is hard to reach by hand: a premiere only comes out
    at its minute, and a draft is processing for seconds. The demo holds each state still."""
    from datetime import timedelta

    from django.utils import timezone

    from gahkflix import services
    from gahkflix.dating import DatePrecision, DateSource
    from gahkflix.models import MAX_DRAFTS, FileState, PremiereBlur, Video, VideoEvent

    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)
    now = timezone.now()

    # `Kommer snart`, soonest first: the series' next episode, then beboer@'s own film.
    upcoming = list(Video.objects.upcoming().order_by("premiere_at"))
    assert [v.title for v in upcoming] == ["Fællesspisning", "Vinterbadning"]
    assert upcoming[0].series is not None and upcoming[0].episode == 3
    assert {v.premiere_blur for v in upcoming} == {PremiereBlur.LIGHT, PremiereBlur.HEAVY}
    assert upcoming[1].uploaded_by.email == "beboer@gahk.dk"
    assert all(v.announced_at is None for v in upcoming), "an upcoming premiere is announced at its minute"

    # Out a few hours ago, published before its premiere and announced at it; still takeable back.
    concert = Video.objects.get(title="Kælderkoncert")
    assert concert.is_released and concert.premiere_at < now
    assert concert.published_at < concert.premiere_at == concert.announced_at
    assert concert.can_be_taken_back
    assert concert.events.filter(kind=VideoEvent.Kind.PREMIERED).exists()

    # The three kinds of release date.
    precisions = set(Video.objects.released().values_list("release_precision", flat=True))
    assert precisions == {DatePrecision.DAY, DatePrecision.MONTH, DatePrecision.YEAR}

    # beboer@'s drafts, one of each kind, with room left to upload more.
    beboer = Account.objects.get(email="beboer@gahk.dk")
    drafts = Video.objects.drafts().filter(uploaded_by=beboer)
    assert 1 <= MAX_DRAFTS - drafts.count() < MAX_DRAFTS
    states = {draft.title: draft.files.get().state for draft in drafts}
    assert states["Revyprøve"] == FileState.PROCESSING
    assert states["Gangmøde"] == FileState.QUEUED
    assert services.queue_ahead(drafts.get(title="Gangmøde").files.get()) == 1
    assert states["Lydoptagelse"] == FileState.FAILED
    summer = drafts.get(title="Sommerfest")
    assert summer.released_on is None, "the date is the uploader's to fill in"
    assert summer.file.date_guess_source == DateSource.NAME and summer.file.date_guess
    assert services.missing_for_publishing(summer) == ["Udfyld udgivelsesdatoen – mindst året."]
    party = drafts.get(title="Gangfest")
    assert party.premiere_at > now and not services.missing_for_publishing(party)
    trip = drafts.get(title="Rusturen")
    assert trip.draft_since is not None and trip.draft_since > trip.created_at + timedelta(days=60)
    assert trip.events.filter(kind=VideoEvent.Kind.UNPUBLISHED).exists()
