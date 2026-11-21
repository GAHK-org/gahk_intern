"""Who may open which mailbox.

A mailbox is opened by its GROUP: residents on one of its embedsgrupper this month, or holding one of
its roles (roles honor the admin preview, like everywhere else — residents.permissions). Nothing is
granted per person, so access follows the månedsliste without anyone remembering to update it.

Note what `roles` implies for administrators: residents.permissions gives the administrator role
every role, so a mailbox with any role listed is open to administrators too. A mailbox opened by
embedsgruppe alone is not.
"""

from django.db.models import Q, QuerySet
from django.http import Http404, HttpRequest

from arkiv.access import current_workgroup_ids
from residents.models import Residency, Resident, RoleAssignment, active_period
from residents.permissions import current_resident, effective_roles

from .models import Mailbox


def visible_mailboxes(request: HttpRequest) -> QuerySet[Mailbox]:
    """The ONLY queryset a view starts from. Role matching is done in Python: `roles` is a JSON list,
    and a portable "list contains any of" lookup does not exist across SQLite and Postgres."""
    resident = current_resident(request)
    roles = effective_roles(request)
    by_group = Q(workgroups__in=current_workgroup_ids(resident))
    candidates = Mailbox.objects.filter(is_active=True)
    ids = set(candidates.filter(by_group).values_list("id", flat=True))
    if roles:
        ids |= {m.id for m in candidates.exclude(roles=[]) if not roles.isdisjoint(m.clean_roles())}
    return Mailbox.objects.filter(id__in=ids)


def get_mailbox(request: HttpRequest, slug: str) -> Mailbox:
    """404 rather than 403 for a mailbox you may not open: its existence is not yours to learn."""
    mailbox = visible_mailboxes(request).filter(slug=slug).first()
    if mailbox is None:
        raise Http404
    return mailbox


def members(mailbox: Mailbox) -> QuerySet[Resident]:
    """The residents who can open `mailbox` right now — the people a sag may be handed to: on one of its
    embedsgrupper this month, or holding one of its roles this month. Administrators are NOT expanded:
    they can open a role-gated mailbox (see the module docstring) but are not on the group, and a sag
    handed to someone who is not working the inbox would just sit there."""
    year, month = active_period()
    ids = set(
        Residency.objects.filter(year=year, month=month, workgroup__in=mailbox.workgroups.all()).values_list(
            "resident_id", flat=True
        )
    )
    if roles := mailbox.clean_roles():
        ids |= set(
            RoleAssignment.objects.filter(year=year, month=month, role__in=roles).values_list(
                "resident_id", flat=True
            )
        )
    return Resident.objects.filter(id__in=ids, is_active=True).order_by("first_name", "last_name", "pk")


def has_any(request: HttpRequest) -> bool:
    """For the sidebar: show "Postkasser" only to someone it would not answer with an empty page."""
    # The sidebar asks on every page, so the common case — no mailbox configured at all — must cost one
    # trivial query, not the residency lookups behind visible_mailboxes (tests pin the per-request count).
    if not Mailbox.objects.filter(is_active=True).exists():
        return False
    return visible_mailboxes(request).exists()
