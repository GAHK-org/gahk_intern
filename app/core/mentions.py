"""@mentions in resident-authored text.

`@[Anders Hansen](42)` names resident 42; `@[Inspektionen](g55)` names embedsgruppe (Workgroup) 55,
which reaches everyone in it on the active alumneliste. The token lives in the text itself, so no
feature needs a table for it: rendering, notifying and re-parsing an edit all read it back from the
body. The composer (frontend/src/mention.ts) inserts it; a hand-typed `@Anders` is just text.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from django.db.models import QuerySet

    from residents.models import Resident

# Groups: 1 the name as written, 2 "g" for an embedsgruppe or "" for a resident, 3 the pk.
MENTION_RE = re.compile(r"@\[([^\[\]\n]{1,100})\]\((g?)(\d{1,10})\)")


def is_group(match: re.Match[str]) -> bool:
    return match.group(2) == "g"


def mentioned_ids(text: str | None) -> set[int]:
    """Residents named directly in `text`."""
    return {int(m.group(3)) for m in MENTION_RE.finditer(text or "") if not is_group(m)}


def mentioned_groups(text: str | None) -> set[int]:
    """Embedsgrupper (Workgroup pks) named in `text`."""
    return {int(m.group(3)) for m in MENTION_RE.finditer(text or "") if is_group(m)}


def to_plain(text: str | None) -> str:
    """`text` with each token shown as `@Name` — for push bodies and other plain-text output."""
    return MENTION_RE.sub(lambda m: f"@{m.group(1)}", text or "")


def profile_url(pk: int) -> str:
    from django.urls import reverse

    return reverse("resident_profile", args=[pk])


def mentionable() -> QuerySet[Resident]:
    """Who can be mentioned: residents on the active period's alumneliste."""
    from residents.models import Resident, active_period

    year, month = active_period()
    return Resident.objects.filter(
        is_active=True, is_kitchen=False, residencies__year=year, residencies__month=month
    ).distinct()


def group_members(group_id: int) -> QuerySet[Resident]:
    """Mentionable residents in embedsgruppe `group_id` this period."""
    return mentionable().filter(residencies__workgroup_id=group_id)


def _reached(text: str | None) -> set[int]:
    """Every resident `text` reaches, directly or through a group."""
    ids = mentioned_ids(text)
    for group_id in mentioned_groups(text):
        ids |= set(group_members(group_id).values_list("pk", flat=True))
    return ids


def notify_mentions(
    text: str | None,
    *,
    actor: Resident,
    where: str,
    url: str,
    audience: QuerySet[Resident],
    old_text: str | None = None,
    expires_at: datetime | None = None,
) -> set[int]:
    """Notify residents newly mentioned in `text` who can open `url`. Returns their ids.

    `audience` is the feature's access gate, so nobody is told about something they would get a 403
    on. `old_text` is the previous version on an edit: only residents the edit newly reaches are
    told. A resident named directly is told that, not that their group was named too.
    """
    from core.models import Workgroup
    from notifications import categories, services

    from . import push

    already = _reached(old_text) | {actor.pk}
    allowed = mentionable().filter(pk__in=audience.values("pk"))
    body = push.preview(to_plain(text))

    def tell(ids: set[int], title: str) -> set[int]:
        recipients = set(allowed.filter(pk__in=ids - already).values_list("pk", flat=True))
        if recipients:
            services.notify(
                recipients,
                category=categories.MENTIONS,
                title=title,
                body=body,
                url=url,
                expires_at=expires_at,
                exclude=actor.pk,
            )
        already.update(recipients)
        return recipients

    told = tell(mentioned_ids(text), f"{actor.full_name} nævnte dig {where}")
    for group in Workgroup.objects.filter(pk__in=mentioned_groups(text)).order_by("name"):
        members = set(group_members(group.pk).values_list("pk", flat=True))
        told |= tell(members, f"{actor.full_name} nævnte {group.name} {where}")
    return told
