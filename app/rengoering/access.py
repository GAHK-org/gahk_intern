"""Who may see and do what in søndagsrengøring.

  * Every logged-in resident sees **their own** assignments, inspections and mangler, and may sign
    off and mark mangler fixed on them. Anyone else's assignment is a 404, not a 403: a beboer has
    no business learning that an id exists or whose it is.
  * Inspektionen (of the active month), administrators and superusers plan rotas, edit assignments,
    inspect, and see the overview and history. Superuser and administrator already resolve to every
    role in residents.permissions.real_roles, so naming them here is for the reader.

Effective roles throughout, so the preview tool ("view the site as a beboer") drops the manager
controls the way it does everywhere else.
"""

from django.http import Http404, HttpRequest

from residents.models import Role
from residents.permissions import request_has_role, role_required

from .models import CleaningAssignment

MANAGE_ROLES = (Role.INSPEKTION, Role.ADMINISTRATOR)

manage_required = role_required(*MANAGE_ROLES)


def can_manage(request: HttpRequest) -> bool:
    return request_has_role(request, *MANAGE_ROLES)


def assignment_for(request: HttpRequest, pk: int) -> CleaningAssignment:
    """The assignment if the request may see it — its own resident or a manager — else 404."""
    try:
        assignment = CleaningAssignment.objects.select_related("area", "rota", "resident").get(pk=pk)
    except CleaningAssignment.DoesNotExist:
        raise Http404 from None
    if assignment.resident_id != request.user.pk and not can_manage(request):
        raise Http404
    return assignment
