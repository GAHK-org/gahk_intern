"""Who may accept a pending Django-admin grant, and what accepting does.

The policy is a **two-person rule inside Netvaerksgruppen**, not an external check: approvers are
administrators who already hold accepted access (plus real superusers, who are what makes the first
grant possible at all). That only buys something if the two people are different, so an approver may
decide neither their own grant nor one they created themselves — otherwise a single administrator
could grant the role and accept it in two clicks and the gate would be decoration.

A consequence worth knowing: a majority of the group can still admit a newcomer between them. This
raises the cost of a unilateral grant; it does not make the group accountable to anyone outside it.
"""

from django.db.models import Q, QuerySet
from django.utils import timezone

from .models import AdminAccessGrant, Resident, Role, active_period


def approvers() -> QuerySet[Resident]:
    """Residents who may decide a grant: accepted administrators for the active period, or superusers."""
    year, month = active_period()
    return Resident.objects.filter(
        Q(is_superuser=True)
        | Q(
            role_assignments__role=Role.ADMINISTRATOR,
            role_assignments__year=year,
            role_assignments__month=month,
            admin_access__status=AdminAccessGrant.Status.APPROVED,
        ),
        is_active=True,
    ).distinct()


def can_decide(user: Resident, grant: AdminAccessGrant) -> bool:
    """Both halves of the two-person rule: an approver, and not the two people already involved."""
    if user.pk in {grant.resident_id, grant.requested_by_id}:
        return False
    return approvers().filter(pk=user.pk).exists()


def pending() -> QuerySet[AdminAccessGrant]:
    return AdminAccessGrant.objects.filter(status=AdminAccessGrant.Status.PENDING).select_related(
        "resident", "requested_by"
    )


def decide(grant: AdminAccessGrant, decided_by: Resident, *, approve: bool) -> None:
    grant.status = AdminAccessGrant.Status.APPROVED if approve else AdminAccessGrant.Status.DENIED
    grant.decided_by = decided_by
    grant.decided_at = timezone.now()
    grant.save(update_fields=["status", "decided_by", "decided_at"])
