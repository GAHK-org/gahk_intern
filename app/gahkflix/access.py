"""Who may reach GAHKFlix, and who may do what to a video.

STAGED ROLLOUT: limited to administrators and Fotogruppen while it is tried out — except with DEBUG
on, where every resident is let in, so a non-administrator's GAHKFlix can be tried locally (log in
as one of seed_demo's residents).

    TO OPEN IT TO EVERY RESIDENT: set ACCESS_ROLES to None.

That one edit widens the views, the sidebar entry, the "Under test" chip, the resident profile's
"Medvirker i" strip and the audience of the "new video" notification together (core.rollout).

"ADMINISTRATORS" in the spec are the administrator role and Fotogruppen, and that is what
`is_administrator` answers — an administrator holds every role, so asking for Fotogruppen's covers
both. They are the people who may bin an old video (with two others agreeing), approve late changes
and restore from the bin.

Every rule that depends on time runs from the upload (models.Video.created_at), and none of them
applies to a draft: until it is published a video is its uploader's own, which they may edit, replace
and delete as they like. The only other people who see a draft are the administrators.
"""

from collections.abc import Collection
from datetime import datetime

from django.conf import settings
from django.db.models import Q, QuerySet
from django.http import HttpRequest
from django.utils import timezone

from core.rollout import Gate
from residents.models import Resident, Role, RoleAssignment, active_period
from residents.permissions import current_resident, request_has_role

from .models import ChangeRequest, Series, Video

# None = every logged-in resident. Administrators are listed for the notification audience, which
# is matched on role assignments; for the views an administrator holds every role anyway.
ACCESS_ROLES: tuple[str, ...] | None = (Role.ADMINISTRATOR, Role.FOTO)


def _access_roles() -> tuple[str, ...] | None:
    """ACCESS_ROLES, or nobody kept out at all in local development. Read on every call (core.rollout)."""
    return None if settings.DEBUG else ACCESS_ROLES


gate = Gate(_access_roles)
access_required = gate.required

ADMINISTRATOR_ROLES = (Role.ADMINISTRATOR, Role.FOTO)


def roles_allowed(roles: Collection[str]) -> bool:
    return gate.roles_allowed(roles)


def request_allowed(request: HttpRequest) -> bool:
    return gate.request_allowed(request)


def _with_roles(residents: QuerySet[Resident], roles: Collection[str]) -> QuerySet[Resident]:
    year, month = active_period()
    holders = RoleAssignment.objects.filter(role__in=roles, year=year, month=month).values("resident_id")
    return residents.filter(Q(pk__in=holders) | Q(is_superuser=True)).filter(is_active=True)


def audience(residents: QuerySet[Resident]) -> QuerySet[Resident]:
    """`residents` narrowed to those who can open GAHKFlix — a notification must not lead to a 403."""
    roles = _access_roles()
    if roles is None:
        return residents
    return _with_roles(residents, roles)


def administrators() -> QuerySet[Resident]:
    """Everyone who may approve requests this month, as a notification audience."""
    return _with_roles(Resident.objects.all(), ADMINISTRATOR_ROLES)


def is_administrator(request: HttpRequest) -> bool:
    return request_has_role(request, Role.FOTO)


def _now(now: datetime | None) -> datetime:
    return now or timezone.now()


def is_uploader(request: HttpRequest, video: Video) -> bool:
    return video.uploaded_by_id == current_resident(request).pk


def _owns_or_administers(request: HttpRequest, video: Video) -> bool:
    return is_uploader(request, video) or is_administrator(request)


def can_see(request: HttpRequest, video: Video) -> bool:
    if video.is_binned:
        return is_administrator(request)
    if video.is_draft:
        return _owns_or_administers(request, video)
    return True


def can_edit(request: HttpRequest, video: Video) -> bool:
    """Whether the edit page is open to this reader at all; see `edit_needs_approval` for how."""
    return not video.is_binned and _owns_or_administers(request, video)


def edit_needs_approval(video: Video, now: datetime | None = None) -> bool:
    """After 30 days a change is a proposal, which one other administrator must accept."""
    return video.is_published and _now(now) >= video.edit_window_ends


def can_upload_revision(request: HttpRequest, video: Video, now: datetime | None = None) -> bool:
    return can_edit(request, video) and (video.is_draft or _now(now) < video.revision_window_ends)


def can_delete_directly(request: HttpRequest, video: Video, now: datetime | None = None) -> bool:
    """A draft at any time (it is deleted outright); a published video within 7 days (to the bin)."""
    return can_edit(request, video) and (video.is_draft or _now(now) < video.direct_delete_window_ends)


def can_request_deletion(request: HttpRequest, video: Video, now: datetime | None = None) -> bool:
    return (
        is_administrator(request)
        and video.is_published
        and not video.is_binned
        and _now(now) >= video.direct_delete_window_ends
    )


def can_restore(request: HttpRequest, video: Video) -> bool:
    return video.is_binned and is_administrator(request)


def can_view_stats(request: HttpRequest, video: Video) -> bool:
    return _owns_or_administers(request, video)


def can_vote(request: HttpRequest, change: ChangeRequest) -> bool:
    """An administrator other than the one who asked, once per request."""
    resident = current_resident(request)
    return (
        change.is_open
        and is_administrator(request)
        and change.requested_by_id != resident.pk
        and not change.votes.filter(voter=resident).exists()
    )


def can_withdraw(request: HttpRequest, change: ChangeRequest) -> bool:
    return change.is_open and change.requested_by_id == current_resident(request).pk


def can_edit_series(request: HttpRequest, series: Series) -> bool:
    return series.created_by_id == current_resident(request).pk or is_administrator(request)


def series_edit_needs_approval(series: Series, now: datetime | None = None) -> bool:
    return _now(now) >= series.edit_window_ends


def can_delete_series(request: HttpRequest, series: Series) -> bool:
    """Only an empty series — drafts and binned videos count — so no video loses its show."""
    return can_edit_series(request, series) and not series.videos.exists()
