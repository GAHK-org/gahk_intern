"""A made-up history for the audit trail (`manage.py seed_demo`).

The rest of the seed writes unaudited: loading demo data is nobody's action, and thousands of
"created" lines would show nothing. This step then writes, on purpose, the five weeks before the
seeded day, so /admin/audit (and the features' own history pages) have something to read on a fresh
checkout:

  * logins by every documented account and a few others, from the house network, phones and an IPv6
    address, and some logouts;
  * failed logins of both kinds a resident causes: a wrong password followed by the right one, and an
    address with no account, typed with an å (the trail keeps only its domain and a keyed hash); and a
    night-time burst of wrong passwords against admin@ from an address no resident uses;
  * a password changed, and a reset link asked for (first with a private address that has no account,
    kept the same way, then the right one) and used;
  * pages refused: beboer@ trying the roles page, a resident trying the audit page;
  * roles given and removed, and the two-person rule both ways: one new administrator approved and
    one refused, each role taken away again afterwards;
  * a resident's details corrected by indstilling, a profile filled in, the alumneliste saved, the
    newcomer put on it in the leaver's place, the room round ended, an e-conomic kontonr. set;
  * an administrator viewing the site as another role, and back;
  * "Uden handling": an edit in the Django admin (web) and a fix from a shell (System).

Every event goes through the trail's own API (trail.event / trail.record), inside a made-up request
for its address, dated in the past with trail.backdated. Its writes are real: what an event changes
is first set back to an earlier value unaudited, and the event then changes it, so every before and
after is true and the history ends at the data the rest of the seed made (a role given here is the
one the seed gave; one removed here is one the seed never gave). The addresses are from the ranges
reserved for documentation (RFC 5737, RFC 3849), never anyone's real one.

The page lists by id, so the whole history is played in time order, with other apps' moments (their
own `history(residents, now, rng)`, passed to `seed`) merged in.
"""

import contextlib
import random
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from residents.models import AdminAccessGrant, Residency, Resident, Role, RoleAssignment

from . import trail
from .models import AuditEvent

# How far back the history goes.
WEEKS = 5

HOUSE = "192.0.2.17"  # the kollegium's network: everyone at home is this one address from outside
PHONES = ("198.51.100.23", "198.51.100.142")  # mobile networks
IPV6 = "2001:db8:4a1:7::25"  # a resident's own line
STRANGER = "203.0.113.66"  # nobody who lives here

LOGINS = (
    "admin@gahk.dk",
    "formand@gahk.dk",
    "ak@gahk.dk",
    "oel@gahk.dk",
    "regnskab@gahk.dk",
    "beboer@gahk.dk",
)


@dataclass(frozen=True)
class Moment:
    """One event of the made-up history: when, by whom, from where, and the work it does.

    `source` "web" plays it inside a request from `ip` to `path`; "worker" inside a worker job;
    "system" outside both, as a shell or a management command."""

    at: datetime
    play: Callable[[], object]  # what it returns is not used
    actor: Resident | None = None
    ip: str | None = HOUSE
    path: str = "/"
    source: str = "web"


History = Callable[[list[Resident], datetime, random.Random], list[Moment]]


def ago(now: datetime, days: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    """`days` before `now` at hh:mm (Copenhagen), and never later than a minute ago."""
    local = timezone.localtime(now) - timedelta(days=days)
    at = local.replace(hour=hour, minute=minute, second=second, microsecond=0)
    return min(at, now - timedelta(minutes=1))


def this_month(now: datetime, days: int, hour: int, minute: int = 0) -> datetime:
    """Like `ago`, but within the current month: an event that has to fall in the period the seed's
    roles and lists belong to."""
    start = timezone.localtime(now).replace(day=1, hour=8, minute=0, second=0, microsecond=0)
    # Days that fall before the month all land on its first morning, still in their order.
    first = start + timedelta(minutes=WEEKS * 7 - days)
    return min(max(ago(now, days, hour, minute), first), now - timedelta(minutes=1))


def seed(residents: list[Resident], now: datetime, rng: random.Random, *more: History) -> int:
    """Write the history; returns how many events it made. Nothing when the trail already has events
    (seed_demo without --fresh): a trail is never written twice."""
    if AuditEvent.objects.exists():
        return 0
    with trail.unaudited():  # setting things back to how they were before the history
        moments = _history(residents, now, rng)
        for history in more:
            moments += history(residents, now, rng)
    for moment in sorted(moments, key=lambda m: m.at):  # stable: same-time moments keep their order
        with _scene(moment, rng):
            moment.play()
    return AuditEvent.objects.count()


@contextlib.contextmanager
def _scene(moment: Moment, rng: random.Random) -> Iterator[None]:
    correlation_id = uuid.UUID(int=rng.getrandbits(128)).hex
    with contextlib.ExitStack() as stack:
        stack.enter_context(trail.backdated(moment.at))
        if moment.source == "web":
            request = RequestFactory().post(moment.path, REMOTE_ADDR=moment.ip or "")
            request.user = moment.actor or AnonymousUser()
            stack.enter_context(trail.context("web", correlation_id=correlation_id, request=request))
        elif moment.source == "worker":
            stack.enter_context(trail.context("worker", correlation_id=correlation_id))
        yield


# --- the residents' own history ---------------------------------------------------------------------


@dataclass
class _Cast:
    by_email: dict[str, Resident]
    indstilling: Resident
    others: list[Resident]  # neither a documented login, nor the leaver or the newcomer
    leaver: Resident | None
    newcomer: Resident | None
    year: int
    month: int

    def __getitem__(self, email: str) -> Resident:
        return self.by_email[email]


def _cast(residents: list[Resident], now: datetime) -> _Cast | None:
    by_email = {r.email: r for r in Resident.objects.filter(email__in=LOGINS)}
    if len(by_email) < len(LOGINS):
        return None
    today = timezone.localtime(now).date()
    leaver = Resident.objects.filter(move_out_date__isnull=False).order_by("pk").first()
    newcomer = Resident.objects.filter(move_in_date=today.replace(day=1)).order_by("pk").first()
    skip = {*LOGINS, getattr(leaver, "email", ""), getattr(newcomer, "email", "")}
    others = sorted((r for r in residents if r.email not in skip), key=lambda r: r.pk)
    if len(others) < 3:
        return None
    holder = (
        RoleAssignment.objects.filter(role=Role.INDSTILLING, year=today.year, month=today.month)
        .select_related("resident")
        .order_by("pk")
        .first()
    )
    return _Cast(
        by_email=by_email,
        indstilling=holder.resident if holder else by_email["formand@gahk.dk"],
        others=others,
        leaver=leaver,
        newcomer=newcomer,
        year=today.year,
        month=today.month,
    )


def _history(residents: list[Resident], now: datetime, rng: random.Random) -> list[Moment]:
    cast = _cast(residents, now)
    if cast is None:
        return []
    # Each story takes its people by position from a shuffled copy, so with enough residents no one
    # plays two parts; a small demo (the tests' dozen) goes round again.
    shuffled = list(cast.others)
    rng.shuffle(shuffled)
    spare = [shuffled[i % len(shuffled)] for i in range(13)]
    return [
        *_logins(cast, spare[:4], now, rng),
        *_failed_logins(cast, now),
        *_passwords(cast, now),
        *_refused(cast, spare[4], now),
        *_roles(cast, spare, now),
        *_resident_edits(cast, spare, now),
        *_month_list(cast, spare, now),
        *_room_round(cast, spare[:2], now),
        *_preview(cast, now),
        *_unclaimed(cast, spare, now),
    ]


def _login(person: Resident) -> Callable[[], None]:
    return lambda: trail.record("auth.login", actor=person, subject=person)


def _logout(person: Resident) -> Callable[[], None]:
    return lambda: trail.record("auth.logout", actor=person, subject=person)


def _logins(cast: _Cast, regulars: list[Resident], now: datetime, rng: random.Random) -> list[Moment]:
    """Everyday logins, mostly from the house, sometimes a phone; a few log out again. The kind of
    line the page is mostly made of, so the rarer ones below stand out among them."""
    moments = []
    for person in [*cast.by_email.values(), *regulars]:
        own_ip = IPV6 if person.email == "regnskab@gahk.dk" else HOUSE
        for _ in range(rng.randint(3, 6)):
            at = ago(now, rng.randint(0, WEEKS * 7 - 1), rng.randint(7, 23), rng.randint(0, 59))
            ip = rng.choice([own_ip, own_ip, own_ip, *PHONES])
            moments.append(Moment(at, _login(person), person, ip, reverse("login")))
            if rng.random() < 0.3:
                out = at + timedelta(minutes=rng.randint(5, 90))
                moments.append(
                    Moment(
                        min(out, now - timedelta(seconds=30)),
                        _logout(person),
                        person,
                        ip,
                        reverse("logout"),
                    )
                )
    return moments


def _failed(account: Resident | None, details: dict[str, str]) -> Callable[[], None]:
    return lambda: trail.record("auth.login.failed", actor=None, subject=account, details=details)


def _failed_logins(cast: _Cast, now: datetime) -> list[Moment]:
    beboer, formand, admin = cast["beboer@gahk.dk"], cast["formand@gahk.dk"], cast["admin@gahk.dk"]
    login = reverse("login")
    moments = [
        # A wrong password on the phone, then the right one: the everyday case.
        Moment(ago(now, 9, 21, 14, 5), _failed(beboer, {"reason": "wrong password"}), None, PHONES[0], login),
        Moment(ago(now, 9, 21, 14, 40), _login(beboer), beboer, PHONES[0], login),
        # An address with no account: "formånd", the keyboard's å for a. The trail keeps its domain
        # and a keyed hash, so searching the page for "formånd@gahk.dk" finds it.
        Moment(
            ago(now, 16, 8, 2, 10),
            _failed(None, {"reason": "no account", **trail.typed_address("formånd@gahk.dk")}),
            None,
            HOUSE,
            login,
        ),
        Moment(ago(now, 16, 8, 2, 50), _login(formand), formand, HOUSE, login),
    ]
    # Somebody trying admin@ at night from an address no resident uses: what the page is for.
    for i in range(5):
        moments.append(
            Moment(
                ago(now, 4, 3, 11, 7 * i), _failed(admin, {"reason": "wrong password"}), None, STRANGER, login
            )
        )
    moments.append(
        Moment(
            ago(now, 4, 3, 12, 10),
            _failed(None, {"reason": "no account", "email": ascii("administrator@gahk.dk")}),
            None,
            STRANGER,
            login,
        )
    )
    return moments


def _new_password(person: Resident, action: str, actor: Resident) -> Callable[[], None]:
    """The demo password set again: a new hash, so the trail records that it changed (never what it
    holds), and the documented login keeps working."""
    from core.management.commands.seed_demo import DEMO_PASSWORD

    def play() -> None:
        with trail.event(action, actor=actor, subject=person):
            person.set_password(DEMO_PASSWORD)
            person.save(update_fields=["password"])

    return play


def _reset_asked(email: str, account: Resident | None) -> Callable[[], None]:
    details: dict[str, object] = {"accounts": 1} if account else {"accounts": 0, **trail.typed_address(email)}
    return lambda: trail.record(
        "auth.password_reset.requested", actor=None, details=details, involve=[(account, "subject")]
    )


def _passwords(cast: _Cast, now: datetime) -> list[Moment]:
    ak, oel = cast["ak@gahk.dk"], cast["oel@gahk.dk"]
    reset = reverse("password_reset")
    return [
        Moment(
            ago(now, 12, 19, 40),
            _new_password(ak, "auth.password.changed", ak),
            ak,
            HOUSE,
            reverse("password_change"),
        ),
        # Ole asks with his private address first (no account, though Django answers "sent" all the
        # same: only the trail tells the two apart), then with the right one, and uses the link.
        Moment(ago(now, 20, 10, 2), _reset_asked("ole.oelmand@gmail.com", None), None, PHONES[1], reset),
        Moment(ago(now, 20, 10, 5), _reset_asked(oel.email, oel), None, PHONES[1], reset),
        Moment(
            ago(now, 20, 10, 9),
            _new_password(oel, "auth.password_reset.completed", oel),
            oel,
            PHONES[1],
            "/admin/reset/done",
        ),
    ]


def _denied(person: Resident, path: str, roles: list[str]) -> Callable[[], None]:
    return lambda: trail.record("auth.access.denied", actor=person, details={"path": path, "roles": roles})


def _refused(cast: _Cast, curious: Resident, now: datetime) -> list[Moment]:
    beboer = cast["beboer@gahk.dk"]
    roles, audit = reverse("siteadmin:roles"), reverse("siteadmin:audit")
    return [
        Moment(ago(now, 6, 13, 22), _denied(beboer, roles, [Role.ADMINISTRATOR]), beboer, HOUSE, roles),
        Moment(
            ago(now, 22, 23, 51), _denied(curious, audit, [Role.ADMINISTRATOR]), curious, PHONES[0], audit
        ),
    ]


def _role_event(
    action: str, by: Resident, resident: Resident, role: str, period: tuple[int, int], at: datetime
) -> Callable[[], None]:
    """What the roles page does (residents.views_admin.roles), with an administrator's grant opened
    when the history says."""
    year, month = period
    details = {"role": role, "period": f"{year}-{month:02d}"}

    def play() -> None:
        with trail.event(action, actor=by, subject=resident, details=details):
            if action == "residents.role.removed":
                RoleAssignment.objects.filter(resident=resident, role=role, year=year, month=month).delete()
                return
            RoleAssignment.objects.get_or_create(resident=resident, role=role, year=year, month=month)
            if role == Role.ADMINISTRATOR:  # the granter is stamped, so they cannot also approve
                grant = AdminAccessGrant.objects.filter(resident=resident, requested_by=None).first()
                if grant is not None:
                    grant.requested_by = by
                    grant.created_at = at
                    grant.save(update_fields=["requested_by", "created_at"])

    return play


def _decide(resident: Resident, by: Resident, approve: bool, at: datetime) -> Callable[[], None]:
    """residents.admin_access.decide, with the decision dated when the history says it was made."""

    def play() -> None:
        grant = AdminAccessGrant.objects.get(resident=resident)
        action = "residents.admin_access.approved" if approve else "residents.admin_access.denied"
        with trail.event(action, actor=by, subject=grant, involve=[(resident, "resident")]):
            grant.status = AdminAccessGrant.Status.APPROVED if approve else AdminAccessGrant.Status.DENIED
            grant.decided_by = by
            grant.decided_at = at
            grant.save(update_fields=["status", "decided_by", "decided_at"])

    return play


def _roles(cast: _Cast, spare: list[Resident], now: datetime) -> list[Moment]:
    formand, admin = cast["formand@gahk.dk"], cast["admin@gahk.dk"]
    period = (cast.year, cast.month)

    def by_formand(action: str, resident: Resident, role: str, at: datetime) -> Moment:
        play = _role_event(action, formand, resident, role, period, at)
        return Moment(at, play, formand, HOUSE, reverse("siteadmin:roles"))

    moments = []
    # Given: one of the kitchen roles the seed gave, taken back first so the event gives it again.
    kitchen = (
        RoleAssignment.objects.filter(role=Role.KOKKENGRUPPE, year=cast.year, month=cast.month)
        .select_related("resident")
        .order_by("pk")
        .first()
    )
    if kitchen is not None:
        kitchen.delete()
        moments.append(
            by_formand(
                "residents.role.added", kitchen.resident, Role.KOKKENGRUPPE, this_month(now, 10, 14, 5)
            )
        )

    # Removed: an inspektion role the seed never gave, so taking it away leaves the seeded roles.
    held = set(
        RoleAssignment.objects.filter(role=Role.INSPEKTION, year=cast.year, month=cast.month).values_list(
            "resident_id", flat=True
        )
    )
    taken_from = next((r for r in spare[5:] if r.pk not in held), None)
    if taken_from is not None:
        RoleAssignment.objects.create(
            resident=taken_from, role=Role.INSPEKTION, year=cast.year, month=cast.month
        )
        moments.append(
            by_formand("residents.role.removed", taken_from, Role.INSPEKTION, this_month(now, 3, 16, 40))
        )

    # The two-person rule both ways. Formand makes two residents administrators; admin@ approves one
    # and refuses the other; both roles are taken away again later, which closes their grants.
    for resident, approve, days in ((spare[6], True, 17), (spare[7], False, 8)):
        decided = this_month(now, days - 1, 9, 12)
        moments += [
            by_formand("residents.role.added", resident, Role.ADMINISTRATOR, this_month(now, days, 20, 10)),
            Moment(
                decided,
                _decide(resident, admin, approve, decided),
                admin,
                HOUSE,
                reverse("siteadmin:admin_access"),
            ),
            by_formand(
                "residents.role.removed",
                resident,
                Role.ADMINISTRATOR,
                this_month(now, days - 6 if approve else days - 1, 11, 30),
            ),
        ]
    return moments


def _save_resident(
    action: str, by: Resident, resident: Resident, fields: Mapping[str, object]
) -> Callable[[], None]:
    def play() -> None:
        fresh = Resident.objects.get(pk=resident.pk)
        for name, value in fields.items():
            setattr(fresh, name, value)
        with trail.event(action, actor=by, subject=fresh, only_if_changed=True):
            fresh.save()

    return play


def _resident_edits(cast: _Cast, spare: list[Resident], now: datetime) -> list[Moment]:
    beboer, regnskab, indstilling = cast["beboer@gahk.dk"], cast["regnskab@gahk.dk"], cast.indstilling
    moments = []

    # Indstilling corrects a phone number and a study: the seeded ones, after made-up earlier ones.
    corrected = spare[8]
    seeded = {"phone": corrected.phone, "study": corrected.study}
    Resident.objects.filter(pk=corrected.pk).update(
        phone="+45 20 11 22 33", study="Fysik, KU" if corrected.study != "Fysik, KU" else "Jura, KU"
    )
    moments.append(
        Moment(
            ago(now, 13, 18, 20),
            _save_resident("residents.resident.updated", indstilling, corrected, seeded),
            indstilling,
            HOUSE,
            reverse("edit_resident", args=[corrected.pk]),
        )
    )

    # beboer@ fills in her profile: new text where the seed left none.
    moments.append(
        Moment(
            ago(now, 11, 22, 47),
            _save_resident(
                "residents.profile.updated",
                beboer,
                beboer,
                {
                    "bio": "Læser biologi og passer kollegiets bier om sommeren.",
                    "instagram_handle": "bente.bier",
                },
            ),
            beboer,
            PHONES[0],
            reverse("edit_profile"),
        )
    )

    # Regnskab enters a missing e-conomic kontonr.: the one the seed gave, after none.
    numbered = next((r for r in spare[9:] if r.economic_account_number is not None), None)
    if numbered is None:
        return moments
    number = numbered.economic_account_number
    Resident.objects.filter(pk=numbered.pk).update(economic_account_number=None)

    def account_number() -> None:
        fresh = Resident.objects.get(pk=numbered.pk)
        with trail.event(
            "regnskab.account_number.updated",
            actor=regnskab,
            subject=fresh,
            details={"account_name": fresh.full_name},
        ):
            fresh.economic_account_number = number
            fresh.save(update_fields=["economic_account_number"])

    moments.append(
        Moment(
            ago(now, 15, 10, 30),
            account_number,
            regnskab,
            IPV6,
            reverse("regnskab:edit_economic_account_number", args=[numbered.pk]),
        )
    )
    return moments


def _month_list(cast: _Cast, spare: list[Resident], now: datetime) -> list[Moment]:
    """This month's list as indstilling made it, in the days before the month began."""
    indstilling = cast.indstilling
    period = f"{cast.year}-{cast.month:02d}"
    page = reverse("next_month_list")
    month_start = timezone.localtime(now).replace(day=1)
    days_before = (timezone.localtime(now) - month_start).days
    moments = []

    # Saved: one resident's cleaning moved, from a made-up one to the seeded one.
    residency = Residency.objects.filter(resident=spare[10], year=cast.year, month=cast.month).first()
    if residency is not None and residency.cleaning is not None:
        cleaning = residency.cleaning
        other = Residency.objects.exclude(cleaning=cleaning).exclude(cleaning=None).first()
        if other is not None:
            Residency.objects.filter(pk=residency.pk).update(cleaning=other.cleaning)

            def saved() -> None:
                with trail.event("residents.month_list.saved", actor=indstilling, details={"period": period}):
                    fresh = Residency.objects.get(pk=residency.pk)
                    fresh.cleaning = cleaning
                    fresh.save(update_fields=["cleaning"])

            moments.append(Moment(ago(now, days_before + 6, 20, 15), saved, indstilling, HOUSE, page))

    # Added in the leaver's place ("Erstatter"): the newcomer's seeded row, which was the leaver's.
    leaver, newcomer = cast.leaver, cast.newcomer
    place = Residency.objects.filter(resident=newcomer, year=cast.year, month=cast.month).first()
    if leaver is not None and newcomer is not None and place is not None:
        values = {"room": place.room, "workgroup": place.workgroup, "cleaning": place.cleaning}
        place.delete()
        Residency.objects.create(resident=leaver, year=cast.year, month=cast.month, **values)

        def added() -> None:
            with trail.event(
                "residents.month_list.added",
                actor=indstilling,
                subject=newcomer,
                details={"period": period},
                involve=[(leaver, "replaced")],
            ):
                Residency.objects.filter(resident=leaver, year=cast.year, month=cast.month).delete()
                Residency.objects.create(resident=newcomer, year=cast.year, month=cast.month, **values)

        moments.append(Moment(ago(now, days_before + 9, 19, 50), added, indstilling, HOUSE, page))
    return moments


def _room_round(cast: _Cast, movers: list[Resident], now: datetime) -> list[Moment]:
    """The room round that ended before this month: what it said it did, as rooms.views_soegvaerelse
    records it. Only the summary: the rooms the movers have now are the seeded ones, and redoing the
    moves for real would shuffle the whole month's list."""
    month_start = timezone.localtime(now).replace(day=1)
    previous = (month_start - timedelta(days=1)).date()
    moves = []
    for mover in movers:
        now_in = Residency.objects.filter(resident=mover, year=cast.year, month=cast.month).first()
        before = Residency.objects.filter(resident=mover, year=previous.year, month=previous.month).first()
        if now_in is not None:
            moves.append(
                {
                    "name": mover.full_name,
                    "from": before.room.number if before else None,
                    "to": now_in.room.number,
                }
            )
    if not moves:
        return []
    details = {"assigned": len(moves), "unresolved": 0, "evicted": 0, "moves": moves, "left": []}
    days_before = (timezone.localtime(now) - month_start).days

    def ended() -> None:
        with trail.event("rooms.round.ended", actor=cast.indstilling) as event:
            event.details.update(details)

    return [
        Moment(
            ago(now, days_before + 4, 21, 0),
            ended,
            cast.indstilling,
            HOUSE,
            reverse("soegvaerelse:end_round"),
        )
    ]


def _preview(cast: _Cast, now: datetime) -> list[Moment]:
    formand = cast["formand@gahk.dk"]
    page = reverse("siteadmin:preview_set")
    return [
        Moment(
            ago(now, 5, 15, 2),
            lambda: trail.record("residents.preview.set", actor=formand, details={"roles": [Role.AK]}),
            formand,
            HOUSE,
            page,
        ),
        Moment(
            ago(now, 5, 15, 9),
            lambda: trail.record("residents.preview.cleared", actor=formand),
            formand,
            HOUSE,
            page,
        ),
    ]


def _unclaimed(cast: _Cast, spare: list[Resident], now: datetime) -> list[Moment]:
    """Writes no screen named: the Django admin (recorded as the person logged in there) and a shell
    (System, nobody). Both show as "Uden handling"."""
    admin = cast["admin@gahk.dk"]

    # A birthday corrected in the Django admin.
    in_admin = spare[11]
    birthday = in_admin.birthday
    if birthday is not None:
        Resident.objects.filter(pk=in_admin.pk).update(birthday=_year_earlier(birthday))

    def admin_edit() -> None:
        fresh = Resident.objects.get(pk=in_admin.pk)
        fresh.birthday = birthday
        fresh.save()

    # An address with capitals, tidied from a shell.
    in_shell = spare[12]
    email = in_shell.email
    Resident.objects.filter(pk=in_shell.pk).update(
        email=email[:1].upper() + email[1:].replace("@gahk.dk", "@GAHK.dk")
    )

    def shell_fix() -> None:
        fresh = Resident.objects.get(pk=in_shell.pk)
        fresh.email = email
        fresh.save(update_fields=["email"])

    return [
        Moment(
            ago(now, 7, 23, 5),
            admin_edit,
            admin,
            HOUSE,
            f"/django-admin/residents/resident/{in_admin.pk}/change/",
        ),
        Moment(ago(now, 19, 16, 12), shell_fix, None, None, source="system"),
    ]


def _year_earlier(day: date) -> date:
    return day.replace(year=day.year - 1, day=28 if (day.month, day.day) == (2, 29) else day.day)
