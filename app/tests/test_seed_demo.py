"""Smoke test for the seed_demo dev fixture command: it should run, be idempotent, and produce
data that lights up the app's real queries (active period, roles, derived ølkælder balances)."""

import pytest
from django.core.management import call_command

from den_hurtige.models import QuickPost
from oelkaelder.models import Shopper
from residents.models import Resident, active_period
from residents.permissions import real_roles


@pytest.mark.django_db
def test_seed_demo_populates_and_is_idempotent() -> None:
    # --force because Django's test runner forces DEBUG=False, which the safety guard blocks.
    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)
    first_count = Resident.objects.count()
    assert first_count == 12

    # Re-running with --fresh must not duplicate or raise (idempotent).
    # --force because Django's test runner forces DEBUG=False, which the safety guard blocks.
    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)
    assert Resident.objects.count() == first_count

    # active_period follows the seeded current-month residencies.
    year, month = active_period()
    from django.utils import timezone

    today = timezone.localdate()
    assert (year, month) == (today.year, today.month)

    # Documented logins exist with the expected roles and password.
    formand = Resident.objects.get(email="formand@gahk.dk")
    assert "administrator" in real_roles(formand)
    assert formand.check_password("demo1234")

    # Derived ølkælder balance is computable (no crash, integer øre).
    shopper = Shopper.objects.first()
    assert isinstance(shopper.balance_ore, int)

    from arkiv.models import ArchiveFile, ArchiveFolder

    shared = ArchiveFolder.objects.get(parent=None, name="Fælles dokumenter")
    billeder = ArchiveFolder.objects.get(parent=shared, name="Billeder")
    summer = ArchiveFolder.objects.get(parent=billeder, name="Sommerfest 2026")
    assert ArchiveFile.objects.filter(folder=summer, name="gruppebillede.jpg").exists()
    assert not ArchiveFolder.objects.filter(parent=None, name="Billeder").exists()


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
    from notifications import services
    from notifications.categories import Importance
    from notifications.models import Notification

    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)
    beboer = Resident.objects.get(email="beboer@gahk.dk")
    mine = Notification.objects.filter(recipient=beboer)

    assert services.unseen_count(beboer.pk) == 4
    assert mine.visible().filter(importance=Importance.KRITISK).exists()
    assert mine.visible().filter(seen_at__isnull=False).count() == 2
    assert any(len(n.actors) == 2 for n in mine)  # "Mette og Jonas svarede"
    assert services.purge_expired() == 5  # one long-expired row per documented login
    assert beboer.notification_preferences.filter(push=False).exists()

    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)  # still idempotent
    assert Notification.objects.filter(recipient__email="beboer@gahk.dk").count() == 7


@pytest.mark.django_db
def test_seed_demo_gives_the_kitchen_every_zone_verdict_and_plan_state() -> None:
    """kitchen.demo promises a picture: every zone today, every combination of the overview's two
    verdicts, statements that add up, and the plan's awkward states. Each is checked here, so a
    change to the rules that quietly empties one of them fails rather than leaving a blank cell."""
    import datetime

    from django.test import Client
    from django.urls import reverse
    from django.utils import timezone

    from kitchen import demo, services
    from kitchen.models import KitchenAssignment, KitchenPointEntry, KitchenShift
    from residents.models import Residency

    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)
    now = timezone.now()

    # Statements: an unbroken chain of balances, in time order, ending where the profile said.
    for resident in Resident.objects.all():
        entries = list(resident.kitchen_entries.order_by("id"))
        balance = 0
        for entry in entries:
            assert (entry.balance_before, entry.balance_after) == (balance, balance + entry.amount)
            balance = entry.balance_after
        assert [e.created_at for e in entries] == sorted(e.created_at for e in entries)
    balances = KitchenPointEntry.balances()
    for email, (_profile, balance) in demo.LOGINS.items():
        assert balances[Resident.objects.get(email=email).pk] == balance, email
    kinds = set(KitchenPointEntry.objects.values_list("kind", flat=True))
    assert kinds == {"shift", "monthly", "absence", "trade", "manual"}

    # Every zone now, and every combination of "fine in 30 days" and "fine at the end".
    year, month = active_period()
    current = list(Resident.objects.filter(residencies__year=year, residencies__month=month))
    forecasts = services.forecasts(current)
    zones, verdicts = set(), set()
    for resident in current:
        steps, balance = forecasts[resident.pk], balances.get(resident.pk, 0)
        soon = services.balance_at(balance, steps, now + datetime.timedelta(days=services.OUTLOOK_DAYS))
        end = steps[-1].balance if steps else balance
        zones.add(services.zone(balance))
        verdicts.add((soon >= 0, end >= 0))
    assert zones == {"good", "warn", "neg", "alarm"}
    assert verdicts == {(True, True), (True, False), (False, True), (False, False)}

    # The plan: history behind, the calendar ahead, and the states that take effort to make by hand.
    assert KitchenShift.objects.filter(date=services.calendar_horizon()).exists()
    assert KitchenShift.objects.filter(ends_at__lte=now, completed_at__isnull=False).count() > 100
    assert services.unmanned_shifts(datetime.timedelta(hours=24)).exists()
    assert (
        KitchenShift.objects.filter(starts_at__gt=now, is_disabled=True).exclude(disabled_reason="").exists()
    )
    assert KitchenShift.objects.filter(starts_at__gt=now, bonus_points__gt=0).exists()
    assert KitchenShift.objects.filter(starts_at__gt=now, kind="special").exists()
    ahead = KitchenAssignment.objects.filter(shift__starts_at__gt=now)
    assert ahead.filter(spots=2).exists(), "nobody holds both spots of an evening"
    assert ahead.filter(for_sale=True, sale_bonus__gt=0).exists()
    beboer = Resident.objects.get(email="beboer@gahk.dk")
    assert ahead.filter(resident=beboer, for_sale=True).exists()
    assert any(services.can_unenroll(a) for a in ahead.filter(resident=beboer).select_related("shift")), (
        "beboer@ has no sign-up inside the regret window"
    )
    assert ahead.filter(self_enrolled_at__gt=now - datetime.timedelta(minutes=10)).count() == 1
    assert KitchenAssignment.objects.filter(is_absent=True).count() == 2
    assert Residency.objects.filter(resident__email="koekken@gahk.dk", year=year, month=month).exists()

    client = Client()
    for email, status in (("koekken@gahk.dk", 200), ("inspektion@gahk.dk", 200), ("beboer@gahk.dk", 403)):
        client.force_login(Resident.objects.get(email=email))
        assert client.get(reverse("kitchen:dashboard")).status_code == status, email

    entries = KitchenPointEntry.objects.count()
    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)
    assert KitchenPointEntry.objects.count() == entries
