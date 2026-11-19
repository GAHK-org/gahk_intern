"""Delte mapper: folders for whoever is invited, until they expire (spec/features/Drive.md).

Any resident creates one and is its first moderator. It always has at least MIN_SHARED_MEMBERS
members, counting every role and counting members who have moved out, and always a moderator:
nobody can be removed, or leave, if that would break either.

At creation the moderator picks an expiry - SHARED_DEFAULT_MONTHS ahead by default, at most
SHARED_MAX_YEARS after creation - and whether the folder is then deleted or archived. The choice can
go from archive to delete, never back: people uploaded to a folder that promised to disappear.
Moderators are told SHARED_WARN_DAYS before. Deletion at expiry is for good, with no bin. Archiving
moves it to `Delte mapper/<year it was created>/`, read-only and readable by every resident.
"""

from datetime import date, timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F, Value
from django.db.models.functions import Concat, Substr
from django.urls import reverse
from django.utils import timezone

from core.clock import current_date
from notifications import categories
from notifications import services as notifications
from residents.models import Residency, Resident, active_period

from . import history, services
from .access import Viewer
from .models import (
    MIN_SHARED_MEMBERS,
    SHARED_DEFAULT_MONTHS,
    SHARED_MAX_YEARS,
    SHARED_WARN_DAYS,
    Event,
    File,
    FileVersion,
    Folder,
    SharedFolder,
    SharedMember,
    Space,
    add_years,
    name_key,
)

Role = SharedMember.Role


def residents_to_invite() -> list[Resident]:
    """Who lives here this month, by name."""
    year, month = active_period()
    ids = Residency.objects.filter(year=year, month=month).values("resident_id")
    return list(Resident.objects.filter(pk__in=ids).order_by("first_name", "last_name"))


def default_expiry(today: date | None = None) -> date:
    today = today or current_date()
    month = today.month - 1 + SHARED_DEFAULT_MONTHS
    year, month = today.year + month // 12, month % 12 + 1
    for day in (today.day, 30, 29, 28):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    raise AssertionError  # pragma: no cover


def latest_new_expiry() -> date:
    """The latest expiry a Delt mappe created today may have."""
    return add_years(current_date(), SHARED_MAX_YEARS)


def _check_expiry(shared: SharedFolder | None, expires_on: date | None) -> date:
    today = current_date()
    if expires_on is None:
        raise ValidationError("Vælg en udløbsdato.")
    if expires_on <= today:
        raise ValidationError("Udløbsdatoen skal ligge efter i dag.")
    latest = shared.latest_expiry() if shared else latest_new_expiry()
    if expires_on > latest:
        raise ValidationError(f"Udløbsdatoen kan højst være {latest:%-d/%-m %Y}.")
    return expires_on


def _tell_invited(shared: SharedFolder, folder: Folder, residents: list[int], actor: Resident) -> None:
    notifications.notify(
        residents,
        category=categories.GENERAL,
        title=f"{actor.full_name} delte mappen {folder.name} med dig",
        url=reverse("drive:folder", args=[folder.pk]),
        exclude=actor.pk,
    )


@transaction.atomic
def create(
    viewer: Viewer,
    *,
    name: str,
    members: list[int],
    expires_on: date | None,
    on_expiry: str,
    actor: Resident,
) -> Folder:
    if not viewer.living:
        raise PermissionDenied
    root = Folder.objects.get(kind=Folder.Kind.SHARED_ROOT)
    name = services.check_name(root, name)
    invited = sorted({pk for pk in members if pk != actor.pk})
    allowed = {resident.pk for resident in residents_to_invite()}
    if any(pk not in allowed for pk in invited):
        raise ValidationError("Man kan kun invitere beboere, der bor her nu.")
    if len(invited) + 1 < MIN_SHARED_MEMBERS:
        raise ValidationError(
            f"En delt mappe skal have mindst {MIN_SHARED_MEMBERS} medlemmer, dig selv medregnet."
        )
    if on_expiry not in SharedFolder.OnExpiry.values:
        raise ValidationError("Vælg, om mappen slettes eller arkiveres, når den udløber.")
    shared = SharedFolder.objects.create(
        expires_on=_check_expiry(None, expires_on), on_expiry=on_expiry, created_by=actor
    )
    folder = Folder(
        parent=root, name=name, kind=Folder.Kind.SHARED, space=Space.SHARED, shared=shared, created_by=actor
    )
    folder.save()
    SharedMember.objects.create(shared=shared, resident=actor, role=Role.MODERATOR)
    SharedMember.objects.bulk_create(
        SharedMember(shared=shared, resident_id=pk, role=Role.EDIT) for pk in invited
    )
    history.record(Event.Action.CREATED, actor=actor, folder=folder, name=folder.name)
    for resident in Resident.objects.filter(pk__in=invited):
        history.record(
            Event.Action.MEMBER_ADDED,
            actor=actor,
            folder=folder,
            name=folder.name,
            member=resident.full_name,
            role=Role.EDIT.label,
        )
    _tell_invited(shared, folder, invited, actor)
    return folder


def _moderated(viewer: Viewer, shared: SharedFolder) -> Folder:
    if not viewer.moderates(shared.pk):
        raise PermissionDenied
    return shared.top()


def _check_still_whole(
    shared: SharedFolder, *, without: int | None = None, demoting: int | None = None
) -> None:
    rows = list(shared.members.all())
    left = [row for row in rows if row.resident_id != without]
    if len(left) < MIN_SHARED_MEMBERS:
        raise ValidationError(f"En delt mappe skal have mindst {MIN_SHARED_MEMBERS} medlemmer.")
    moderators = [row for row in left if row.role == Role.MODERATOR and row.resident_id != demoting]
    if not moderators:
        raise ValidationError("Der skal altid være en moderator. Gør en anden til moderator først.")


@transaction.atomic
def add_member(viewer: Viewer, shared: SharedFolder, resident_id: int, role: str, actor: Resident) -> None:
    folder = _moderated(viewer, shared)
    if role not in Role.values:
        raise ValidationError("Ukendt rolle.")
    if resident_id not in {resident.pk for resident in residents_to_invite()}:
        raise ValidationError("Man kan kun invitere beboere, der bor her nu.")
    _, created = SharedMember.objects.get_or_create(
        shared=shared, resident_id=resident_id, defaults={"role": role}
    )
    if not created:
        raise ValidationError("Vedkommende er allerede medlem.")
    resident = Resident.objects.get(pk=resident_id)
    history.record(
        Event.Action.MEMBER_ADDED,
        actor=actor,
        folder=folder,
        name=folder.name,
        member=resident.full_name,
        role=Role(role).label,
    )
    _tell_invited(shared, folder, [resident_id], actor)


@transaction.atomic
def set_role(viewer: Viewer, shared: SharedFolder, resident_id: int, role: str, actor: Resident) -> None:
    folder = _moderated(viewer, shared)
    if role not in Role.values:
        raise ValidationError("Ukendt rolle.")
    member = (
        SharedMember.objects.select_for_update()
        .select_related("resident")
        .get(shared=shared, resident_id=resident_id)
    )
    if member.role == role:
        return
    if member.role == Role.MODERATOR:
        _check_still_whole(shared, demoting=resident_id)
    member.role = role
    member.save(update_fields=["role"])
    history.record(
        Event.Action.ROLE_CHANGED,
        actor=actor,
        folder=folder,
        name=folder.name,
        member=member.resident.full_name,
        role=Role(role).label,
    )


@transaction.atomic
def remove_member(viewer: Viewer, shared: SharedFolder, resident_id: int, actor: Resident) -> None:
    """A moderator removes somebody, or a member leaves (resident_id is their own)."""
    leaving = resident_id == actor.pk
    if leaving:
        if shared.pk not in viewer.shared_roles:
            raise PermissionDenied
        folder = shared.top()
    else:
        folder = _moderated(viewer, shared)
    member = (
        SharedMember.objects.select_for_update()
        .select_related("resident")
        .get(shared=shared, resident_id=resident_id)
    )
    _check_still_whole(shared, without=resident_id)
    member.delete()
    if leaving:
        history.record(Event.Action.MEMBER_LEFT, actor=actor, folder=folder, name=folder.name)
    else:
        history.record(
            Event.Action.MEMBER_REMOVED,
            actor=actor,
            folder=folder,
            name=folder.name,
            member=member.resident.full_name,
        )


@transaction.atomic
def set_expiry(
    viewer: Viewer, shared: SharedFolder, expires_on: date | None, on_expiry: str, actor: Resident
) -> None:
    folder = _moderated(viewer, shared)
    expires_on = _check_expiry(shared, expires_on)
    if on_expiry not in SharedFolder.OnExpiry.values:
        raise ValidationError("Vælg, om mappen slettes eller arkiveres.")
    if shared.on_expiry == SharedFolder.OnExpiry.DELETE and on_expiry == SharedFolder.OnExpiry.ARCHIVE:
        raise ValidationError(
            "En mappe, der skal slettes, kan ikke laves om til at blive arkiveret: "
            "nogen har lagt filer i den i tillid til, at de forsvinder."
        )
    if (shared.expires_on, shared.on_expiry) == (expires_on, on_expiry):
        return
    shared.expires_on, shared.on_expiry = expires_on, on_expiry
    shared.save(update_fields=["expires_on", "on_expiry"])
    history.record(
        Event.Action.EXPIRY_CHANGED,
        actor=actor,
        folder=folder,
        name=folder.name,
        expires_on=f"{expires_on:%-d/%-m %Y}",
        then=SharedFolder.OnExpiry(on_expiry).label,
    )


@transaction.atomic
def rename(viewer: Viewer, shared: SharedFolder, name: str, actor: Resident) -> None:
    folder = _moderated(viewer, shared)
    name = services.check_name(folder, name)
    if name == folder.name:
        return
    old = folder.name
    folder.name = name
    folder.save()
    history.record(Event.Action.RENAMED, actor=actor, folder=folder, name=name, old=old)


@transaction.atomic
def delete(viewer: Viewer, shared: SharedFolder, actor: Resident) -> None:
    """Into the bin, like any folder, and only when empty. The bin then removes it for good."""
    folder = _moderated(viewer, shared)
    if (
        File.objects.filter(folder__shared=shared, deleted_at__isnull=True).exists()
        or folder.children.filter(deleted_at__isnull=True).exists()
    ):
        raise ValidationError("En delt mappe kan kun slettes, når den er tom.")
    Folder.objects.filter(pk=folder.pk).update(deleted_at=timezone.now(), deleted_by=actor)
    history.record(Event.Action.DELETED, actor=actor, folder=folder, name=folder.name)


# --- Expiry ----------------------------------------------------------------------------------------


def _moderator_ids(shared: SharedFolder) -> list[int]:
    return list(shared.members.filter(role=Role.MODERATOR).values_list("resident_id", flat=True))


def _purge(shared: SharedFolder) -> None:
    """Everything in it, for good: no bin."""
    keys = set(FileVersion.objects.filter(file__folder__shared=shared).values_list("key", flat=True))
    File.objects.filter(folder__shared=shared).delete()
    for folder in sorted(Folder.objects.filter(shared=shared), key=lambda f: f.path.count("/"), reverse=True):
        folder.delete()
    shared.delete()
    services.drop_unreferenced(keys)


def _archive(shared: SharedFolder) -> None:
    top = shared.top()
    root = Folder.objects.get(kind=Folder.Kind.SHARED_ROOT)
    year = timezone.localtime(shared.created_at).year
    shelf = Folder.objects.filter(
        parent=root, kind=Folder.Kind.ARCHIVE_YEAR, period_start=date(year, 1, 1)
    ).first()
    if shelf is None:
        shelf = Folder(
            parent=root,
            name=str(year),
            kind=Folder.Kind.ARCHIVE_YEAR,
            space=Space.SHARED,
            period_start=date(year, 1, 1),
        )
        shelf.save()
    name = top.name
    if Folder.objects.filter(parent=shelf, name_key=name_key(name), deleted_at__isnull=True).exists():
        name = services.free_name(shelf, name, keep_suffix=False)
    old_path = top.path
    Folder.objects.filter(pk=top.pk).update(parent=shelf, name=name, name_key=name_key(name))
    Folder.objects.filter(path__startswith=old_path).update(
        path=Concat(Value(f"{shelf.path}{top.pk}/"), Substr("path", len(old_path) + 1))
    )
    shared.archived_at = timezone.now()
    shared.save(update_fields=["archived_at"])
    top.refresh_from_db()
    history.record(Event.Action.ARCHIVED, actor=None, folder=top, name=top.name)


def run_expiry(today: date | None = None) -> tuple[int, int, int]:
    """Daily: warn moderators SHARED_WARN_DAYS ahead, and delete or archive what has expired.
    Returns (warned, deleted, archived)."""
    today = today or current_date()
    warned = deleted = archived = 0
    live = SharedFolder.objects.filter(archived_at__isnull=True)
    for shared in live.filter(expires_on__lt=today):
        with transaction.atomic():
            if shared.on_expiry == SharedFolder.OnExpiry.DELETE:
                _purge(shared)
                deleted += 1
            else:
                _archive(shared)
                archived += 1
    for shared in live.filter(expires_on__lte=today + timedelta(days=SHARED_WARN_DAYS)).exclude(
        warned_for=F("expires_on")
    ):
        top = Folder.objects.filter(shared=shared, kind=Folder.Kind.SHARED, deleted_at__isnull=True).first()
        if top is None:
            continue
        notifications.notify(
            _moderator_ids(shared),
            category=categories.GENERAL,
            title=f"Den delte mappe {top.name} udløber {shared.expires_on:%-d/%-m}",
            body=f"Så {shared.get_on_expiry_display()} den. Du kan flytte datoen under Medlemmer og udløb.",
            url=reverse("drive:shared", args=[shared.pk]),
        )
        shared.warned_for = shared.expires_on
        shared.save(update_fields=["warned_for"])
        warned += 1
    return warned, deleted, archived
