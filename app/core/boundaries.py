"""The half of the domain boundaries a static import check cannot see: the ORM's reverse accessors.

Tach (tach.toml) stops `from opslagstavle.models import Notice` in `events`. It cannot stop
`event.notices.all()`, because that needs no import at all: a ForeignKey("events.Event") declared in
opslagstavle hands every Event a `notices` manager onto opslagstavle's table, and any code holding an
Event can read it — or `.update()` it. Same for every FK into `residents`: `resident.ak_entries`,
`resident.shopper_accounts` and four dozen more, on the one model every domain holds.

THE RULE: a relation to a model in another domain sets `related_name="+"`. The owning side keeps its
forward accessor (`notice.event_id` is how it stores the link); the other domain gets nothing to
traverse. Code that wants the other direction asks the owning domain's `api.py`.

  core.E010  a cross-domain relation creates a reverse accessor and is not in BASELINE
  core.E011  a BASELINE entry no longer needs to be there — delete the line

BASELINE is the list of relations that broke the rule before it existed. It may only shrink: E011
makes a fixed entry fail the check until it is removed, so nobody can fix one and quietly re-add
another. Burning an entry down means `related_name="+"`, a (SQL-free) AlterField migration, and
routing whoever used the accessor through the owning domain's api.

What this still cannot see, and why it is not a substitute for review: the FORWARD accessor
(`notice.event.title` reads the events table) and `__` lookups that cross into another domain
(`Notice.objects.filter(event__visibility=...)`). Both are ordinary Django and both compile to a join.
See docs/domain-boundaries.md, "What is not enforced".

Domains are the project's own apps — everything whose code lives under BASE_DIR — so a new app is
covered the moment it is in INSTALLED_APPS.
"""

from collections.abc import Sequence
from pathlib import Path

from django.apps import apps
from django.apps.config import AppConfig
from django.conf import settings
from django.core.checks import CheckMessage, Error
from django.db import models

# Existing cross-domain relations with a reverse accessor, as "app_label.Model.field". Do not add to
# this list. See the module docstring for how to remove an entry.
BASELINE = frozenset(
    {
        "admissions.Application.discarded_by",
        "admissions.Application.received_by",
        "ak.AkEntry.created_by",
        "ak.AkEntry.resident",
        "ak.AkMonthlyCharge.updated_by",
        "arkiv.ArchiveFolder.workgroup",
        "cms.CmsImage.uploaded_by",
        "cms.PageRedirect.created_by",
        "cms.PageVersion.created_by",
        "core.PushSubscription.user",
        "den_hurtige.QuickComment.author",
        "den_hurtige.QuickPost.author",
        "den_hurtige.QuickReaction.author",
        "events.CalendarFeedToken.resident",
        "events.Event.co_organisers",
        "events.Event.organiser",
        "events.EventComment.author",
        "events.EventInvite.resident",
        "events.Rsvp.resident",
        "oelkaelder.Shopper.resident",
        "opslagstavle.Notice.author",
        "opslagstavle.Notice.pinned_by",
        "opslagstavle.NoticeComment.author",
        "opslagstavle.NoticeImage.uploaded_by",
        "opslagstavle.NoticeReaction.author",
        "photo_album.AlbumDownload.requested_by",
        "photo_album.AlbumImport.requested_by",
        "photo_album.Media.approved_by",
        "photo_album.Media.deleted_by",
        "photo_album.Media.requested_by",
        "photo_album.MediaView.resident",
        "reparationer.RepairComment.author",
        "reparationer.RepairTask.reported_by",
        "residents.Residency.cleaning",
        "residents.Residency.room",
        "residents.Residency.workgroup",
        "rooms.KvotientApplication.resident",
        "rooms.RoomCondition.room",
        "rooms.RoomOffer.room",
    }
)


def _project_app_labels() -> set[str]:
    base = Path(settings.BASE_DIR).resolve()
    return {c.label for c in apps.get_app_configs() if Path(c.path).resolve().is_relative_to(base)}


def cross_domain_reverse_accessors() -> dict[str, str]:
    """Every relation from one project app to another that leaves a reverse accessor behind, as
    {"app.Model.field": "target app.Model"}."""
    domains = _project_app_labels()
    found: dict[str, str] = {}
    for model in apps.get_models():
        source = model._meta.app_label
        if source not in domains:
            continue
        for field in model._meta.get_fields():
            if not isinstance(field, models.Field) or not field.is_relation or field.auto_created:
                continue
            target = field.related_model
            if target is None or isinstance(target, str):
                continue
            if target._meta.app_label not in domains or target._meta.app_label == source:
                continue
            if field.remote_field is None or field.remote_field.hidden:  # related_name "+"
                continue
            found[f"{model._meta.label}.{field.name}"] = target._meta.label
    return found


def check_cross_domain_relations(
    app_configs: Sequence[AppConfig] | None, **kwargs: object
) -> list[CheckMessage]:
    found = cross_domain_reverse_accessors()
    errors: list[CheckMessage] = [
        Error(
            f"{key} -> {target} gives {target} a reverse accessor into another domain's table.",
            hint='Set related_name="+" and expose what the other side needs through this app\'s api.py '
            "(docs/domain-boundaries.md).",
            id="core.E010",
        )
        for key, target in sorted(found.items())
        if key not in BASELINE
    ]
    errors += [
        Error(
            f"{key} is in core.boundaries.BASELINE but no longer has a cross-domain reverse accessor.",
            hint="Delete it from BASELINE, so the list keeps shrinking.",
            id="core.E011",
        )
        for key in sorted(BASELINE - found.keys())
    ]
    return errors
