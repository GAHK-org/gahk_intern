"""Who may accept a pending Django-admin grant, and what accepting does.

The policy is a **two-person rule inside Netvaerksgruppen**, not an external check: approvers are
administrators who already hold accepted access (plus real superusers, who are what makes the first
grant possible at all). That only buys something if the two people are different, so an approver may
decide neither their own grant nor one they created themselves — otherwise a single administrator
could grant the role and accept it in two clicks and the gate would be decoration.

A consequence worth knowing: a majority of the group can still admit a newcomer between them. This
raises the cost of a unilateral grant; it does not make the group accountable to anyone outside it.
"""

from django.db import transaction
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


def reopen_denied(resident_id: int, requested_by: Resident | None) -> bool:
    """Put a refused grant back up for decision when someone grants `administrator` again.

    A grant only disappears once the resident holds the role in no month at all (signals._close_grant),
    and role rows are history that is never deleted — so a refused administrator keeps a DENIED grant
    for good, and without this a later, deliberate re-grant would quietly open nothing. Called from the
    role editor only, not from the signal: the monthly roll-forward also mints role rows, and it must
    not turn a refusal into a monthly re-request. Returns whether a grant was reopened.
    """
    reopened = AdminAccessGrant.objects.filter(
        resident_id=resident_id, status=AdminAccessGrant.Status.DENIED
    ).update(
        status=AdminAccessGrant.Status.PENDING,
        requested_by=requested_by,
        decided_by=None,
        decided_at=None,
        created_at=timezone.now(),
    )
    if reopened:
        from .tasks import notify_admin_access_request  # tasks imports this module

        grant_id = AdminAccessGrant.objects.get(resident_id=resident_id).pk
        transaction.on_commit(lambda: notify_admin_access_request.delay(grant_id))
    return bool(reopened)
