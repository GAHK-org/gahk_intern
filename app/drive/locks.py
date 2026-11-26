"""Locks, and the requests somebody else decides: unlocking for a while, and deleting for good.

A lock (models.Lockable) is set by hand by anyone who can write, or by `auto_lock` every night: a
file a year after its last new version, a folder after a year with no new file, version or subfolder
anywhere inside it. Renaming and moving do not count. The folders the system makes never lock.

Lifting a lock, and deleting a version or a binned file before its time, take two people: a member
who can write asks, with a reason, and an admin or a member of Inspektionen decides - or, in a Delt
mappe, which they cannot see, one of its moderators - never the one who asked. They decide from the name, the path and the version, and never see the contents. The
asker is told the outcome.

An unlock is for at most MAX_UNLOCK_DAYS, and only ever of the item that holds the lock: a file in
a locked folder is unlocked by unlocking the folder, which leaves the folder's files locked.
"""

from datetime import datetime, timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q, QuerySet
from django.urls import reverse
from django.utils import timezone

from audit import trail
from notifications import categories
from notifications import services as notifications
from residents.models import Resident

from . import history, services
from .access import Viewer
from .models import LOCK_AFTER_DAYS, MAX_UNLOCK_DAYS, ApprovalRequest, File, FileVersion, Folder

Target = File | Folder


def _where(target: Target) -> str:
    folder = target.folder if isinstance(target, File) else target
    return folder.display_path()


def _page(target: Target) -> str:
    if isinstance(target, File):
        return reverse("drive:file", args=[target.uuid])
    return reverse("drive:folder", args=[target.uuid])


# --- Locking ---------------------------------------------------------------------------------------


def _lock(target: Target, actor: Resident | None, now: datetime) -> None:
    target.locked_at, target.locked_by, target.unlocked_until = now, actor, None
    target.save(update_fields=["locked_at", "locked_by", "unlocked_until"])
    kind = {"file": target} if isinstance(target, File) else {"folder": target}
    history.record(history.Action.LOCKED, actor=actor, name=target.name, automatic=actor is None, **kind)


def lock_file(viewer: Viewer, file: File, actor: Resident) -> None:
    """By hand. Also ends an unlock early."""
    if file.deleted_at is not None or not viewer.can_change_file(file):
        raise PermissionDenied
    with trail.event("drive.file.locked", subject=file, actor=actor):
        _lock(file, actor, timezone.now())


def lock_folder(viewer: Viewer, folder: Folder, actor: Resident) -> None:
    if folder.is_system or folder.deleted_at is not None or folder.lock_active():
        raise PermissionDenied
    if not viewer.may_write(folder) or viewer.lock_holder(folder) is not None:
        raise PermissionDenied
    with trail.event("drive.folder.locked", subject=folder, actor=actor):
        _lock(folder, actor, timezone.now())


def auto_lock(now: datetime | None = None) -> tuple[int, int]:
    """Lock what has had nothing new for LOCK_AFTER_DAYS. Returns (files, folders) locked."""
    now = now or timezone.now()
    cutoff = now - timedelta(days=LOCK_AFTER_DAYS)
    files = 0
    with trail.event("drive.items.auto_locked", actor=None, only_if_changed=True):
        for file in File.objects.filter(locked_at__isnull=True, deleted_at__isnull=True).exclude(
            versions__created_at__gte=cutoff
        ):
            _lock(file, None, now)
            files += 1
        folders = 0
        for folder in Folder.objects.filter(
            kind=Folder.Kind.FOLDER, locked_at__isnull=True, deleted_at__isnull=True, created_at__lt=cutoff
        ):
            inside = folder.path
            if Folder.objects.filter(path__startswith=inside, created_at__gte=cutoff).exists():
                continue
            if FileVersion.objects.filter(
                file__folder__path__startswith=inside, created_at__gte=cutoff
            ).exists():
                continue
            _lock(folder, None, now)
            folders += 1
    return files, folders


# --- Requests --------------------------------------------------------------------------------------


def _reason(reason: str) -> str:
    reason = reason.strip()
    if not reason:
        raise ValidationError("Skriv en begrundelse.")
    return reason[:2000]


def _pending(**target: object) -> bool:
    return ApprovalRequest.objects.filter(state=ApprovalRequest.State.PENDING, **target).exists()


def request_unlock(
    viewer: Viewer, target: Target, days: int, reason: str, actor: Resident
) -> ApprovalRequest:
    with trail.event("drive.unlock.requested", actor=actor, involve=[(target, "target")]) as event:
        request = _request_unlock(viewer, target, days, reason, actor)
        event.involve(request, "subject")
        return request


def _request_unlock(
    viewer: Viewer, target: Target, days: int, reason: str, actor: Resident
) -> ApprovalRequest:
    if not target.lock_active() or target.deleted_at is not None:
        raise ValidationError("Den er ikke låst.")
    folder = target.folder if isinstance(target, File) else target
    # Unlocking never widens access: only who could write here without the lock may ask.
    if not viewer.may_write(folder):
        raise PermissionDenied
    if not 1 <= days <= MAX_UNLOCK_DAYS:
        raise ValidationError(f"Højst {MAX_UNLOCK_DAYS} dage.")
    kind = {"file": target} if isinstance(target, File) else {"folder": target}
    if _pending(kind=ApprovalRequest.Kind.UNLOCK, **kind):
        raise ValidationError("Der er allerede bedt om at låse den op.")
    reason = _reason(reason)
    request = ApprovalRequest.objects.create(
        kind=ApprovalRequest.Kind.UNLOCK,
        name=target.name,
        where=_where(target),
        days=days,
        reason=reason,
        requested_by=actor,
        shared_id=folder.shared_id,
        **kind,
    )
    history.record(
        history.Action.UNLOCK_REQUESTED, actor=actor, name=target.name, days=days, reason=reason, **kind
    )
    return request


def request_purge(
    viewer: Viewer, file: File, number: int | None, reason: str, actor: Resident
) -> ApprovalRequest:
    """`number`: one old version of a file; None: a file in the bin, with all its versions."""
    with trail.event("drive.purge.requested", actor=actor, involve=[(file, "target")]) as event:
        request = _request_purge(viewer, file, number, reason, actor)
        event.involve(request, "subject")
        return request


def _request_purge(
    viewer: Viewer, file: File, number: int | None, reason: str, actor: Resident
) -> ApprovalRequest:
    if number is None:
        if file.deleted_at is None:
            raise ValidationError("Kun en fil i papirkurven kan slettes permanent.")
        if not viewer.may_write(file.folder):
            raise PermissionDenied
    else:
        if file.deleted_at is not None or not viewer.can_change_file(file):
            raise PermissionDenied
        if not file.versions.filter(number=number).exists() or number == file.latest_version().number:
            raise ValidationError("Den nyeste version kan ikke slettes. Læg filen i papirkurven i stedet.")
    if _pending(kind=ApprovalRequest.Kind.PURGE, file=file, version_number=number):
        raise ValidationError("Der er allerede bedt om at slette den permanent.")
    reason = _reason(reason)
    request = ApprovalRequest.objects.create(
        kind=ApprovalRequest.Kind.PURGE,
        file=file,
        version_number=number,
        name=file.name,
        where=_where(file),
        reason=reason,
        requested_by=actor,
        shared_id=file.folder.shared_id,
    )
    history.record(
        history.Action.PURGE_REQUESTED, actor=actor, file=file, name=file.name, number=number, reason=reason
    )
    return request


def decides(viewer: Viewer) -> QuerySet[ApprovalRequest]:
    """The requests this viewer decides: admins and Inspektionen those outside Delte mapper, a
    moderator those in their Delte mapper."""
    moderated = [pk for pk in viewer.shared_roles if viewer.moderates(pk)]
    mine = Q(shared__in=moderated)
    if viewer.oversees:
        mine |= Q(shared__isnull=True)
    return ApprovalRequest.objects.filter(mine)


def pending_count(viewer: Viewer) -> int:
    return decides(viewer).filter(state=ApprovalRequest.State.PENDING).count()


# --- Deciding --------------------------------------------------------------------------------------


def _purge(request: ApprovalRequest, approver: Resident) -> None:
    file = request.file
    if file is None:
        raise ValidationError("Filen findes ikke længere.")
    requested_by = request.requested_by.full_name if request.requested_by else ""

    def record() -> None:
        history.record(
            history.Action.PURGED,
            actor=approver,
            file=file,
            name=file.name,
            number=request.version_number,
            requested_by=requested_by,
            approved_by=approver.full_name,
        )

    if request.version_number is None:
        if file.deleted_at is None:
            raise ValidationError("Filen er blevet gendannet fra papirkurven siden.")
        keys = set(file.versions.values_list("key", flat=True))
        record()
        file.delete()
        services.drop_unreferenced(keys)
        return
    version = file.versions.filter(number=request.version_number).first()
    if version is None or version.number == file.latest_version().number:
        raise ValidationError("Versionen kan ikke slettes nu.")
    key = version.key
    version.delete()
    record()
    services.drop_unreferenced({key})


def decide(viewer: Viewer, request: ApprovalRequest, *, approve: bool, reason: str, actor: Resident) -> None:
    action = f"drive.{request.kind}.{'approved' if approve else 'rejected'}"
    with trail.event(
        action, subject=request, actor=actor, involve=[(request.file or request.folder, "target")]
    ):
        _decide(viewer, request, approve=approve, reason=reason, actor=actor)


def _decide(viewer: Viewer, request: ApprovalRequest, *, approve: bool, reason: str, actor: Resident) -> None:
    request = ApprovalRequest.objects.select_for_update().get(pk=request.pk)
    if not (viewer.moderates(request.shared_id) if request.shared_id else viewer.oversees):
        raise PermissionDenied
    if request.state != ApprovalRequest.State.PENDING:
        raise ValidationError("Anmodningen er allerede besluttet.")
    if request.requested_by_id == actor.pk:
        raise ValidationError("En anden skal beslutte din egen anmodning.")
    reason = reason.strip()
    if not approve and not reason:
        raise ValidationError("Skriv hvorfor anmodningen afvises.")
    now = timezone.now()
    target: Target | None = request.file or request.folder
    kind = {"file": request.file} if request.file else {"folder": request.folder}
    if request.kind == ApprovalRequest.Kind.UNLOCK:
        if approve:
            if target is None:
                raise ValidationError("Den findes ikke længere.")
            target.unlocked_until = now + timedelta(days=request.days or 1)
            target.save(update_fields=["unlocked_until"])
        until = timezone.localtime(target.unlocked_until).strftime("%-d/%-m %Y") if approve and target else ""
        history.record(
            history.Action.UNLOCK_DECIDED,
            actor=actor,
            name=request.name,
            approved=approve,
            until=until,
            reason=reason,
            **kind,
        )
    elif approve:
        _purge(request, actor)
        if request.file is not None and request.file.pk is None:  # the whole file went
            request.file = None
    else:
        history.record(
            history.Action.PURGE_DECIDED,
            actor=actor,
            name=request.name,
            number=request.version_number,
            reason=reason,
            **kind,
        )
    request.state = ApprovalRequest.State.APPROVED if approve else ApprovalRequest.State.REJECTED
    request.decided_by, request.decided_at, request.decision_reason = actor, now, reason
    request.save()
    _tell_requester(request, target)


def _tell_requester(request: ApprovalRequest, target: Target | None) -> None:
    if request.requested_by_id is None:
        return
    what = (
        f"låse {request.name} op"
        if request.kind == ApprovalRequest.Kind.UNLOCK
        else f"slette version {request.version_number} af {request.name} permanent"
        if request.version_number
        else f"slette {request.name} permanent"
    )
    approved = request.state == ApprovalRequest.State.APPROVED
    exists = target is not None and target.pk is not None and (target.deleted_at is None)
    notifications.notify(
        [request.requested_by_id],
        category=categories.GENERAL,
        title=f"Din anmodning om at {what} er {'godkendt' if approved else 'afvist'}",
        body="" if approved else request.decision_reason,
        url=_page(target) if exists and target is not None else reverse("drive:home"),
    )
