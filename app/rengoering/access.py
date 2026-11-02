"""Who may see and do what in søndagsrengøring.

  * Every logged-in resident sees **their own** assignments, inspections and mangler, and may sign
    off and mark mangler fixed on them. Anyone else's assignment is a 404, not a 403: a beboer has
    no business learning that an id exists or whose it is.
  * Inspektionen (of the active month), administrators and superusers plan rotas, edit assignments,
    inspect, and see the overview and history. Superuser and administrator already resolve to every
    role in residents.permissions.real_roles, so naming them here is for the reader.
  * The handover after a generalforsamling (March and September): the new Inspektion gets the role
    the month after (April, October) but only starts inspecting the month after that (May,
    November). So in April and October the OUTGOING Inspektion — who held the role the month
    before — still inspects: the walk, the overview and the history, and the assignment pages they
    reach from them. Not the rotas; those are the new Inspektion's from the day they get the role,
    and the new Inspektion keeps everything the role gives them (they can shadow the walk).

Effective roles throughout, so the preview tool ("view the site as a beboer") drops the manager
controls the way it does everywhere else.
"""

from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest
from django.http.response import HttpResponseBase

from residents.models import Resident, Role, RoleAssignment, prev_period
from residents.permissions import PREVIEW_SESSION_KEY, View, request_has_role, role_required, user_period

from .models import CleaningAssignment

MANAGE_ROLES = (Role.INSPEKTION, Role.ADMINISTRATOR)

manage_required = role_required(*MANAGE_ROLES)


# The months a new Inspektion holds the role while the outgoing one still inspects: the month after
# the generalforsamling in March and in September.
HANDOVER_MONTHS = (4, 10)
_OUTGOING_MEMO = "_rengoering_outgoing"


def can_manage(request: HttpRequest) -> bool:
    return request_has_role(request, *MANAGE_ROLES)


def is_outgoing_inspektion(user: object) -> bool:
    """Whether `user` held Inspektion the month before a handover month that is now active.

    The sidebar asks on every page, so this reuses the period `real_roles` already resolved, only
    queries in April and October, and is memoised on the user for the request like the roles are."""
    if not isinstance(user, Resident):
        return False
    cached = getattr(user, _OUTGOING_MEMO, None)
    if cached is None:
        period = user_period(user)
        year, month = prev_period(period)
        cached = period[1] in HANDOVER_MONTHS and (
            RoleAssignment.objects.filter(
                resident=user, role=Role.INSPEKTION, year=year, month=month
            ).exists()
        )
        setattr(user, _OUTGOING_MEMO, cached)
    return cached


def can_inspect(request: HttpRequest) -> bool:
    """Managers, plus the outgoing Inspektion in a handover month. A role preview ("view the site as
    a beboer") is a preview of roles, so it switches the handover exception off with them."""
    if can_manage(request):
        return True
    return PREVIEW_SESSION_KEY not in request.session and is_outgoing_inspektion(request.user)


def inspect_required(view: View) -> View:
    @wraps(view)
    def wrapped(request: HttpRequest, *args: object, **kwargs: object) -> HttpResponseBase:
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not can_inspect(request):
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapped


def assignment_for(request: HttpRequest, pk: int) -> CleaningAssignment:
    """The assignment if the request may see it — its own resident or an inspector — else 404.
    Editing it is still for managers: those views sit behind `manage_required` as well."""
    try:
        assignment = CleaningAssignment.objects.select_related("area", "rota", "resident").get(pk=pk)
    except CleaningAssignment.DoesNotExist:
        raise Http404 from None
    if assignment.resident_id != request.user.pk and not can_inspect(request):
        raise Http404
    return assignment
