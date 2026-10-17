"""What GAHKFlix tells people, through the notifications app. Wording and audiences only."""

from collections.abc import Iterable
from datetime import datetime, time, timedelta

from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from notifications import categories
from notifications.categories import Importance
from notifications.services import everyone, notify
from residents.models import Resident

from . import access
from .models import ChangeRequest, Video


def _video_url(video: Video) -> str:
    return reverse("gahkflix:detail", args=[video.pk])


def _edit_url(video: Video) -> str:
    return reverse("gahkflix:edit", args=[video.pk])


def published(video: Video) -> None:
    notify(
        access.audience(everyone()),
        category=categories.GAHKFLIX_NEW,
        title=f"Ny på GAHKFlix: {video.title}",
        body=video.description[:140],
        url=_video_url(video),
        exclude=video.uploaded_by_id,
    )


def credited(video: Video, residents: Iterable[int], by: Resident) -> None:
    """Residents newly added to a published video's cast. Only once it is out: a draft is private."""
    ids = [pk for pk in residents if pk != by.pk]
    if not ids or not video.is_published:
        return
    recipients = access.audience(Resident.objects.filter(pk__in=ids))
    notify(
        recipients,
        category=categories.GAHKFLIX_CAST,
        title=f"Du medvirker i {video.title}",
        body=f"{by.full_name} har krediteret dig på GAHKFlix.",
        url=_video_url(video),
    )


def processed(video: Video, *, revision: bool) -> None:
    if revision:
        title = f"Den nye version af {video.title} er klar"
        body = "Den afspilles nu. Du kan vælge nye billeder, hvis du vil."
    else:
        title = f"{video.title} er klar"
        body = "Vælg miniature og banner, og udgiv videoen."
    notify(
        [video.uploaded_by_id],
        category=categories.GAHKFLIX_UPLOADS,
        title=title,
        body=body,
        url=_edit_url(video),
    )


def processing_failed(video: Video, reason: str) -> None:
    notify(
        [video.uploaded_by_id],
        category=categories.GAHKFLIX_UPLOADS,
        title=f"{video.title} kunne ikke behandles",
        body=reason,
        url=_edit_url(video),
    )


def request_opened(change: ChangeRequest) -> None:
    target = change.target
    what = "slette" if change.kind == ChangeRequest.Kind.DELETE else "ændre"
    notify(
        access.administrators(),
        category=categories.GAHKFLIX_APPROVALS,
        title=f"{change.requested_by.full_name} vil {what} {target}",
        body="Det kræver en anden administrators godkendelse.",
        url=reverse("gahkflix:request", args=[change.pk]),
        exclude=change.requested_by_id,
    )


def request_decided(change: ChangeRequest) -> None:
    what = "sletning" if change.kind == ChangeRequest.Kind.DELETE else "ændring"
    verdict = change.get_state_display().lower()
    notify(
        [change.requested_by_id],
        category=categories.GAHKFLIX_UPLOADS,
        title=f"Din anmodning om {what} af {change.target} er {verdict}",
        body=change.note,
        url=reverse("gahkflix:request", args=[change.pk]),
    )


def _day(video: Video) -> str:
    return date_format(video.draft_deleted_on, "j. F")


def _end_of(video: Video) -> datetime:
    """A reminder about a draft means nothing once the draft is gone."""
    return timezone.make_aware(datetime.combine(video.draft_deleted_on, time(6, 0)))


def drafts_reminder(drafts: list[Video]) -> None:
    """The weekly reminder to one uploader about their own unpublished drafts, first-deleted first."""
    first = drafts[0]
    if len(drafts) == 1:
        title = f"{first.title} er ikke udgivet endnu"
        url = _edit_url(first)
    else:
        title = f"Du har {len(drafts)} videoer, der ikke er udgivet"
        url = reverse("gahkflix:mine")
    notify(
        [first.uploaded_by_id],
        category=categories.GAHKFLIX_UPLOADS,
        title=title,
        body=f"Kladder slettes, hvis de ikke udgives. {first.title} slettes {_day(first)}.",
        url=url,
        expires_at=min(timezone.now() + timedelta(days=7), _end_of(first)),
    )


def drafts_overview(drafts: list[Video]) -> None:
    """The weekly overview of every unpublished draft, for the administrators."""
    first = drafts[0]
    count = len(drafts)
    notify(
        access.administrators(),
        category=categories.GAHKFLIX_DRAFTS,
        title=f"{count} video{'er' if count != 1 else ''} på GAHKFlix er ikke udgivet",
        body=f"Den første, {first.title} fra {first.uploaded_by.full_name}, slettes {_day(first)}.",
        url=reverse("gahkflix:drafts"),
        expires_at=min(timezone.now() + timedelta(days=7), _end_of(first)),
    )


def draft_expiring(video: Video) -> None:
    """The day before a draft is deleted: its uploader, and the administrators."""
    notify(
        [video.uploaded_by_id],
        category=categories.GAHKFLIX_UPLOADS,
        title=f"{video.title} slettes i nat",
        body="Kladden er ikke udgivet og slettes med alle sine filer. Udgiv den i dag, hvis den skal blive.",
        url=_edit_url(video),
        importance=Importance.HOEJ,
        expires_at=_end_of(video),
    )
    notify(
        access.administrators(),
        category=categories.GAHKFLIX_DRAFTS,
        title=f"Kladden {video.title} slettes i nat",
        body=f"{video.uploaded_by.full_name} har ikke udgivet den.",
        url=_edit_url(video),
        importance=Importance.HOEJ,
        exclude=video.uploaded_by_id,
        expires_at=_end_of(video),
    )
