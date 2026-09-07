"""Populate the database with realistic **fake** data for local development and demos.

Why a command (not a JSON fixture): fixtures rot on every schema change, can't produce hashed
passwords, and are painful to scale. This generator stays close to the models, is deterministic
(fixed seed → identical data for everyone), and is safe to re-run.

Usage:
    task seed                      # wipe demo tables + regenerate (dev only)
    python manage.py seed_demo --fresh --residents 40

Safety: refuses to run when DEBUG is off unless --force is given, so it can never wipe a prod DB
by accident. --fresh deletes the demo-managed tables (everything below) inside one transaction.

Deterministic logins (all password `demo1234`):
    admin@gahk.dk      superuser (all access)
    formand@gahk.dk    administrator role (all internal tools)
    ak@gahk.dk         AK role
    oel@gahk.dk        Ølkælder role
    beboer@gahk.dk     plain resident (no roles)
"""

import argparse
import random
import unicodedata
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import models, transaction
from django.utils import timezone

from admissions.models import Application
from ak.models import AkEntry, AkMonthlyCharge
from ak.services import apply_monthly_charge
from arkiv.demo import seed as seed_arkiv
from arkiv.models import ArchiveFile, ArchiveFolder

# CmsEvent, never Event: the internal events app has one of the same name, and both are seeded
# here. See events.models on the three-way collision.
from cms.models import Event as CmsEvent
from cms.models import NewsItem, Page, PylonEvent
from core.models import Cleaning, Room, Workgroup
from events.demo import seed as seed_events
from events.models import CalendarFeedToken, Event, EventInvite, Rsvp
from oelkaelder.models import (
    Deposit,
    Product,
    PurchaseShare,
    Shopper,
    Transaction,
    TransactionItem,
)
from opslagstavle.demo import seed as seed_opslagstavle
from opslagstavle.models import Notice, NoticeComment, NoticeReaction
from residents.models import WORKGROUP_ROLE, Residency, Resident, Role, RoleAssignment
from rooms.models import (
    KvotientApplication,
    KvotientPriority,
    RoomCondition,
    RoomConditionScore,
    RoomCriterion,
    RoomOffer,
)
from stats.models import DailyVisitCount, VisitTally

from .seed_rooms import build_rooms

DEMO_PASSWORD = "demo1234"  # noqa: S105 — public demo credential, not a secret
SEED = 1908  # the year GAHK was founded — any fixed value works, we just want reproducibility

# Non-privileged chore groups (no site role) mixed in with the privileged ones from WORKGROUP_ROLE.
EXTRA_WORKGROUPS = ["Haven", "Vinklubben", "Festudvalget", "Bladet", "IT / Netværk"]
CLEANING_GROUPS = ["Køkken", "Bad 1. sal", "Bad 2. sal", "Trappe", "Kælder", "Fællesrum"]
# (code, name, options, description) — real rows from intern_room_criteria covering all three scale
# shapes, so the værelsestjek score explainer is visible in dev. See RoomCriterion.score_values.
ROOM_CRITERIA = [
    (
        "walls",
        "Vægge",
        5,
        "Maling.\n1: Nymalet/pæn stand,\n2: Få pletter eller misfarvninger,\n"
        "3: Større pletter og misfarvninger,\n4: Revner,\n5: Huller el. svamp",
    ),
    (
        "floor",
        "Gulve",
        5,
        "Lakering og slibning.\n1: God/pæn stand,\n2: Få ridser og pletter,\n"
        "3: Større ridser og pletter,\n4: Mellem de to,\n5: Huller i gulvet eller skibslak",
    ),
    (
        "windows",
        "Vinduer",
        3,
        "0: Virker/fin stand,\n1: Tætningslister eller hasper mangler,\n2: Virker ikke",
    ),
    ("curtains", "Gardiner", 2, "0: Er der\n1: Mangler"),
]

# Deletion order: children before parents so PROTECT FKs don't block the wipe.
WIPE_ORDER: list[type[models.Model]] = [
    Rsvp,
    EventInvite,
    CalendarFeedToken,
    Event,
    NoticeReaction,
    NoticeComment,
    Notice,
    PurchaseShare,
    TransactionItem,
    Transaction,
    Deposit,
    Shopper,
    Product,
    RoomConditionScore,
    RoomCondition,
    RoomCriterion,
    KvotientPriority,
    KvotientApplication,
    RoomOffer,
    AkEntry,
    Application,
    RoleAssignment,
    Residency,
    Page,
    NewsItem,
    CmsEvent,
    PylonEvent,
    DailyVisitCount,
    VisitTally,
    Resident,
    # Before Workgroup: ArchiveFolder references it with PROTECT (a SET_NULL would silently turn a
    # gated folder into a world-readable one), so the archive has to go first or --fresh cannot run.
    ArchiveFile,
    ArchiveFolder,
    Room,
    Workgroup,
    Cleaning,
]


def _ascii_slug(text: str) -> str:
    """Fold Danish letters to ASCII for email local-parts (Bjørn -> bjorn)."""
    swaps = {"æ": "ae", "ø": "oe", "å": "aa", "Æ": "ae", "Ø": "oe", "Å": "aa"}
    text = "".join(swaps.get(c, c) for c in text)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return "".join(c for c in text.lower() if c.isalnum())


class Command(BaseCommand):
    help = "Seed the DB with realistic fake data for dev/demo (deterministic, idempotent)."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--fresh", action="store_true", help="Delete existing demo data first.")
        parser.add_argument("--residents", type=int, default=40, help="How many residents to create.")
        parser.add_argument("--force", action="store_true", help="Allow running when DEBUG is off.")

    def handle(self, *args, **opts) -> None:  # noqa: ANN002, ANN003
        if not settings.DEBUG and not opts["force"]:
            raise CommandError("Refusing to seed with DEBUG=off. Pass --force if you really mean it.")

        try:
            from faker import Faker
        except ImportError as exc:  # dev-only dependency
            raise CommandError("Faker is not installed. Run `uv sync` (it is a dev dependency).") from exc

        self.fake = Faker("da_DK")
        Faker.seed(SEED)
        self.rng = random.Random(SEED)  # noqa: S311 — demo data only, not security-sensitive
        self.now = timezone.now()
        today = timezone.localdate()
        self.year, self.month = today.year, today.month

        with transaction.atomic():
            if opts["fresh"]:
                self._wipe()
            rooms = self._seed_rooms()
            workgroups, cleanings = self._seed_lookups()
            residents = self._seed_residents(opts["residents"])
            self._seed_residencies(residents, rooms, workgroups, cleanings)
            self._seed_roles(residents)
            self._seed_ak(residents)
            self._seed_oelkaelder(residents)
            self._seed_admissions(residents)
            self._seed_cms()
            self._seed_room_conditions(rooms, residents)
            self._seed_kvotient(residents, rooms)
            self._seed_stats()
            seed_opslagstavle(residents, self.now, self.rng)
            seed_events(residents, self.now, self.rng)
            seed_arkiv(residents, self.now, self.rng)

        self._report(residents)

    # ---------------------------------------------------------------- wipe
    def _wipe(self) -> None:
        for model in WIPE_ORDER:
            model._default_manager.all().delete()
        self.stdout.write("  wiped existing demo data")

    # ------------------------------------------------------------- lookups
    def _seed_rooms(self) -> list[Room]:
        for r in build_rooms():
            Room.objects.update_or_create(legacy_index=r["legacy_index"], defaults=r)
        return list(Room.objects.all())

    def _seed_lookups(self) -> tuple[list[Workgroup], list[Cleaning]]:
        workgroups = []
        for name in list(WORKGROUP_ROLE.keys()) + EXTRA_WORKGROUPS:
            wg, _ = Workgroup.objects.get_or_create(name=name)
            workgroups.append(wg)
        cleanings = [Cleaning.objects.get_or_create(name=n)[0] for n in CLEANING_GROUPS]
        return workgroups, cleanings

    # ----------------------------------------------------------- residents
    def _seed_residents(self, count: int) -> list[Resident]:
        residents = []
        seen_emails = set()

        # Fixed, documented accounts first so devs always have known logins.
        fixtures = [
            ("admin@gahk.dk", "Admin", "Istrator", {"is_superuser": True, "is_staff": True}),
            ("formand@gahk.dk", "Frederik", "Formand", {}),
            ("ak@gahk.dk", "Astrid", "Krydsen", {}),
            ("oel@gahk.dk", "Ole", "Ølmand", {}),
            ("regnskab@gahk.dk", "Regina", "Regnskab", {}),
            ("beboer@gahk.dk", "Bente", "Beboer", {}),
        ]
        for email, first, last, extra in fixtures:
            r = self._make_resident(email, first, last, **extra)
            residents.append(r)
            seen_emails.add(email)

        for _ in range(max(0, count - len(fixtures))):
            first = self.fake.first_name()
            last = self.fake.last_name()
            base = f"{_ascii_slug(first)}.{_ascii_slug(last)}"
            email = f"{base}@gahk.dk"
            n = 1
            while email in seen_emails:
                n += 1
                email = f"{base}{n}@gahk.dk"
            seen_emails.add(email)
            residents.append(self._make_resident(email, first, last))

        # Lineage: give most residents a sponsor (fylgje) picked from someone created earlier.
        for i, r in enumerate(residents):
            if i > 4 and self.rng.random() < 0.8:
                r.sponsor = residents[self.rng.randint(0, i - 1)]
                r.save(update_fields=["sponsor"])
        return residents

    def _make_resident(self, email: str, first: str, last: str, **extra) -> Resident:  # noqa: ANN003
        r = Resident(
            email=email,
            first_name=first,
            last_name=last,
            phone=self.fake.phone_number(),
            birthday=self.fake.date_between(start_date="-30y", end_date="-19y"),
            move_in_date=self.fake.date_between(start_date="-6y", end_date="-3m"),
            study=self.rng.choice(
                [
                    "Medicin, KU",
                    "Statskundskab, KU",
                    "Software, DTU",
                    "Jura, KU",
                    "Fysik, KU",
                    "Arkitektur, KADK",
                    "Økonomi, CBS",
                    "Matematik, DTU",
                ]
            ),
            **extra,
        )
        r.set_password(DEMO_PASSWORD)
        r.save()
        return r

    # --------------------------------------------------------- residencies
    def _iter_recent_months(self, n: int = 3) -> list[tuple[int, int]]:
        """The current month and the n-1 months before it, as (year, month) tuples (oldest first)."""
        y, m = self.year, self.month
        months = []
        for _ in range(n):
            months.append((y, m))
            m -= 1
            if m == 0:
                y, m = y - 1, 12
        return list(reversed(months))

    def _seed_residencies(
        self,
        residents: list[Resident],
        rooms: list[Room],
        workgroups: list[Workgroup],
        cleanings: list[Cleaning],
    ) -> None:
        for y, m in self._iter_recent_months(3):
            # Give each resident a distinct room this month (rooms outnumber residents).
            chosen_rooms = self.rng.sample(rooms, k=min(len(residents), len(rooms)))
            for resident, room in zip(residents, chosen_rooms, strict=False):
                Residency.objects.update_or_create(
                    resident=resident,
                    year=y,
                    month=m,
                    defaults={
                        "room": room,
                        "workgroup": self.rng.choice(workgroups),
                        "cleaning": self.rng.choice(cleanings),
                    },
                )
        # Make one resident a "leaver" (present last month, gone this month) so the regnskab page has data.
        if residents:
            Residency.objects.filter(resident=residents[-1], year=self.year, month=self.month).delete()

    # --------------------------------------------------------------- roles
    def _seed_roles(self, residents: list[Resident]) -> None:
        by_email = {r.email: r for r in residents}
        # Documented accounts get specific roles.
        fixed = {
            "formand@gahk.dk": [Role.ADMINISTRATOR],
            "ak@gahk.dk": [Role.AK],
            "oel@gahk.dk": [Role.OELKAELDER],
            "regnskab@gahk.dk": [Role.REGNSKAB],
        }
        assigned = set()
        for email, roles in fixed.items():
            for role in roles:
                RoleAssignment.objects.get_or_create(
                    resident=by_email[email], role=role, year=self.year, month=self.month
                )
                assigned.add(by_email[email].pk)

        # Sprinkle the remaining privileged roles across other residents for the current month.
        pool = [r for r in residents if r.email not in fixed and not r.email.startswith(("admin", "beboer"))]
        for role in [
            Role.INDSTILLING,
            Role.INSPEKTION,
            Role.KOKKENGRUPPE,
            Role.AK,
            Role.OELKAELDER,
            Role.REGNSKAB,
        ]:
            for r in self.rng.sample(pool, k=min(3, len(pool))):
                RoleAssignment.objects.get_or_create(resident=r, role=role, year=self.year, month=self.month)
                assigned.add(r.pk)

        Resident.objects.filter(pk__in=assigned).update(is_staff=True)

    # ------------------------------------------------------------------ AK
    def _seed_ak(self, residents: list[Resident]) -> None:
        entries = []
        for r in residents:
            entries.append(
                AkEntry(
                    resident=r,
                    delta=self.rng.randint(-4, 8),
                    kind=AkEntry.Kind.OPENING,
                    reason="Startsaldo (demo)",
                    created_at=self.now - timedelta(days=120),
                )
            )
            # Some labour history so balances vary.
            for months_ago in (2, 1):
                if self.rng.random() < 0.6:
                    entries.append(
                        AkEntry(
                            resident=r,
                            delta=self.rng.randint(1, 4),
                            kind=AkEntry.Kind.LABOUR,
                            reason="Udført arbejde",
                            created_at=self.now - timedelta(days=30 * months_ago - 5),
                        )
                    )
        AkEntry.objects.bulk_create(entries)

        # The 12-month schedule rows are created by the migration; make sure they exist (belt-and-braces
        # for a --fresh run) then apply the monthly deduction through the real service for the recent
        # months, so demo MONTHLY entries respect alumneliste membership.
        for month in range(1, 13):
            AkMonthlyCharge.objects.get_or_create(month=month, defaults={"active": True, "krydser": 2})
        for y, m in self._iter_recent_months(3):
            apply_monthly_charge(y, m)

    # ----------------------------------------------------------- ølkælder
    def _seed_oelkaelder(self, residents: list[Resident]) -> None:
        products = [
            Product.objects.create(name=n, price_ore=p, active=True, highlighted=h)
            for n, p, h in [
                ("Grøn Tuborg", 700, True),
                ("Carlsberg", 700, False),
                ("Sodavand", 500, False),
                ("Fadøl", 1000, True),
                ("Vand", 300, False),
                ("Snacks", 1200, False),
            ]
        ]
        # Roughly 2/3 of residents shop in the cellar.
        shoppers = [
            Shopper.objects.create(resident=r, active=True) for r in residents if self.rng.random() < 0.66
        ]
        for s in shoppers:
            for _ in range(self.rng.randint(1, 3)):
                Deposit.objects.create(
                    shopper=s,
                    amount_ore=self.rng.choice([5000, 10000, 20000]),
                    created_at=self.now - timedelta(days=self.rng.randint(1, 90)),
                )

        for _ in range(80):  # 80 fake purchases
            buyers = self.rng.sample(shoppers, k=self.rng.randint(1, min(4, len(shoppers))))
            txn = Transaction.objects.create(created_at=self.now - timedelta(days=self.rng.randint(0, 60)))
            total = 0
            for _ in range(self.rng.randint(1, 3)):
                prod = self.rng.choice(products)
                qty = self.rng.randint(1, 3)
                line = prod.price_ore * qty
                total += line
                TransactionItem.objects.create(transaction=txn, product=prod, quantity=qty, price_ore=line)
            # Largest-remainder split so shares sum exactly to the total (mirrors the real POS).
            base, extra = divmod(total, len(buyers))
            for i, b in enumerate(buyers):
                PurchaseShare.objects.create(
                    transaction=txn, shopper=b, share_ore=base + (1 if i < extra else 0)
                )

    # ------------------------------------------------------------ admissions
    def _seed_admissions(self, residents: list[Resident]) -> None:
        officers = [r for r in residents if r.is_staff]
        apps = []
        for _ in range(25):
            is_tour = self.rng.random() < 0.7
            submitted = self.now - timedelta(days=self.rng.randint(0, 200))
            received = self.rng.random() < 0.5
            apps.append(
                Application(
                    type=Application.Type.TOUR if is_tour else Application.Type.SUBLET,
                    full_name=self.fake.name(),
                    email=self.fake.ascii_email(),
                    gender=self.rng.choice([g.value for g in Application.Gender]),
                    age=str(self.rng.randint(19, 28)),
                    study_year=self.rng.choice(["1", "2", "3", ""]) if is_tour else "",
                    university=self.rng.choice(["KU", "DTU", "CBS", ""]) if is_tour else "",
                    field_of_study=self.fake.job() if is_tour else "",
                    occupation="" if is_tour else self.rng.choice(["Studerende", "I arbejde"]),
                    heard_about_us=self.rng.choice(["Ven", "Avis", "Hjemmeside", "Facebook"]),
                    motivation=self.fake.paragraph(nb_sentences=3),
                    submitted_at=submitted,
                    received_by=self.rng.choice(officers) if (received and officers) else None,
                    received_at=submitted + timedelta(days=2) if received else None,
                )
            )
        Application.objects.bulk_create(apps)

    # ------------------------------------------------------------------ cms
    def _seed_cms(self) -> None:
        Page.objects.create(
            slug="om-kollegiet",
            header="Om Kollegiet",
            body="<p>G. A. Hagemanns Kollegium blev grundlagt i 1908.</p>",
            menu_category=1,
        )
        Page.objects.create(
            slug="ansoegning",
            header="Ansøgning",
            body="<p>Sådan søger du en plads på kollegiet.</p>",
            menu_category=2,
        )
        # A sub-page, so the section sidebar (cms.views._section_nav) and the CMS overview's
        # reachability badge are both exercised in dev — with only top-level slugs, neither ever
        # renders, and the bug that made a renamed page vanish was invisible locally.
        Page.objects.create(
            slug="faciliteter",
            header="Faciliteter",
            body="<p>Kollegiets faciliteter.</p>",
            menu_category=1,
        )
        Page.objects.create(
            slug="faciliteter/kokken",
            header="Køkkenet",
            body="<p>Fælleskøkkenet på hver gang.</p>",
            menu_category=1,
        )
        for _ in range(6):
            NewsItem.objects.create(
                title=self.fake.sentence(nb_words=6),
                body=self.fake.paragraph(nb_sentences=4),
                published_at=self.now - timedelta(days=self.rng.randint(1, 300)),
            )
        today = timezone.localdate()
        for _ in range(8):
            CmsEvent.objects.create(
                title=self.rng.choice(
                    ["Fællesspisning", "Havedag", "Julefrokost", "Rusarrangement", "Generalforsamling"]
                ),
                description=self.fake.sentence(),
                starts_on=today + timedelta(days=self.rng.randint(1, 90)),
            )
        PylonEvent.objects.create(
            title="Arkiveret begivenhed", description="(migreret)", starts_on=today - timedelta(days=400)
        )

    # ------------------------------------------------------ room conditions
    def _seed_room_conditions(self, rooms: list[Room], residents: list[Resident]) -> None:
        # Four real criteria from intern_room_criteria, chosen to cover all three scale shapes
        # (5 -> 1..5, 3 -> 0..2, 2 -> 0..1) with their real Danish legends, so the score explainer and
        # the range validation are both visible in dev. The previous invented set used options=4 —
        # a shape no real criterion has — with no descriptions, which made the feature untestable.
        # update_or_create, not get_or_create: re-seeding without --wipe must refresh stale rows.
        criteria = [
            RoomCriterion.objects.update_or_create(
                code=code, defaults={"name": name, "options": options, "description": description}
            )[0]
            for code, name, options, description in ROOM_CRITERIA
        ]
        for room in self.rng.sample(rooms, k=min(15, len(rooms))):
            # A couple of superseded reports per room so the "vis tidligere rapport" dropdown has
            # something to show (the real ones come from backfill_room_history).
            for age, current in ((self.rng.randint(400, 700), False), (self.rng.randint(1, 300), True)):
                cond = RoomCondition.objects.create(
                    room=room,
                    resident=self.rng.choice(residents),
                    recorded_by_name=self.rng.choice(residents).full_name,
                    recorded_at=self.now - timedelta(days=age),
                    is_current=current,
                )
                for crit in criteria:
                    RoomConditionScore.objects.create(
                        condition=cond,
                        criterion=crit,
                        score=self.rng.choice(crit.score_values),  # always on-scale
                        comment=self.rng.choice(["", "Fin stand", "Slitage", "Skal males"]),
                    )

    # -------------------------------------------------------------- kvotient
    def _seed_kvotient(self, residents: list[Resident], rooms: list[Room]) -> None:
        month_no = 12 * self.year + self.month
        for room in self.rng.sample(rooms, k=min(6, len(rooms))):
            RoomOffer.objects.get_or_create(room=room, month=month_no)
        for r in self.rng.sample(residents, k=min(12, len(residents))):
            app = KvotientApplication.objects.create(
                resident=r,
                move_month=month_no + 2,
                move_in_month=month_no + 3,
                done_studying_month=month_no + self.rng.randint(6, 36),
                k=round(self.rng.uniform(0.5, 9.5), 2),
            )
            for prio, room in enumerate(self.rng.sample(rooms, k=3), start=1):
                KvotientPriority.objects.create(application=app, room=room, priority=prio)

    # ----------------------------------------------------------------- stats
    def _seed_stats(self) -> None:
        today = timezone.localdate()
        for i in range(60):
            DailyVisitCount.objects.update_or_create(
                date=today - timedelta(days=i), defaults={"count": self.rng.randint(5, 120)}
            )
        for i in range(30):
            VisitTally.objects.create(
                ip_hash=f"{i:064x}",
                count=self.rng.randint(1, 50),
                first_seen=self.now - timedelta(days=self.rng.randint(30, 120)),
                last_seen=self.now,
            )

    # ---------------------------------------------------------------- report
    def _report(self, residents: list[Resident]) -> None:
        self.stdout.write(
            self.style.SUCCESS(
                f"\nSeeded demo data: {len(residents)} residents, "
                f"{Residency.objects.count()} residencies, {AkEntry.objects.count()} AK entries, "
                f"{Event.objects.count()} begivenheder, "
                f"{Transaction.objects.count()} ølkælder purchases, {Application.objects.count()} applications."
            )
        )
        self.stdout.write(f"\nDev logins (password: {DEMO_PASSWORD}):")
        for email, desc in [
            ("admin@gahk.dk", "superuser (all access)"),
            ("formand@gahk.dk", "administrator role"),
            ("ak@gahk.dk", "AK role"),
            ("oel@gahk.dk", "Ølkælder role"),
            ("beboer@gahk.dk", "plain resident"),
        ]:
            self.stdout.write(f"  {email:22} {desc}")
